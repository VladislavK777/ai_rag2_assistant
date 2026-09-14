"""Аутентификация: SSO Keycloak (LDAP). Backend — resource server.

Логин выполняется фронтом через Keycloak (authorization code + PKCE,
client_id rag2_client) — здесь только валидация токена и профиль.
"""
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.keycloak import claims_to_dto, decode_token
from app.core.security import get_current_user, get_db
from app.db.models import User

router = APIRouter()

bearer = HTTPBearer(auto_error=False)
settings = get_settings()


@router.get("/config")
async def auth_config() -> dict[str, str]:
    """Публичные параметры SSO для фронтенда (login-редирект).

    public_issuer — адрес Keycloak с точки зрения браузера (nginx /auth/
    в контуре; на Mac — прямой порт). По умолчанию равен issuer.
    """
    return {
        "issuer": settings.keycloak_public_issuer or settings.keycloak_issuer,
        "client_id": settings.keycloak_client_id,
    }


@router.get("/me")
async def me(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Профиль из JWT-claims (идентичность — Keycloak, не БД)."""
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется авторизация")
    try:
        payload = await decode_token(credentials.credentials)
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Невалидный токен")
    dto = claims_to_dto(payload)
    return {
        "id": str(user.id),
        "employee_no": dto["employee_no"],
        "email": dto["email"],
        "full_name": dto["full_name"],
        "roles": ["admin"] if dto["is_admin"] else ["user"],
        "department_id": str(user.department_id) if user.department_id else None,
    }
