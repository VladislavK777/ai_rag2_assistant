"""Unit-тесты security-утилит."""
import pytest

from app.core.security import hash_password, verify_password


def test_password_hash_roundtrip():
    h = hash_password("S3cret!")
    assert h != "S3cret!"
    assert verify_password("S3cret!", h)
    assert not verify_password("wrong", h)
