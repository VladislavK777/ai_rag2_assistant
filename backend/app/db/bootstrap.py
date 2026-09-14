"""Bootstrap: схема БД и append-only триггер аудита.

Пользователи не создаются — аутентификация в Keycloak (SSO/LDAP),
теневые записи JIT-создаются из JWT. Справочник департаментов
и демо-данные — app.db.seed_demo.

Запуск: docker compose exec backend python -m app.db.bootstrap
"""
import asyncio

from sqlalchemy import text

from app.core.security import SessionLocal
from app.db.models import Base


async def bootstrap() -> None:
    # Таблицы (MVP: create_all; Фаза 2 — Alembic-миграции)
    from app.db.session import make_engine

    engine = make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Триггер append-only для audit_log
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
        await conn.execute(text("DROP TRIGGER IF EXISTS audit_protect ON audit_log;"))
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
        await db.commit()

    print("Bootstrap завершён: схема создана. Пользователи — через Keycloak (seed_demo: департаменты).")


if __name__ == "__main__":
    asyncio.run(bootstrap())