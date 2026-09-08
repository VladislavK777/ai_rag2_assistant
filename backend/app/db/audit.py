"""Аудит-лог: append-only запись событий ИБ."""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.observability import current_trace_id
from app.db.models import AuditLog

logger = logging.getLogger("rag2.audit")


async def audit(
    db: AsyncSession,
    *,
    user_id: str | None,
    action: str,
    decision: str = "allow",
    resource: str | None = None,
) -> None:
    """Запись в аудит. Не бросает исключений — сбой аудита не ломает бизнес-флоу,
    но логируется с уровнем ERROR для alerting."""
    try:
        db.add(
            AuditLog(
                user_id=user_id,
                action=action,
                decision=decision,
                resource=resource,
                trace_id=current_trace_id(),
            )
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("audit write failed: action=%s user=%s", action, user_id)
