"""Безопасность: JWT, хэширование паролей, RBAC-зависимости FastAPI."""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Department, Permission, User
from app.db.session import make_engine, make_session_factory

_settings = get_settings()
engine = make_engine()
SessionLocal = make_session_factory(engine)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer = HTTPBearer(auto_error=False)


def hash_password(raw: str) -> str:
    return pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    return pwd_context.verify(raw, hashed)


def create_access_token(user_id: uuid.UUID, roles: list[str]) -> str:
    payload = {
        "sub": str(user_id),
        "roles": roles,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=_settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, _settings.jwt_secret_key, algorithm=_settings.jwt_algorithm)


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется авторизация")
    try:
        payload = jwt.decode(
            credentials.credentials,
            _settings.jwt_secret_key,
            algorithms=[_settings.jwt_algorithm],
        )
        user_id = uuid.UUID(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Невалидный токен")

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Пользователь неактивен")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if not any(r.name == "admin" for r in user.roles):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Требуются права администратора")
    return user


async def get_user_acl(db: AsyncSession, user: User) -> dict:
    """Собирает ACL пользователя: workspace_ids + dept_ids (с наследованием).

    Результат кэшируется в Redis (TTL 60c), инвалидируется при изменении прав.
    Возвращает структуру для Qdrant pre-filter.
    """
    import redis.asyncio as aioredis

    from app.core.config import get_settings as gs

    cache_key = f"acl:{user.id}"
    redis = aioredis.from_url(gs().redis_url, decode_responses=True)
    try:
        cached = await redis.get(cache_key)
        if cached:
            import json

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
    # Хранится в Permission.level ( pii_read | read | write | admin );
    # admin-роль видит ПДн везде без отдельного права.
    is_admin = any(r.name == "admin" for r in user.roles)
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
    }

    redis = aioredis.from_url(gs().redis_url, decode_responses=True)
    try:
        import json

        await redis.setex(cache_key, 60, json.dumps(acl))
    finally:
        await redis.aclose()
    return acl


async def check_workspace_access(
    db: AsyncSession, user: User, workspace_id: uuid.UUID, level: str = "read"
) -> bool:
    """Прямая проверка доступа к workspace (для admin-операций и загрузки)."""
    if any(r.name == "admin" for r in user.roles):
        return True

    level_order = {"read": 0, "write": 1, "admin": 2}
    needed = level_order[level]

    perm = (
        await db.execute(
            select(Permission).where(
                cond,
                (Permission.user_id == user.id)
                | (Permission.department_id == user.department_id),
            )
        )
    ).scalars().all()
    return any(level_order[p.level] >= needed for p in perm)
