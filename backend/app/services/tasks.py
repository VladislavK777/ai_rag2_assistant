"""Задачи ingestion (парсинг → чанкинг → эмбеддинг → Qdrant) и STT."""
import asyncio
import uuid

from app.services.celery_app import celery_app


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def ingest_document(self, document_id: str):
    """Пайплайн индексации документа.

    1. Скачать из MinIO
    2. Извлечь текст (pdf/docx/pptx/xlsx)
    3. Чанкинг (1024 токена, overlap 100)
    4. Эмбеддинг батчами (BGE-M3)
    5. Upsert в Qdrant с ACL-payload
    """
    _run(_ingest_document_async(document_id))


async def _ingest_document_async(document_id: str) -> None:
    from sqlalchemy import update

    from app.core.config import get_settings
    from app.core.security import SessionLocal
    from app.db.models import Chunk, Document
    from app.rag.ingestion import extract_text, chunk_text, embed_batch, upsert_chunks

    settings = get_settings()
    async with SessionLocal() as db:
        doc = await db.get(Document, uuid.UUID(document_id))
        if doc is None:
            return
        try:
            await db.execute(
                update(Document).where(Document.id == doc.id).values(status="parsed")
            )
            await db.commit()

            client = _minio_client()
            data = await client.get_object("documents", doc.minio_key.removeprefix("documents/"))
            raw = await data.read()
            data.close()
            await data.release()

            text = extract_text(raw, doc.mime_type)
            chunks = chunk_text(text, size=512, overlap=100)

            # Пометка чанков с ПДн: retriever маскирует их пользователям
            # без права pii_read (контекст сохраняется, персоналия — нет)
            from app.guardrails.guardrails import detect_pii_types

            pii_types = [detect_pii_types(c) for c in chunks]

            qdrant_point_ids = []
            for i, chunk in enumerate(chunks):
                point_id = str(uuid.uuid4())
                qdrant_point_ids.append(point_id)
                db.add(
                    Chunk(
                        document_id=doc.id,
                        qdrant_point_id=point_id,
                        content=chunk,
                        chunk_index=i,
                        token_count=len(chunk) // 3,
                        metadata_json={
                            "title": doc.title,
                            "pii_types": pii_types[i],
                        },
                    )
                )
            await db.commit()

            await upsert_chunks(
                collection=f"ws_{str(doc.workspace_id).replace('-', '_')}",
                point_ids=qdrant_point_ids,
                chunks=chunks,
                payload_base={
                    "document_id": str(doc.id),
                    "title": doc.title,
                    "workspace_id": str(doc.workspace_id),
                },
                acl_payload={
                    # ACL-метки точки: все пользователи workspace получают доступ.
                    # admin/owner-ACL копится в БД; для pre-filter в Qdrant
                    # MVP-логика: точка доступна любому, кто видит workspace
                    "acl_groups": [str(doc.workspace_id)],
                    "pii_types": [t for types in pii_types for t in types] or [],
                },
            )
            await db.execute(
                update(Document).where(Document.id == doc.id).values(status="indexed")
            )
            await db.commit()

            # GraphRAG: экстракция сущностей/связей → graph_nodes/graph_edges.
            # После status=indexed: сбой экстракции не влияет на доступность документа
            try:
                from app.rag.graphrag import extract_and_store

                n, e = await extract_and_store(
                    str(doc.workspace_id), doc.title, chunks
                )
                if n:
                    import logging

                    logging.getLogger("rag2.graphrag").info(
                        "graph extracted: doc=%s nodes=%d edges=%d", doc.id, n, e
                    )
            except Exception:  # noqa: BLE001 — граф не критичен для RAG
                import logging

                logging.getLogger("rag2.graphrag").exception(
                    "graph extraction failed: doc=%s", doc.id
                )
        except Exception:
            await db.rollback()
            await db.execute(
                update(Document).where(Document.id == doc.id).values(status="failed")
            )
            await db.commit()
            raise


@celery_app.task(bind=True, max_retries=2, default_retry_delay=60)
def transcribe_audio(self, job_id: str, workspace_id: str):
    """STT-пайплайн: аудио → GigaAM → резюме (LLM) → индексация в workspace."""
    _run(_transcribe_async(job_id, workspace_id))


async def _transcribe_async(job_id: str, workspace_id: str) -> None:
    import httpx
    from sqlalchemy import update

    from app.core.config import get_settings
    from app.core.security import SessionLocal
    from app.db.models import Document, STTJob

    settings = get_settings()
    async with SessionLocal() as db:
        job = await db.get(STTJob, uuid.UUID(job_id))
        if job is None:
            return
        await db.execute(
            update(STTJob).where(STTJob.id == job.id).values(status="transcribing")
        )
        await db.commit()

        async with httpx.AsyncClient(timeout=3600) as client:
            resp = await client.post(
                f"{settings.stt_url}/transcribe",
                json={"minio_key": job.audio_minio_key},
            )
            transcript = resp.json()["transcript"]

        summary_resp_content = await _summarize(transcript)
        import json as _json

        try:
            parsed = _json.loads(
                summary_resp_content[
                    summary_resp_content.index("{") : summary_resp_content.rindex("}") + 1
                ]
            )
        except ValueError:
            parsed = {"summary": summary_resp_content, "action_items": []}
        except ValueError:
            parsed = {"summary": content, "action_items": []}

        # Индексация транскрипта как документа workspace
        doc = Document(
            workspace_id=uuid.UUID(workspace_id),
            minio_key=f"documents/{workspace_id}/transcripts/{job.id}.txt",
            title=f"Транскрипт встречи {job.created_at:%d.%m.%Y}",
            mime_type="text/plain",
            uploaded_by=job.created_by if hasattr(job, "created_by") else None,
        )
        db.add(doc)
        await db.commit()

        await db.execute(
            update(STTJob)
            .where(STTJob.id == job.id)
            .values(
                status="done",
                transcript=transcript,
                summary=parsed.get("summary"),
                action_items=parsed.get("action_items", []),
                document_id=doc.id,
            )
        )
        await db.commit()

        ingest_document.delay(str(doc.id))


async def _summarize(transcript: str) -> str:
    """Резюме встречи через LLM (локальный vLLM или Yandex GPT)."""
    from app.core.llm_provider import llm_chat

    return await llm_chat(
        [
            {
                "role": "user",
                "content": (
                    "Сделай резюме встречи и список action items.\n\n"
                    f"Транскрипт:\n{transcript[:30000]}\n\n"
                    'Ответ в JSON: {"summary": "...", "action_items": ["..."]}'
                ),
            }
        ],
        max_tokens=1000,
    )


def _minio_client():
    from miniopy_async import Minio

    from app.core.config import get_settings

    s = get_settings()
    return Minio(s.minio_endpoint, s.minio_access_key, s.minio_secret_key, secure=False)
