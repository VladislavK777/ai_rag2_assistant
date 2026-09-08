"""Администрирование: workspace, права (RBAC), пользователи."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, get_db, require_admin
from app.db.audit import audit
from app.db.models import Permission, User, Workspace

# RBAC-исключение: GET /workspaces — селектор для ВСЕХ пользователей
# (возвращает только workspace из своего ACL); остальные операции — admin.
router = APIRouter(dependencies=[Depends(require_admin)])
workspaces_router = APIRouter()


class WorkspaceCreate(BaseModel):
    name: str
    description: str | None = None


class PermissionGrant(BaseModel):
    workspace_id: str
    user_id: str | None = None
    department_id: str | None = None
    level: str  # read | write | admin


@workspaces_router.get("/workspaces")
async def list_workspaces(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список workspace, доступных пользователю (для селектора в UI).

    admin видит все; остальные — только свои по Permission.
    """
    from app.core.security import get_user_acl

    acl = await get_user_acl(db, user)
    rows = (
        await db.execute(select(Workspace).where(Workspace.id.in_(
            uuid.UUID(w) for w in acl["workspace_ids"]
        )))
    ).scalars()
    return [{"id": str(w.id), "name": w.name} for w in rows]


@router.post("/workspaces", status_code=201)
async def create_workspace(
    body: WorkspaceCreate,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ws = Workspace(name=body.name, description=body.description, owner_id=admin.id)
    db.add(ws)
    await db.commit()

    # Владелец получает полный доступ + Qdrant-коллекция создаётся при первом ingestion
    db.add(Permission(user_id=admin.id, workspace_id=ws.id, level="admin"))
    await db.commit()
    await audit(db, user_id=str(admin.id), action="workspace_create", resource=str(ws.id))
    return {"id": str(ws.id), "name": ws.name}


@router.post("/permissions")
async def grant_permission(
    body: PermissionGrant,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Выдача прав: user-based или department-based.

    Инвалидация ACL-кэша выполняется через Redis DEL acl:{user_id}.
    """
    if not body.user_id and not body.department_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Нужен user_id или department_id")

    perm = Permission(
        user_id=uuid.UUID(body.user_id) if body.user_id else None,
        department_id=uuid.UUID(body.department_id) if body.department_id else None,
        workspace_id=uuid.UUID(body.workspace_id),
        level=body.level,
    )
    db.add(perm)
    await db.commit()
    await audit(db, user_id=str(admin.id), action="permission_grant",
                resource=f"ws:{body.workspace_id} level:{body.level}")

    from app.core.config import get_settings
    import redis.asyncio as aioredis

    redis = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    if body.user_id:
        await redis.delete(f"acl:{body.user_id}")
    else:
        # Инвалидация кэша всех пользователей департамента
        users = (await db.execute(select(User).where(User.department_id == body.department_id))).scalars()
        for u in users:
            await redis.delete(f"acl:{u.id}")
    await redis.aclose()
    return {"granted": True}


@router.get("/audit")
async def get_audit_log(
    limit: int = 100,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Просмотр аудит-лога (роль auditor/admin)."""
    from app.db.models import AuditLog

    rows = (
        await db.execute(
            select(AuditLog).order_by(AuditLog.ts.desc()).limit(min(limit, 1000))
        )
    ).scalars()
    return [
        {
            "ts": r.ts.isoformat(),
            "user_id": str(r.user_id) if r.user_id else None,
            "action": r.action,
            "decision": r.decision,
            "resource": r.resource,
            "trace_id": r.trace_id,
        }
        for r in rows
    ]
