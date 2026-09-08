"""Аутентификация: login, регистрация администратором, bootstrap."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    create_access_token,
    get_current_user,
    get_db,
    hash_password,
    verify_password,
)
from app.db.audit import audit
from app.db.models import Role, User

router = APIRouter()


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    email: str
    full_name: str
    password: str


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    user = (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        await audit(db, user_id=None, action="login", decision="deny", resource=body.email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный email или пароль")

    token = create_access_token(user.id, [r.name for r in user.roles])
    await audit(db, user_id=str(user.id), action="login", decision="allow")
    return TokenResponse(access_token=token)


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "roles": [r.name for r in user.roles],
        "department_id": str(user.department_id) if user.department_id else None,
    }


@router.post("/users", status_code=201)
async def create_user(
    body: UserCreate,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Создание пользователя (LDAP-интеграция — Фаза 2)."""
    exists = (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "Пользователь уже существует")

    role = (
        await db.execute(select(Role).where(Role.name == "user"))
    ).scalar_one()
    user = User(
        email=body.email,
        full_name=body.full_name,
        password_hash=hash_password(body.password),
    )
    user.roles.append(role)
    db.add(user)
    await db.commit()
    await audit(db, user_id=str(admin.id), action="user_create", resource=body.email)
    return {"id": str(user.id), "email": user.email}
