"""Пароли больше не хранятся в RAG2 (аутентификация — Keycloak/LDAP).

Проверяем смежные примитивы безопасности: детерминизм теневых id,
конфигурацию SSO, формат claim'ов.
"""
import uuid

import pytest
from fastapi import HTTPException

from app.core.config import get_settings
from app.core.keycloak import claims_to_dto, user_id_from_employee_no


def test_user_id_deterministic_per_employee():
    a = user_id_from_employee_no("ТАБ-0001")
    b = user_id_from_employee_no("ТАБ-0001")
    other = user_id_from_employee_no("ТАБ-0002")
    assert a == b and a != other


def test_settings_sso_defaults():
    s = get_settings()
    assert s.keycloak_client_id == "rag2_client"
    assert s.keycloak_issuer.endswith("/realms/rag2")


def test_claims_to_dto_requires_employee_no():
    with pytest.raises(HTTPException):
        claims_to_dto({"role": ["EMPLOYEE"], "email": "x@y"})