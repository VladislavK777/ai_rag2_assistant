import asyncio
import re

import httpx
from qdrant_client import AsyncQdrantClient, models

from app.core.config import get_settings


def extract_text(raw: bytes, mime_type: str) -> str:
    """Извлечение текста из документа (MVP: pypdf/docx/txt).

    docx: заголовки (стили Heading 1-6) помечаются префиксом "#"*level —
    структурный чанкер использует их как границы секции.
    """
    import io

    if mime_type == "application/pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        return "\n\n".join(
            f"[стр. {i + 1}]\n{page.extract_text() or ''}"
            for i, page in enumerate(reader.pages)
        )
    if mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        import docx
        from docx.oxml.ns import qn

        d = docx.Document(io.BytesIO(raw))

        def _table_md(tbl) -> str:
            """Таблица → markdown. Заголовок из первой строки, разделитель,
            данные далее. Пустые ячейки сохраняются — не теряются значения."""
            rows = []
            for r in tbl.rows:
                cells = [" ".join(c.text.split()) for c in r.cells]
                rows.append("| " + " | ".join(cells) + " |")
            if len(rows) > 1:
                sep = "|" + "---|" * len(tbl.rows[0].cells)
                rows.insert(1, sep)
            return "\n".join(rows)

        # Обход тела в порядке следования: paragraphs + tables вперемешку.
        # d.paragraphs пропускает w:tbl — данные таблиц терялись полностью.
        parts: list[str] = []
        for child in d.element.body.iterchildren():
            if child.tag == qn("w:p"):
                p = docx.text.paragraph.Paragraph(child, d)
                if not p.text.strip():
                    continue
                style = (p.style.name or "").lower()
                m = re.match(r"heading (\d)", style)
                if m:
                    level = min(int(m.group(1)), 6)
                    parts.append(f"{'#' * level} {p.text.strip()}")
                else:
                    parts.append(p.text.strip())
            elif child.tag == qn("w:tbl"):
                parts.append(_table_md(docx.table.Table(child, d)))
        return "\n\n".join(parts)
    return raw.decode("utf-8", errors="ignore")


# Универсальные сигналы заголовка (не привязаны к конкретным словам типа "Этап"):
# 1) markdown-заголовки из docx-стилей: "# ...", "## ..."
# 2) нумерация любой вложенности: "1.", "1.2", "1.2.3", "Глава 2", "Часть 3"
# 3) эвристика: короткая строка без завершающей точки
_HEADING_MD = re.compile(r"^#{1,6}\s+\S")
_HEADING_NUM = re.compile(r"^(?:\d+(?:\.\d+)*[.)]?\s+\S|(?:глава|часть|раздел|section)\s+\d+)", re.IGNORECASE)


def _is_heading(line: str) -> bool:
    if _HEADING_MD.match(line) or _HEADING_NUM.match(line):
        return True
    # Эвристика: короткая строка без точки в конце — вероятный заголовок
    stripped = line.strip()
    return 0 < len(stripped) <= 80 and not stripped.endswith((".", ":", ";", ","))


