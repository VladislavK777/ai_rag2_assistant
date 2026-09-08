"""Чат: SSE-стриминг ответов агента с учётом RBAC."""
import json
import time

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from prometheus_client import Counter
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, get_db, get_user_acl
from app.db.audit import audit
from app.db.models import Message, Session as ChatSession, User
from app.agents.graph import run_agent

router = APIRouter()

chat_requests_total = Counter(
    "rag2_chat_requests_total", "Chat stream requests", ["status"]
)


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    workspace_id: str


class ChatEvent(BaseModel):
    type: str  # status | answer | citations | done | error
    data: str


async def _get_or_create_session(
    db: AsyncSession, user: User, session_id: str | None, title: str
) -> ChatSession:
    if session_id:
        session = await db.get(ChatSession, __import__("uuid").UUID(session_id))
        if session and session.user_id == user.id:
            return session
    session = ChatSession(user_id=user.id, title=title[:255])
    db.add(session)
    await db.commit()
    return session


@router.post("/stream")
async def chat_stream(
    body: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """SSE-эндпоинт: статус этапов + стриминг ответа.

    Безопасность: ACL пользователя собирается здесь и передаётся в граф;
    все tool-вызовы фильтруются по нему (RBAC pre-filter в Qdrant).
    """
    acl = await get_user_acl(db, user)
    if body.workspace_id not in acl["workspace_ids"]:
        await audit(
            db, user_id=str(user.id), action="chat",
            decision="deny", resource=f"workspace:{body.workspace_id}",
        )
        event = ChatEvent(type="error", data="Нет доступа к указанному рабочему пространству.")
        return StreamingResponse(
            _sse([event.model_dump()]), media_type="text/event-stream"
        )

    session = await _get_or_create_session(db, user, body.session_id, body.message)
    history_rows = (
        await db.execute(
            select(Message)
            .where(Message.session_id == session.id)
            .order_by(Message.created_at.desc())
            .limit(10)
        )
    ).scalars()
    history = [{"role": m.role, "content": m.content} for m in reversed(history_rows.all())]

    db.add(Message(session_id=session.id, role="user", content=body.message))
    await db.commit()

    async def generate():
        full_answer, citations, trace_id = "", [], ""
        first_answer_at = None
        started = time.monotonic()
        try:
            async for event in run_agent(body.message, acl, body.workspace_id, history):
                if event["type"] == "answer":
                    full_answer = event["data"]
                    # TTFT: от старта запроса до первого события с ответом
                    if first_answer_at is None:
                        from app.core.metrics import rag2_ttft_seconds

                        first_answer_at = time.monotonic()
                        rag2_ttft_seconds.observe(first_answer_at - started)
                elif event["type"] == "citations":
                    citations = event["data"]
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception:  # noqa: BLE001 — не роняем SSE-поток
            chat_requests_total.labels(status="error").inc()
            yield f"data: {json.dumps({'type': 'error', 'data': 'Внутренняя ошибка. Попробуйте позже.'}, ensure_ascii=False)}\n\n"
            raise
        chat_requests_total.labels(status="ok").inc()

        # Сохранение ответа и цитат
        db.add(
            Message(
                session_id=session.id,
                role="assistant",
                content=full_answer,
                citations=citations,
            )
        )
        await db.commit()
        await audit(db, user_id=str(user.id), action="chat", decision="allow")

    return StreamingResponse(generate(), media_type="text/event-stream")


def _sse(events: list[dict]):
    import json

    for e in events:
        yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"


@router.get("/sessions")
async def list_sessions(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    rows = (
        await db.execute(
            select(ChatSession)
            .where(ChatSession.user_id == user.id)
            .order_by(ChatSession.updated_at.desc())
            .limit(50)
        )
    ).scalars()
    return [{"id": str(s.id), "title": s.title} for s in rows]


@router.get("/sessions/{session_id}/messages")
async def get_messages(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import uuid as _uuid

    session = await db.get(ChatSession, _uuid.UUID(session_id))
    if session is None or session.user_id != user.id:
        return []
    rows = (
        await db.execute(
            select(Message)
            .where(Message.session_id == session.id)
            .order_by(Message.created_at)
        )
    ).scalars()
    return [
        {
            "role": m.role,
            "content": m.content,
            "citations": m.citations or [],
            "created_at": m.created_at.isoformat(),
        }
        for m in rows
    ]
