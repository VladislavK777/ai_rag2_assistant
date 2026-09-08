"""Ingestion: загрузка документов и аудио, статусы обработки."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from miniopy_async import Minio
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import check_workspace_access, get_current_user, get_db
from app.db.audit import audit
from app.db.models import Document, STTJob, User
from app.services.tasks import ingest_document, transcribe_audio

router = APIRouter()

ALLOWED_DOC_TYPES = {
    "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/plain", "text/markdown",
}
ALLOWED_AUDIO_TYPES = {"audio/wav", "audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/webm"}
MAX_DOC_SIZE = 200 * 1024 * 1024      # 200MB
MAX_AUDIO_SIZE = 2 * 1024 * 1024 * 1024  # 2GB


def _minio() -> Minio:
    s = get_settings()
    return Minio(s.minio_endpoint, s.minio_access_key, s.minio_secret_key, secure=False)


class DocumentStatus(BaseModel):
    id: str
    title: str
    status: str


@router.post("/upload", status_code=202)
async def upload_document(
    file: UploadFile,
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Загрузка документа → очередь индексации (асинхронно)."""
    if not await check_workspace_access(db, user, uuid.UUID(workspace_id), level="write"):
        await audit(db, user_id=str(user.id), action="doc_upload", decision="deny",
                    resource=workspace_id)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет прав на запись в это пространство")

    if file.content_type not in ALLOWED_DOC_TYPES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Неподдерживаемый формат")
    if file.size and file.size > MAX_DOC_SIZE:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Файл больше 200MB")

    doc = Document(
        workspace_id=uuid.UUID(workspace_id),
        minio_key=f"documents/{workspace_id}/{uuid.uuid4()}/{file.filename}",
        title=file.filename or "документ",
        mime_type=file.content_type,
        uploaded_by=user.id,
    )
    db.add(doc)
    await db.commit()

    client = _minio()
    await client.put_object(
        "documents", doc.minio_key.removeprefix("documents/"),
        file.file, length=-1, part_size=10 * 1024 * 1024,
        content_type=file.content_type,
    )

    ingest_document.delay(str(doc.id))
    await audit(db, user_id=str(user.id), action="doc_upload", decision="allow",
                resource=str(doc.id))
    return {"id": str(doc.id), "status": doc.status}


@router.post("/audio", status_code=202)
async def upload_audio(
    file: UploadFile,
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Загрузка аудио встречи → транскрипция → резюме → индексация."""
    if not await check_workspace_access(db, user, uuid.UUID(workspace_id), level="write"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет прав на запись")
    if file.content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Неподдерживаемый аудиоформат")

    job = STTJob(audio_minio_key=f"audio/{uuid.uuid4()}_{file.filename}")
    db.add(job)
    await db.commit()

    client = _minio()
    await client.put_object(
        "audio", job.audio_minio_key.removeprefix("audio/"),
        file.file, length=-1, part_size=10 * 1024 * 1024, content_type=file.content_type,
    )

    transcribe_audio.delay(str(job.id), workspace_id)
    await audit(db, user_id=str(user.id), action="audio_upload", decision="allow",
                resource=str(job.id))
    return {"id": str(job.id), "status": job.status}


@router.get("/status/{document_id}", response_model=DocumentStatus)
async def document_status(
    document_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await db.get(Document, uuid.UUID(document_id))
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Документ не найден")
    if not await check_workspace_access(db, user, doc.workspace_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет доступа")
    return DocumentStatus(id=str(doc.id), title=doc.title, status=doc.status)


@router.get("/stt/{job_id}")
async def stt_status(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(STTJob, uuid.UUID(job_id))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Задача не найдена")
    return {
        "id": str(job.id),
        "status": job.status,
        "summary": job.summary,
        "action_items": job.action_items or [],
    }
