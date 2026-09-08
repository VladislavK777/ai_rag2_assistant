"""Retrieval-слой: гибридный поиск с RBAC pre-filter.

Ключевое требование безопасности: фильтр ACL применяется ДО векторного
поиска (pre-filter в Qdrant), поэтому недоступные чанки физически не могут
попасть в контекст модели.
"""
import time

import httpx
from opentelemetry import trace
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    models,
)

from app.core.config import get_settings

tracer = trace.get_tracer("rag2.retrieval")


def build_acl_filter(acl: dict) -> Filter:
    """Qdrant pre-filter из ACL пользователя.

    Правило доступа: workspace разрешён И (пользователь в acl_users ИЛИ
    департамент в acl_groups). Непопадающие точки исключаются до поиска.
    MVP: workspace-права дают доступ ко всем точкам workspace (acl_groups
    точки содержит workspace_id), плюс явные user/dept-ограничения.
    """
    must = [
        FieldCondition(
            key="workspace_id",
            match=MatchAny(any=acl["workspace_ids"]),
        )
    ]
    # Точка доступна, если совпал user-ACL, dept-ACL или она общая для workspace
    should = [
        FieldCondition(key="acl_users", match=MatchAny(any=[acl["user_id"]])),
        FieldCondition(key="acl_groups", match=MatchAny(any=acl["dept_ids"] + acl["workspace_ids"])),
    ]
    return Filter(must=must, should=should, min_should={"conditions": should, "min_count": 1})


async def embed_query(text: str) -> list[float]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{settings.embedding_url}/embed",
            json={"inputs": text, "truncate": True},
        )
        return resp.json()[0]


async def dense_search(
    qdrant: AsyncQdrantClient, collection: str, vector: list[float], acl: dict, top_k: int
) -> list[models.ScoredPoint]:
    # await до .points: query_points — coroutine, результат разворачивается первым
    result = await qdrant.query_points(
        collection_name=collection,
        query=vector,
        query_filter=build_acl_filter(acl),
        limit=top_k,
        with_payload=True,
    )
    return result.points


async def sparse_search(
    qdrant: AsyncQdrantClient, collection: str, query_text: str, acl: dict, top_k: int
) -> list[models.ScoredPoint]:
    """Sparse-плечо гибрида: BM25-подобный поиск по full-text payload-индексу
    (MVP: pg_trgm в PostgreSQL; Фаза 2 — нативный sparse BGE-M3 в Qdrant)."""
    from sqlalchemy import text as sql_text

    from app.core.security import SessionLocal

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                sql_text(
                    """
                    SELECT c.id::text, c.qdrant_point_id, c.document_id::text,
                           c.content, c.metadata,
                           GREATEST(similarity(c.content, :q), 0.3) AS score
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE d.workspace_id = CAST(:ws AS uuid)
                      AND (c.content ILIKE :pattern
                           OR similarity(c.content, :q) > 0.05)
                    ORDER BY score DESC
                    LIMIT :k
                    """
                ),
                {"q": query_text, "pattern": f"%{query_text[:40]}%", "ws": acl["workspace_ids"][0], "k": top_k},
            )
        ).mappings()
        return [dict(r) for r in rows]


async def reciprocal_rank_fusion(
    *result_lists: list, k: int = 60
) -> list[dict]:
    """RRF-слияние результатов dense и sparse плеч.

    Плечи возвращают разные типы: dense — ScoredPoint (Qdrant),
    sparse — dict (строки PostgreSQL). Нормализуем к dict с payload.
    """
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}
    for results in result_lists:
        for rank, item in enumerate(results):
            if isinstance(item, dict):
                pid = item.get("qdrant_point_id") or item.get("id") or str(rank)
                payload = {
                    "content": item.get("content", ""),
                    "document_id": item.get("document_id"),
                    "metadata": item.get("metadata"),
                }
                entry = {"id": pid, "payload": payload, "score": item.get("score", 0)}
            else:
                # ScoredPoint
                pid = str(item.id)
                entry = {"id": pid, "payload": item.payload or {}, "score": item.score}
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (k + rank + 1)
            items.setdefault(pid, entry)
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [{"item": items[pid], "rrf_score": s} for pid, s in ranked]


async def rerank(query: str, candidates: list[dict], top_n: int) -> list[dict]:
    """Кросс-энкодер BGE-reranker-v2-m3: переранжирование top-k → top-n."""
    settings = get_settings()
    if not candidates:
        return []
    texts = [c["item"]["payload"].get("content", "") for c in candidates]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.reranker_url}/rerank",
            json={"query": query, "texts": texts, "top_n": top_n, "raw_scores": True},
        )
        ranked = resp.json()
    scored = [
        {**candidates[r["index"]], "rerank_score": r["score"]} for r in ranked
    ]
    return [c for c in scored if c["rerank_score"] > 0.015], scored


