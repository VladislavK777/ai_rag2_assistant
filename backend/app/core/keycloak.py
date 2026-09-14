"""Интеграция Keycloak (SSO/LDAP): валидация JWT.

Backend — resource server: токены выпускает Keycloak (realm rag2, RS256),
здесь проверяется подпись по JWKS (кэш, обновление по kid-miss), iss/exp/aud.
Локальной аутентификации (пароли в БД) нет — источник идентичности один.

Claims от LDAP-мапперов (payload):
    employee_no — табельный номер (ТАБ-хххх)
    email, full_name, dept_code (ДЕП-хххх)
    role: ["ADMIN" | "EMPLOYEE"]
"""
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.core.config import get_settings

logger = logging.getLogger("rag2.keycloak")

bearer = HTTPBearer(auto_error=False)
JWKS_TTL_SECONDS = 3600
LEEWAY_SECONDS = 30


@dataclass
class JwksCache:
    keys: list[dict] = field(default_factory=list)
    fetched_at: float = 0.0
    issuers: list[str] = field(default_factory=list)


_jwks = JwksCache()


async def _get_signing_key(kid: str) -> dict | None:
    """RSA public key по kid; при miss — одно обновление JWKS."""
    settings = get_settings()
    now = time.monotonic()
    if not _jwks.keys or now - _jwks.fetched_at > JWKS_TTL_SECONDS:
        await refresh_jwks()
    for k in _jwks.keys:
        if k.get("kid") == kid:
            return k
    # ротация ключей Keycloak — обновляем кэш и пробуем ещё раз
    await refresh_jwks()
    for k in _jwks.keys:
        if k.get("kid") == kid:
            return k
    return None


async def refresh_jwks() -> None:
    settings = get_settings()
    # JWKS всегда тянем по внутреннему issuer (доступен из docker-сети),
    # а iss в токенах валидируем по обоим адресам: Keycloak в dev-режиме ставит
    # iss по тому URL, с которым пришёл запрос, поэтому токены, выпущенные
    # браузерному фронту, несут публичный issuer, а не внутренний.
    issuers = [settings.keycloak_issuer]
    if settings.keycloak_public_issuer:
        issuers.append(settings.keycloak_public_issuer)
    jwks_url = f"{settings.keycloak_issuer}/protocol/openid-connect/certs"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(jwks_url)
        resp.raise_for_status()
    _jwks.keys = resp.json().get("keys", [])
    _jwks.fetched_at = time.monotonic()
    _jwks.issuers = issuers
    logger.info("JWKS refreshed: %d keys (issuers: %s)", len(_jwks.keys), issuers)


def user_id_from_employee_no(employee_no: str) -> uuid.UUID:
    """Детерминированный id: теневая запись стабильна между сессиями."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"rag2:{employee_no}")


async def decode_token(token: str) -> dict[str, Any]:
    """Валидация access-токена: подпись RS256, iss, exp, aud."""
    settings = get_settings()
    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Невалидный токен")

    key = await _get_signing_key(header.get("kid", ""))
    if key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неизвестный ключ подписи")

    try:
        issuers = _jwks.issuers or [settings.keycloak_issuer]
        # aud у public-клиента Keycloak часто = "account", клиент проверяем по azp/aud сами
        payload = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            options={
                "leeway": LEEWAY_SECONDS,
                "verify_aud": False,
            },
            issuer=issuers,
        )
        azp = payload.get("azp")
        aud = payload.get("aud")
        aud_list = aud if isinstance(aud, list) else [aud] if aud else []
        if azp != settings.keycloak_client_id and settings.keycloak_client_id not in aud_list:
            raise JWTError("Токен выдан другому клиенту")
        return payload
    except JWTError as exc:
        logger.warning("JWT rejected: %s", type(exc).__name__)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Невалидный или истёкший токен")


def claims_to_dto(payload: dict[str, Any]) -> dict[str, Any]:
    """Claims JWT → DTO пользователя (для /me и JIT-тени).

    Роли: realm-роль или client-роль rag2_client (role: [ADMIN|EMPLOYEE]).
    """
    roles = set(payload.get("role") or [])
    realm_access = (payload.get("realm_access") or {}).get("roles") or []
    roles |= {r.upper() for r in realm_access if r.upper() in ("ADMIN", "EMPLOYEE")}
    resource_roles = ((payload.get("resource_access") or {})
                      .get(get_settings().keycloak_client_id) or {}).get("roles") or []
    roles |= {r.upper() for r in resource_roles if r.upper() in ("ADMIN", "EMPLOYEE")}

    employee_no = payload.get("employee_no") or ""
    if not employee_no:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "В токене нет табельного номера")

    return {
        "employee_no": employee_no,
        "email": payload.get("email") or "",
        "full_name": payload.get("full_name") or payload.get("preferred_username") or "",
        "dept_code": payload.get("dept_code"),
        "is_admin": "ADMIN" in roles,
        "employee": "EMPLOYEE" in roles or "ADMIN" in roles,
    }
