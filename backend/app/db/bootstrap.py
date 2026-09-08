"""Bootstrap: создание администратора и справочников при первом запуске.

Запуск: docker compose exec backend python -m app.db.bootstrap
"""
import asyncio
import uuid

from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import SessionLocal, hash_password
from app.db.models import AuditLog, Base, Department, Permission, Role, User, Workspace


async def bootstrap() -> None:
    settings = get_settings()

    # Таблицы (MVP: create_all; Фаза 2 — Alembic-миграции)
    from app.db.models import Base as _B

    from app.db.session import make_engine

    engine = make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(_B.metadata.create_all)

    # Триггер append-only для audit_log
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE OR REPLACE FUNCTION audit_no_update_delete()
                RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION 'audit_log is append-only';
                END;
                $$ LANGUAGE plpgsql;
                """
            )
        )
        await conn.execute(
            text(
                """
                DROP TRIGGER IF EXISTS audit_protect ON audit_log;
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TRIGGER audit_protect
                BEFORE UPDATE OR DELETE ON audit_log
                FOR EACH ROW EXECUTE FUNCTION audit_no_update_delete();
                """
            )
        )

    async with SessionLocal() as db:
        # Роли
        for name in ("admin", "user", "auditor"):
            exists = (
                await db.execute(select(Role).where(Role.name == name))
            ).scalar_one_or_none()
            if not exists:
                db.add(Role(id=_fixed_id(name), name=name))
        await db.commit()

        # Администратор из переменных окружения
        admin_email = settings.admin_email if hasattr(settings, "admin_email") else None
        if not admin_email:
            import os

            admin_email = os.environ.get("ADMIN_EMAIL", "admin@company.ru")
            admin_password = os.environ.get("ADMIN_PASSWORD", "changeme")
        else:
            admin_password = getattr(settings, "admin_password", "changeme")

        admin = (
            await db.execute(select(User).where(User.email == admin_email))
        ).scalar_one_or_none()
        if not admin:
            admin_role = (
                await db.execute(select(Role).where(Role.name == "admin"))
            ).scalar_one()
            admin = User(
                email=admin_email,
                full_name="Администратор RAG2",
                password_hash=hash_password(admin_password),
            )
            admin.roles.append(admin_role)
            db.add(admin)
            await db.commit()

            # Демо-workspace с полным доступом админа
            ws = Workspace(
                name="Общая база знаний",
                description="Демо-пространство, созданное при bootstrap",
                owner_id=admin.id,
            )
            db.add(ws)
            await db.commit()
            db.add(Permission(user_id=admin.id, workspace_id=ws.id, level="admin"))
            await db.commit()

    print(f"Bootstrap завершён. Администратор: {admin_email}")


def _fixed_id(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"rag2-role-{name}")


if __name__ == "__main__":
    asyncio.run(bootstrap())