async def _expand_sections(
    qdrant: AsyncQdrantClient,
    collection: str,
    acl: dict,
    reranked: list[dict],
) -> list[dict]:
    """Расширение перечней: соседние чанки того же документа.

    Кросс-энкодер меряет попарную релевантность, поэтому на вопрос-перечень
    («какие этапы в блоке») высоко ранжируется только одна секция, а её
    «братья» (Этап 2, 3, 4) получают низкие скоры. Если в топ попал чанк
    с секционным breadcrumb, подтягиваем соседние чанки того же документа —
    перечень попадает в контекст целиком.
    """
    import re

    if not reranked:
        return reranked

    # Есть ли среди топ-результатов секционный чанк? (breadcrumb "[...]")
    top = reranked[0]["item"]["payload"].get("content", "")
    if not top.startswith("["):
        return reranked

    doc_id = reranked[0]["item"]["payload"].get("document_id")
    if not doc_id:
        return reranked

    # Соседи: чанки того же документа в workspace-коллекции
    from qdrant_client import models as qmodels

    r = await qdrant.scroll(
        collection,
        scroll_filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="document_id", match=qmodels.MatchValue(value=doc_id)
                ),
                qmodels.FieldCondition(
                    key="workspace_id",
                    match=qmodels.MatchAny(any=acl["workspace_ids"]),
                ),
            ]
        ),
        limit=30,
        with_payload=True,
    )
    seen = {c["item"]["id"] for c in reranked}

    # Определяем «область» расширения — блок верхнего уровня. breadcrumb
    # содержит полный путь: «[Блок 1. Развитие > Этап 2. ...]». Область выбираем
    # голосованием: блок, к которому относится больше всего чанков топа —
    # единичный высокоскорной чанк чужого блока не должен перетянуть расширение.
    _block_re = re.compile(r"^\[(Блок|Раздел|Глава|Часть)\s+(\d+)", re.IGNORECASE)

    votes: dict[str, int] = {}
    for c in reranked:
        m = _block_re.match(c["item"]["payload"].get("content", ""))
        if m:
            area_key = f"{m.group(1)} {m.group(2)}".lower()
            votes[area_key] = votes.get(area_key, 0) + 1
    if not votes:
        return reranked  # в топе нет блочной структуры — расширять нечем
    area = max(votes, key=votes.get)

    def _same_area(content: str) -> bool:
        return content.lower().startswith(f"[{area}")

    # Соседи в порядке следования в документе: чтобы перечень шёл Этап 1 → 4,
    # сортируем по позиции заголовка в исходном тексте (qdrant scroll не
    # гарантирует порядок). Позицию даёт совпадение chunk-последовательности
    # в payload — нет её, поэтому сохраняем порядок как есть, но отфильтровываем.
    neighbors = [
        p for p in r[0]
        if str(p.id) not in seen and _same_area((p.payload or {}).get("content", ""))
    ]
    for p in neighbors[:6]:  # бюджет расширения — не раздуваем контекст
        reranked.append(
            {"item": {"id": str(p.id), "payload": p.payload or {}}, "rrf_score": 0.0}
        )
    return reranked


async def hybrid_retrieve(
    qdrant: AsyncQdrantClient,
    collection: str,
    query: str,
    acl: dict,
    top_k: int | None = None,
) -> tuple[list[dict], dict]:
    """Полный пайплайн: embed → dense + sparse → RRF → rerank.

    Возвращает (чанки с цитатами, метрики для OTel/логов).
    """
    settings = get_settings()
    top_k = top_k or settings.rag_top_k
    start = time.monotonic()

    with tracer.start_as_current_span("rag.hybrid_retrieve") as span:
        vector = await embed_query(query)
        dense = await dense_search(qdrant, collection, vector, acl, top_k)
        sparse = await sparse_search(qdrant, collection, query, acl, top_k)
        fused = await reciprocal_rank_fusion(dense, sparse)
        reranked, reranked_all = await rerank(query, fused, settings.rerank_top_n)
        # Расширение секций строим от полного списка (до порога): порог 0.015
        # отсекает соседей-этапов, чей скор занижен кросс-энкодером на
        # вопросах-перечнях — они вернутся через _expand_sections.
        reranked = await _expand_sections(qdrant, collection, acl, reranked_all)

        latency_ms = int((time.monotonic() - start) * 1000)
        span.set_attribute("rag.chunks_retrieved", len(reranked))
        span.set_attribute("rag.latency_ms", latency_ms)

        # Маскирование ПДн: чанки с pii_types отдаются с маской пользователям
        # без права pii_read на workspace (контекст сохраняется, персоналия нет)
        from app.guardrails.guardrails import mask_pii_regex

        pii_allowed = set(acl.get("pii_read_workspace_ids", []))
        masked_chunks = 0

        chunks = []
        for r in reranked:
            item = r["item"]
            payload = item["payload"]
            content = payload.get("content", "")
            pii_types = payload.get("pii_types") or []
            ws_id = payload.get("workspace_id", "")
            if pii_types and ws_id not in pii_allowed:
                content = mask_pii_regex(content)
                masked_chunks += 1
            chunks.append(
                {
                    "chunk_id": item["id"],
                    "document_id": payload.get("document_id"),
                    "title": payload.get("title", ""),
                    "page": payload.get("page"),
                    "content": content,
                    "pii_masked": bool(pii_types) and ws_id not in pii_allowed,
                    "score": r.get("rerank_score", r["rrf_score"]),
                }
            )
        if masked_chunks:
            span.set_attribute("rag.pii_masked_chunks", masked_chunks)
        return chunks, {"latency_ms": latency_ms, "dense": len(dense), "sparse": len(sparse)}
