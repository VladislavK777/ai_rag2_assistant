"""Resource-server безопасность: JWT Keycloak → JIT-тень пользователя → RBAC/ACL.

Идентичность и пароли — в Keycloak (SSO/LDAP). В PG — только теневая запись
(кеш идентичности) для FK от history/документов/аудита и прав (Permission).
"""
import json
import uuid
from typing import Any

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.keycloak import (
    bearer,
    claims_to_dto,
    decode_token,
    user_id_from_employee_no,
)
from app.db.models import Department, Permission, User
from app.db.session import make_engine, make_session_factory

_settings = get_settings()
engine = make_engine()
SessionLocal = make_session_factory(engine)

ACL_CACHE_TTL = 60


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


async def _jwc_user(db: AsyncSession, payload: dict[str, Any]) -> User:
    """JIT-провижининг теневой записи из JWT-claims (create-or-update).

    department_id подтягивается по dept_code при каждом запросе —
    перевод сотрудника в другой отдел подхватится на следующем токене.
    """
    dto = claims_to_dto(payload)
    user_id = user_id_from_employee_no(dto["employee_no"])

    user = await db.get(User, user_id)
    dept_id = None
    if dto["dept_code"]:
        dept = (
            await db.execute(select(Department).where(Department.code == dto["dept_code"]))
        ).scalar_one_or_none()
        if dept is None:
            # Департамент ещё не загружен справочником — создаём заглушку с кодом,
            # имя придёт при обновлении справочника.
            dept = Department(code=dto["dept_code"], name=dto["dept_code"])
            db.add(dept)
            await db.flush()
        dept_id = dept.id

    if user is None:
        user = User(id=user_id, employee_no=dto["employee_no"], email=dto["email"],
                    full_name=dto["full_name"], department_id=dept_id, is_active=True)
        db.add(user)
    else:
        user.email = dto["email"] or user.email
        user.full_name = dto["full_name"] or user.full_name
        user.department_id = dept_id
        user.is_active = True
    await db.commit()
    return user


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Валидация JWT (JWKS Keycloak) + JIT-тень. Роли берутся из claims."""
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется авторизация")
    try:
        payload = await decode_token(credentials.credentials)
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Невалидный токен")

    user = await _jwc_user(db, payload)

    dto = claims_to_dto(payload)
    if not dto["employee"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет системной роли")
    return user


async def get_identity(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> tuple[User, dict]:
    """(пользователь, claims) — для мест, где нужен доступ к роли из токена."""
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется авторизация")
    try:
        payload = await decode_token(credentials.credentials)
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Невалидный токен")
    user = await _jwc_user(db, payload)
    return user, claims_to_dto(payload)


def user_is_admin(user: User, payload_roles: list[str] | None = None) -> bool:
    """Проверка роли — по токену, не по БД. Оставлена как хелпер для флагов."""
    return bool(payload_roles and "ADMIN" in payload_roles)


async def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Требует роль ADMIN из JWT (realm- или client-роль Keycloak)."""
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется авторизация")
    payload = await decode_token(credentials.credentials)
    dto = claims_to_dto(payload)
    if not dto["is_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Требуются права администратора")
    return await _jwc_user(db, payload)


async def get_user_acl(db: AsyncSession, user: User, *, is_admin: bool) -> dict:
    """Собирает ACL пользователя: workspace_ids + dept_ids (с наследованием).

    Роль ADMIN приходит из токена, поэтому ACL-флаг пробрасывается
    вызывающим кодом (routes), кэш ключуется с учётом роли.
    Результат кэшируется в Redis (TTL 60c).
    """
    cache_key = f"acl:{user.id}:{'admin' if is_admin else 'employee'}"
    redis = aioredis.from_url(_settings.redis_url, decode_responses=True)
    try:
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)
    finally:
        await redis.aclose()

    # Явные права пользователя
    perms = (await db.execute(select(Permission).where(Permission.user_id == user.id))).scalars()
    workspace_ids = {p.workspace_id for p in perms}

    # Права по департаментам (включая родительские департаменты)
    dept_ids: set[uuid.UUID] = set()
    if user.department_id:
        chain = [user.department_id]
        current = user.department_id
        while current:
            parent = await db.get(Department, current)
            if parent and parent.parent_id and parent.parent_id not in dept_ids:
                chain.append(parent.parent_id)
            current = parent.parent_id if parent else None
        dept_ids = set(chain)

        dept_perms = (
            await db.execute(
                select(Permission).where(Permission.department_id.in_(dept_ids))
            )
        ).scalars()
        workspace_ids |= {p.workspace_id for p in dept_perms}

    # Право на просмотр ПДн: уровень pii_read на workspace.
    # admin-роль видит ПДн везде без отдельного права.
    pii_ws = set()
    if not is_admin:
        pii_perms = (
            await db.execute(
                select(Permission.level, Permission.workspace_id).where(
                    (Permission.user_id == user.id)
                    | (Permission.department_id.in_(dept_ids) if dept_ids else False)
                )
            )
        ).all()
        pii_ws = {w for lvl, w in pii_perms if lvl == "pii_read"}

    acl = {
        "user_id": str(user.id),
        "dept_ids": [str(d) for d in dept_ids],
        "workspace_ids": [str(w) for w in workspace_ids],
        # workspace-ы, где пользователь видит ПДн без маскирования
        "pii_read_workspace_ids": (
            [str(w) for w in workspace_ids] if is_admin else [str(w) for w in pii_ws]
        ),
        "is_admin": is_admin,
    }

    redis = aioredis.from_url(_settings.redis_url, decode_responses=True)
    try:
        await redis.setex(cache_key, ACL_CACHE_TTL, json.dumps(acl))
    finally:
        await redis.aclose()
    return acl


async def check_workspace_access(
    db: AsyncSession, user: User, is_admin: bool,
    workspace_id: uuid.UUID, level: str = "read",
) -> bool:
    """Прямая проверка доступа к workspace (для admin-операций и загрузки)."""
    if is_admin:
        return True

    level_order = {"read": 0, "write": 1, "admin": 2}
    needed = level_order[level]

    perm = (
        await db.execute(
            select(Permission).where(
                (Permission.user_id == user.id)
                | (Permission.department_id == user.department_id
                   if user.department_id else False)
            )
        )
    ).scalars().all()
    return any(level_order[p.level] >= needed for p in perm)