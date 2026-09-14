"""CRUD департаментов и просмотр пользователей (только admin).

Пользователи создаются/управляются в Keycloak (SSO/LDAP) — здесь только
теневые записи (read-only): список для справочника прав и отладки.
Департаменты — локальный справочник с корпоративными кодами (ДЕП-хххх),
по коду маппятся dept_code из JWT.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, get_db, require_admin
from app.db.audit import audit
from app.db.models import Department, User

router = APIRouter(dependencies=[Depends(require_admin)])


# ---------- Департаменты ----------


class DepartmentCreate(BaseModel):
    code: str  # ДЕП-хххх
    name: str
    parent_id: str | None = None


class DepartmentUpdate(BaseModel):
    name: str | None = None
    parent_id: str | None = None


@router.get("/departments")
async def list_departments(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Department).order_by(Department.code))).scalars()
    return [
        {
            "id": str(d.id),
            "code": d.code,
            "name": d.name,
            "parent_id": str(d.parent_id) if d.parent_id else None,
        }
        for d in rows
    ]


@router.post("/departments", status_code=201)
async def create_department(
    body: DepartmentCreate,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    exists = (
        await db.execute(select(Department).where(Department.code == body.code))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "Департамент с таким кодом уже существует")
    if body.parent_id:
        parent = await db.get(Department, uuid.UUID(body.parent_id))
        if parent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Родительский департамент не найден")
    dept = Department(
        code=body.code,
        name=body.name,
        parent_id=uuid.UUID(body.parent_id) if body.parent_id else None,
    )
    db.add(dept)
    await db.commit()
    await audit(db, user_id=str(admin.id), action="department_create", resource=body.code)
    return {"id": str(dept.id), "code": dept.code, "name": dept.name}


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
    await audit(db, user_id=str(admin.id), action="department_update", resource=dept.code)
    return {"id": str(dept.id), "code": dept.code, "name": dept.name,
            "parent_id": str(dept.parent_id) if dept.parent_id else None}


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
    await audit(db, user_id=str(admin.id), action="department_delete", resource=dept.code)


# ---------- Пользователи (read-only) ----------


def _user_dto(u: User) -> dict:
    return {
        "id": str(u.id),
        "employee_no": u.employee_no,
        "email": u.email,
        "full_name": u.full_name,
        "department_id": str(u.department_id) if u.department_id else None,
        "is_active": u.is_active,
    }


@router.get("/users")
async def list_users(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(User).order_by(User.full_name))).scalars()
    return [_user_dto(u) for u in rows]