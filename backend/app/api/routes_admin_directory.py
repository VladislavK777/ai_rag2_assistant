"""CRUD департаментов и пользователей (только admin). Дополняет routes_admin."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    get_current_user,
    get_db,
    hash_password,
    require_admin,
)
from app.db.audit import audit
from app.db.models import Department, Role, User

router = APIRouter(dependencies=[Depends(require_admin)])


# ---------- Департаменты ----------


class DepartmentCreate(BaseModel):
    name: str
    parent_id: str | None = None


class DepartmentUpdate(BaseModel):
    name: str | None = None
    parent_id: str | None = None


@router.get("/departments")
async def list_departments(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Department).order_by(Department.name))).scalars()
    return [
        {"id": str(d.id), "name": d.name, "parent_id": str(d.parent_id) if d.parent_id else None}
        for d in rows
    ]


@router.post("/departments", status_code=201)
async def create_department(
    body: DepartmentCreate,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.parent_id:
        parent = await db.get(Department, uuid.UUID(body.parent_id))
        if parent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Родительский департамент не найден")
    dept = Department(name=body.name, parent_id=uuid.UUID(body.parent_id) if body.parent_id else None)
    db.add(dept)
    await db.commit()
    await audit(db, user_id=str(admin.id), action="department_create", resource=body.name)
    return {"id": str(dept.id), "name": dept.name}


@router.patch("/departments/{dept_id}")
async def update_department(
    dept_id: str,
    body: DepartmentUpdate,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    dept = await db.get(Department, uuid.UUID(dept_id))
    if dept is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Департамент не найден")
    if body.parent_id:
        if body.parent_id == dept_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Департамент не может быть родителем себе")
        parent = await db.get(Department, uuid.UUID(body.parent_id))
        if parent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Родительский департамент не найден")
        dept.parent_id = uuid.UUID(body.parent_id)
    if body.name is not None:
        dept.name = body.name
    await db.commit()
    await audit(db, user_id=str(admin.id), action="department_update", resource=dept.name)
    return {"id": str(dept.id), "name": dept.name, "parent_id": str(dept.parent_id) if dept.parent_id else None}


@router.delete("/departments/{dept_id}", status_code=204)
async def delete_department(
    dept_id: str,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    dept = await db.get(Department, uuid.UUID(dept_id))
    if dept is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Департамент не найден")
    children = (
        await db.execute(select(Department).where(Department.parent_id == dept.id))
    ).scalar_one_or_none()
    if children:
        raise HTTPException(status.HTTP_409_CONFLICT, "У департамента есть дочерние — удалите сначала их")
    users = (
        await db.execute(select(User).where(User.department_id == dept.id).limit(1))
    ).scalar_one_or_none()
    if users:
        raise HTTPException(status.HTTP_409_CONFLICT, "К департаменту привязаны пользователи")
    await db.delete(dept)
    await db.commit()
    await audit(db, user_id=str(admin.id), action="department_delete", resource=dept.name)


# ---------- Пользователи ----------


class UserAdminCreate(BaseModel):
    email: str
    full_name: str
    password: str
    department_id: str | None = None
    is_admin: bool = False


class UserAdminUpdate(BaseModel):
    full_name: str | None = None
    password: str | None = None
    department_id: str | None = None
    is_active: bool | None = None
    is_admin: bool | None = None


def _user_dto(u: User) -> dict:
    return {
        "id": str(u.id),
        "email": u.email,
        "full_name": u.full_name,
        "department_id": str(u.department_id) if u.department_id else None,
        "is_active": u.is_active,
        "is_admin": any(r.name == "admin" for r in u.roles),
    }


@router.get("/users")
async def list_users(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(User).order_by(User.email))).scalars()
    return [_user_dto(u) for u in rows]


async def _invalidate_acl(db: AsyncSession, user_id: uuid.UUID) -> None:
    import redis.asyncio as aioredis

    from app.core.config import get_settings

    redis = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    await redis.delete(f"acl:{user_id}")
    await redis.aclose()


@router.post("/users", status_code=201)
async def create_user_admin(
    body: UserAdminCreate,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    exists = (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "Пользователь уже существует")
    if body.department_id and await db.get(Department, uuid.UUID(body.department_id)) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Департамент не найден")

    user = User(
        email=body.email,
        full_name=body.full_name,
        password_hash=hash_password(body.password),
        department_id=uuid.UUID(body.department_id) if body.department_id else None,
    )
    role_name = "admin" if body.is_admin else "user"
    role = (await db.execute(select(Role).where(Role.name == role_name))).scalar_one()
    user.roles.append(role)
    db.add(user)
    await db.commit()
    await audit(db, user_id=str(admin.id), action="user_create", resource=body.email)
    return _user_dto(user)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    body: UserAdminUpdate,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, uuid.UUID(user_id))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    if body.department_id is not None:
        if body.department_id and await db.get(Department, uuid.UUID(body.department_id)) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Департамент не найден")
        user.department_id = uuid.UUID(body.department_id) if body.department_id else None
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.password:
        user.password_hash = hash_password(body.password)
    if body.is_active is not None:
        if not body.is_active and user.id == admin.id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Нельзя деактивировать себя")
        user.is_active = body.is_active
    if body.is_admin is not None:
        admin_role = (await db.execute(select(Role).where(Role.name == "admin"))).scalar_one()
        user_role = (await db.execute(select(Role).where(Role.name == "user"))).scalar_one()
        has_admin = any(r.name == "admin" for r in user.roles)
        if body.is_admin and not has_admin:
            user.roles.append(admin_role)
        elif not body.is_admin and has_admin:
            if user.id == admin.id:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Нельзя снять права с себя")
            user.roles = [r for r in user.roles if r.name != "admin"] or [user_role]
    await db.commit()
    await _invalidate_acl(db, user.id)
    await audit(db, user_id=str(admin.id), action="user_update", resource=user.email)
    return _user_dto(user)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user_id == str(admin.id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Нельзя удалить себя")
    user = await db.get(User, uuid.UUID(user_id))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    await db.delete(user)
    await db.commit()
    await audit(db, user_id=str(admin.id), action="user_delete", resource=user.email)
