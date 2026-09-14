"""Тесты SSO-аутентификации: валидация JWT Keycloak + claims → DTO → JIT-тень.

Подпись проверяется на реальных RS256-ключах (генерируются на месте),
issuer/audience — по конфигу.
"""
import time
import uuid

import pytest
from jose import jwt as jose_jwt

from app.core.config import get_settings
from app.core.keycloak import claims_to_dto, decode_token, user_id_from_employee_no


@pytest.fixture
def rsa_keypair():
    """RS256-пара: приватный (подпись) + публичный JWK (для JWKS-мока)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose.utils import base64url_decode

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    nums = key.public_key().public_numbers()

    def _int_to_b64(n: int, size: int) -> str:
        b = n.to_bytes(size, "big")
        import base64

        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    jwk = {
        "kty": "RSA",
        "kid": "test-key-1",
        "alg": "RS256",
        "use": "sig",
        "n": _int_to_b64(nums.n, 256),
        "e": _int_to_b64(nums.e, 3),
    }
    return priv_pem.decode(), jwk


@pytest.fixture
def patch_jwks(rsa_keypair, monkeypatch):
    """JWKS-кэш подменяется тестовым ключом (сетевой вызов Keycloak не нужен)."""
    from app.core import keycloak as kc

    _, jwk = rsa_keypair
    monkeypatch.setattr(kc._jwks, "keys", [jwk])
    monkeypatch.setattr(kc._jwks, "fetched_at", time.monotonic())
    return kc


def _make_token(priv_pem: str, claims: dict) -> str:
    return jose_jwt.encode(claims, priv_pem, algorithm="RS256", headers={"kid": "test-key-1"})


def _base_claims() -> dict:
    settings = get_settings()
    now = int(time.time())
    return {
        "sub": uuid.uuid4().hex,
        "iss": settings.keycloak_issuer,
        "aud": settings.keycloak_client_id,
        "exp": now + 300,
        "employee_no": "ТАБ-0001",
        "email": "ivanov@company.ru",
        "full_name": "Иванов Иван Иванович",
        "dept_code": "ДЕП-0100",
        "role": ["ADMIN"],
    }


def test_valid_token_decodes(patch_jwks, rsa_keypair):
    priv, _ = rsa_keypair
    token = _make_token(priv, _base_claims())
    decoded = _decode(patch_jwks, token)
    assert decoded["employee_no"] == "ТАБ-0001"
    assert decoded["dept_code"] == "ДЕП-0100"


def _decode(kc, token: str) -> dict:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(kc.decode_token(token))
    finally:
        loop.close()


def test_expired_token_rejected(patch_jwks, rsa_keypair):
    priv, _ = rsa_keypair
    claims = _base_claims()
    claims["exp"] = int(time.time()) - 3600
    token = _make_token(priv, claims)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        _decode(patch_jwks, token)
    assert e.value.status_code == 401


def test_wrong_issuer_rejected(patch_jwks, rsa_keypair):
    priv, _ = rsa_keypair
    claims = _base_claims()
    claims["iss"] = "http://evil/realms/other"
    token = _make_token(priv, claims)
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _decode(patch_jwks, token)


def test_claims_to_dto_admin():
    dto = claims_to_dto(_base_claims())
    assert dto["is_admin"] is True
    assert dto["employee"] is True
    assert dto["employee_no"] == "ТАБ-0001"


def test_claims_to_dto_employee_realm_access():
    claims = _base_claims()
    claims["role"] = []
    claims["realm_access"] = {"roles": ["EMPLOYEE", "offline_access"]}
    claims["dept_code"] = "ДЕП-0300"
    dto = claims_to_dto(claims)
    assert dto["is_admin"] is False
    assert dto["employee"] is True
    assert dto["dept_code"] == "ДЕП-0300"


def test_claims_without_employee_no_rejected():
    claims = _base_claims()
    claims["employee_no"] = None
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        claims_to_dto(claims)


def test_user_id_deterministic():
    """Один табельный номер → один id (история чатов стабильна)."""
    a = user_id_from_employee_no("ТАБ-0001")
    b = user_id_from_employee_no("ТАБ-0001")
    c = user_id_from_employee_no("ТАБ-0002")
    assert a == b and a != c