def chunk_text(text: str, size: int = 512, overlap: int = 100) -> list[str]:
    """Структурный чанкинг с фолбэком на скользящее окно.

    1. Документ делится на секции по сигналам заголовков (универсальным:
       стили/нумерация/эвристика — работает для любой структуры, не только
       «Блок/Этап»). Секция сохраняет breadcrumb-префикс — ПОЛНЫЙ путь от
       родительского заголовка: «[Блок 1. Развитие > Этап 2. Техники]».
    2. Большая секция режется по абзацам (size символов, overlap).
    3. Нет ни одного заголовка — обычное окно 512/100 по всему тексту.

    Полный путь в breadcrumb решает три задачи: эмбеддинг и reranker видят
    родительский контекст (два «Этап 1» разных блоков различимы), LM знает
    из какого раздела кусок, а retrieval может фильтровать соседей по блоку.
    """
    lines = text.split("\n")
    # Разбивка на секции: заголовок + тело
    sections: list[tuple[str, list[str]]] = []  # (heading, body_lines)
    cur_head, cur_body = "", []
    for line in lines:
        if line.strip() and _is_heading(line):
            if cur_head or cur_body:
                sections.append((cur_head, cur_body))
            cur_head, cur_body = line.strip(), []
        else:
            cur_body.append(line)
    if cur_head or cur_body:
        sections.append((cur_head, cur_body))

    # Ни одного заголовка во всём документе — фолбэк: скользящее окно
    if len(sections) == 1 and not sections[0][0]:
        return _chunk_window(text, size, overlap)

    # Иерархия: родитель — ближайший заголовок более высокого уровня.
    # Уровень: markdown '#' (1-6); разделы верхнего уровня «Блок/Глава/Часть/
    # Раздел N» → 1; нумерация — число точек+2 («1.2.3» → 4, «Этап 1» → 2);
    # эвристический заголовок — нижний уровень (приложение к текущему пути).
    _TOP_LEVEL_RE = re.compile(
        r"^(?:блок|глава|часть|раздел|section)\s+\d+", re.IGNORECASE
    )

    def _level(heading: str) -> int:
        if heading.startswith("#"):
            return len(heading) - len(heading.lstrip("#"))
        if _TOP_LEVEL_RE.match(heading):
            return 1
        m = re.match(r"^(\d+(?:\.\d+)*)", heading)
        return m.group(1).count(".") + 2 if m else 9

    chunks: list[str] = []
    path: list[tuple[int, str]] = []  # стек (уровень, текст заголовка)
    for heading, body in sections:
        if heading:
            lvl = _level(heading)
            title = heading.lstrip("#").strip()
            while path and path[-1][0] >= lvl:
                path.pop()
            path.append((lvl, title))
        breadcrumb = " > ".join(t for _, t in path)
        body_text = "\n\n".join(
            p.strip() for p in "\n".join(body).split("\n\n") if p.strip()
        )
        if not body_text and heading:
            body_text = title
        if not body_text:
            continue
        # Breadcrumb: каждый кусок секции начинается с полного пути секции
        prefix = f"[{breadcrumb}]\n" if breadcrumb else ""
        # Таблицы (markdown-блоки) — атомарные абзацы: окно их не режет,
        # но при превышении size таблица становится отдельным чанком,
        # чтобы строки таблицы не склеивались с чужим текстом
        pieces = _chunk_window(body_text, size, overlap)
        chunks.extend(prefix + p for p in pieces)
    return [c for c in chunks if len(c) > 50]


def _chunk_window(text: str, size: int, overlap: int) -> list[str]:
    """Скользящее окно по абзацам (прежняя логика, вынесена как фолбэк).

    Абзацы, начинающиеся с '|' (markdown-таблица), атомарны: не режутся
    и не склеиваются с другим текстом — строка таблицы без шапки бессмысленна.
    """
    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    current = ""
    for para in paragraphs:
        is_table = para.strip().startswith("|")
        too_big = len(current) + len(para) > size and current
        if too_big and not is_table or too_big and is_table:
            chunks.append(current.strip())
            current = para if is_table else current[-overlap:] + "\n\n" + para
        else:
            current = (current + "\n\n" + para).strip() if current else para
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if len(c) > 50]


async def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Эмбеддинг батчами через TEI (BGE-M3)."""
    settings = get_settings()
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=120) as client:
        for i in range(0, len(texts), batch_size):
            resp = await client.post(
                f"{settings.embedding_url}/embed",
                json={"inputs": texts[i : i + batch_size], "truncate": True},
            )
            resp.raise_for_status()
            vectors.extend(resp.json())
    return vectors


async def upsert_chunks(
    collection: str,
    point_ids: list[str],
    chunks: list[str],
    payload_base: dict,
    acl_payload: dict | None = None,
) -> None:
    """Upsert в Qdrant: создание коллекции при необходимости + payload с ACL.

    ACL-метки наследуются от workspace-прав: acl_groups = департаменты с
    доступом, acl_users = явные пользователи. Передаются в payload_base.
    """
    settings = get_settings()
    if not chunks or not point_ids:
        # Пустой Batch Qdrant отклоняет с невнятным 400 — даём понятную причину
        raise ValueError(
            "Документ не дал ни одного чанка: текст короче минимального "
            "размера чанка (>50 символов) или не извлекся."
        )
    vectors = await embed_batch(chunks)

    # AsyncQdrantClient не поддерживает async with — используем явно с закрытием
    qdrant = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    try:
        if not await qdrant.collection_exists(collection):
            await qdrant.create_collection(
                collection_name=collection,
                vectors_config=models.VectorParams(
                    size=1024, distance=models.Distance.COSINE
                ),
                hnsw_config=models.HnswConfigDiff(m=16, ef_construct=128),
            )
        payload = {**payload_base, **(acl_payload or {})}
        # Текст чанка обязателен в payload: retriever собирает контекст
        # для LLM из payload.content (без него dense-плечо даёт пустые чанки)
        payloads = [{**payload, "content": chunk} for chunk in chunks]
        await qdrant.upsert(
            collection_name=collection,
            points=models.Batch(
                ids=point_ids,
                vectors=vectors,
                payloads=payloads,
            ),
        )
    finally:
        await qdrant.close()
