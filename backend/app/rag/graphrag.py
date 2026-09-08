"""GraphRAG: экстракция сущностей и связей из чанков → graph_nodes/graph_edges.

MVP-экстрактор: LLM выделяет сущности и связи в JSON по строго заданной схеме.
Вызывается из ingestion после чанкинга. Ошибки экстракции не ломают
индексацию — документ всё равно проиндексирован в Qdrant.
"""
import json

from opentelemetry import trace

tracer = trace.get_tracer("rag2.graphrag")

# Лимит чанков на документ: экстракция — LLM-вызов, дорого на больших файлах
MAX_CHUNKS_PER_DOC = 30

_EXTRACT_PROMPT = (
    "Ты извлекаешь сущности и связи из фрагмента корпоративного документа.\n"
    "Типы сущностей: person, department, document, project, system, position, other.\n"
    "Связи — только фактические из текста: works_in, manages, reports_to, "
    "member_of, regulates, uses, responsible_for.\n\n"
    "Фрагмент:\n{chunk}\n\n"
    "Ответь СТРОГО в JSON без пояснений:\n"
    '{{"entities": [{{"name": "...", "type": "...", "description": "..."}}], '
    '"relations": [{{"src": "...", "relation": "...", "dst": "..."}}]}}\n'
    "Имена сущности — в нормализованной форме (как в тексте, без сокращений). "
    "Если сущностей нет — верни пустые списки."
)


async def extract_and_store(
    workspace_id: str, doc_title: str, chunks: list[str]
) -> tuple[int, int]:
    """Экстракция графа из чанков документа. Возвращает (nodes, edges).

    Мерж по имени узла: одна и та же сущность из разных чанков/документов
    не дублируется (узлы уникальны в рамках workspace+name).
    """
    import uuid as _uuid

    from sqlalchemy import select

    from app.core.llm_provider import llm_chat
    from app.core.security import SessionLocal
    from app.db.models import GraphEdge, GraphNode

    ws = _uuid.UUID(workspace_id)
    nodes_by_name: dict[str, GraphNode] = {}
    edges: list[tuple[str, str, str]] = []  # (src, relation, dst)

    for chunk in chunks[:MAX_CHUNKS_PER_DOC]:
        try:
            # При внешнем LLM (LLM_PROVIDER=yandex) чанк покидает контур —
            # ПДн маскируются до отправки. При local (vLLM on-premise) чанк
            # не выходит за периметр и уходит как есть: экстракции нужны
            # роли/связи, а маска снижает качество разбора имён
            from app.guardrails.guardrails import mask_pii_regex

            from app.core.config import get_settings

            chunk_text = chunk[:3000]
            if get_settings().llm_provider != "local":
                chunk_text = mask_pii_regex(chunk_text)

            content = await llm_chat(
                [{"role": "user", "content": _EXTRACT_PROMPT.format(chunk=chunk_text)}],
                max_tokens=800,
            )
            start, end = content.index("{"), content.rindex("}") + 1
            parsed = json.loads(content[start:end])
        except Exception:  # noqa: BLE001 — чанк без графа не фатален
            continue
        for e in parsed.get("entities", []):
            name = str(e.get("name", "")).strip()
            if not name:
                continue
            # Описание узла маскируется всегда, независимо от провайдера:
            # description попадает в контекст чата, и если LLM скопировал
            # ПДн из чанка, они утекли бы в ответ пользователю без pii_read
            from app.guardrails.guardrails import mask_pii_regex as _m

            desc = _m(str(e.get("description", "")))[:1000] or None
            if name not in nodes_by_name:
                nodes_by_name[name] = GraphNode(
                    workspace_id=ws,
                    name=name[:255],
                    node_type=str(e.get("type", "other"))[:50],
                    description=desc,
                )
            elif desc and not nodes_by_name[name].description:
                nodes_by_name[name].description = desc
        for r in parsed.get("relations", []):
            src, dst, rel = (
                str(r.get("src", "")).strip(),
                str(r.get("relation", "")).strip(),
                str(r.get("dst", "")).strip(),
            )
            if src and dst and rel:
                edges.append((src, rel, dst))

    if not nodes_by_name:
        return 0, 0

    async with SessionLocal() as db:
        # Мерж с уже существующими узлами workspace
        existing = (
            await db.execute(
                select(GraphNode).where(
                    GraphNode.workspace_id == ws,
                    GraphNode.name.in_(list(nodes_by_name)),
                )
            )
        ).scalars()
        for node in existing:
            nodes_by_name[node.name] = node

        db.add_all(n for n in nodes_by_name.values() if n.id is None)
        await db.flush()

        # Связи: пропускаем дубли (src, rel, dst) с учётом уже записанных
        src_ids = (
            await db.execute(
                select(GraphEdge.src_id, GraphEdge.dst_id, GraphEdge.relation).where(
                    GraphEdge.src_id.in_([n.id for n in nodes_by_name.values()])
                )
            )
        ).all()
        seen = {(s, r, d) for s, d, r in src_ids}

        new_edges = []
        # Сначала авто-создание отсутствующих узлов (LM ссылается на
        # сущности вне entities), flush — чтобы у всех появился id
        for src, rel, dst in edges:
            for name in (src, dst):
                if name and name not in nodes_by_name:
                    node = GraphNode(workspace_id=ws, name=name[:255], node_type="other")
                    db.add(node)
                    nodes_by_name[name] = node
        await db.flush()

        for src, rel, dst in edges:
            s, d = nodes_by_name.get(src), nodes_by_name.get(dst)
            if not s or not d or s.id is None or d.id is None:
                continue
            if (s.id, d.id, rel) in seen:
                continue
            seen.add((s.id, d.id, rel))
            new_edges.append(
                GraphEdge(src_id=s.id, dst_id=d.id, relation=rel[:100])
            )
        db.add_all(new_edges)
        await db.commit()

        return len(nodes_by_name), len(new_edges)
