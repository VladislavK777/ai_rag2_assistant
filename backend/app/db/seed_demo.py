"""Демо-данные: департаменты (справочник), теневые пользователи Keycloak, workspace'ы, документы.

Идемпотентный. Пользователи НЕ создаются с паролями — пароли и роли в Keycloak
(realm rag2, client_id rag2_client); здесь заводятся теневые записи, чтобы
права (Permission) можно было выдать до первого входа сотрудника.

Запуск:
    docker compose exec backend python -m app.db.seed_demo
Опционально:
    SEED_SKIP_INGEST=1  — только справочники/права, без загрузки документов
"""
import asyncio
import io
import uuid

from sqlalchemy import select

from app.core.security import SessionLocal
from app.core.keycloak import user_id_from_employee_no
from app.db.models import (
    Department,
    Document,
    Permission,
    User,
    Workspace,
)

# Теневые записи пользователей Keycloak (пароли — в Keycloak).
# key → (employee_no, email, full_name, dept_key, admin?)
USERS = {
    "ivanov": {
        "employee_no": "ТАБ-0001",
        "email": "ivanov@company.ru",
        "full_name": "Иванов Иван Иванович",
        "department": "it",
        "is_admin": True,  # администратор системы
    },
    "petrova": {
        "employee_no": "ТАБ-0002",
        "email": "petrova@company.ru",
        "full_name": "Петрова Анна Сергеевна",
        "department": "hr",
        "is_admin": False,
    },
    "smirnov": {
        "employee_no": "ТАБ-0003",
        "email": "smirnov@company.ru",
        "full_name": "Смирнов Дмитрий Александрович",
        "department": "fin",
        "is_admin": False,
    },
    "sidorov": {
        "employee_no": "ТАБ-0004",
        "email": "sidorov@company.ru",
        "full_name": "Сидоров Павел Викторович",
        "department": "it-dev",
        "is_admin": False,
    },
    "kuznetsova": {
        "employee_no": "ТАБ-0005",
        "email": "kuznetsova@company.ru",
        "full_name": "Кузнецова Мария Андреевна",
        "department": "audit",
        "is_admin": False,
    },
    "hacker": {
        "employee_no": "ТАБ-9999",
        "email": "hacker@evil.net",
        "full_name": "Злоумышленник (внешний)",
        "department": None,
        "is_admin": False,
    },
}

# Департаменты: key → (parent_key, code, name)
DEPARTMENTS = {
    "company": (None, "ДЕП-0001", "Компания"),
    "it": ("company", "ДЕП-0100", "Департамент ИТ"),
    "it-dev": ("it", "ДЕП-0110", "Отдел разработки"),
    "it-ops": ("it", "ДЕП-0120", "Отдел эксплуатации"),
    "it-sec": ("it", "ДЕП-0130", "Отдел информационной безопасности"),
    "hr": ("company", "ДЕП-0200", "Департамент управления персоналом"),
    "hr-kadr": ("hr", "ДЕП-0210", "Отдел кадрового администрирования"),
    "hr-train": ("hr", "ДЕП-0220", "Отдел обучения и развития"),
    "fin": ("company", "ДЕП-0300", "Финансовый департамент"),
    "fin-buh": ("fin", "ДЕП-0310", "Бухгалтерия"),
    "audit": ("company", "ДЕП-0400", "Служба внутреннего аудита"),
    "marketing": ("company", "ДЕП-0500", "Департамент маркетинга"),
}

# Workspace'ы: key → (name, description, доступа: 'all' | 'hr' | 'fin')
WORKSPACES = {
    "common": ("Общая база знаний", "Общекорпоративные регламенты и инструкции", "all"),
    "hr": ("HR: кадры и политика", "Кадровая политика, оклады, персональные данные", "hr"),
    "fin": ("Финансы: отчёты", "Финансовая отчётность и бюджеты", "fin"),
}

DOC_CONTENTS = {
    "common": {
        "reglament_it.txt": (
            "Регламент использования ИТ-сервисов\n\n"
            "1. Рабочие станции. Все рабочие станции обязаны иметь включённое "
            "шифрование диска (FileVault / BitLocker). Пароль экрана блокировки — "
            "не реже чем каждые 15 минут.\n\n"
            "2. Пароли. Минимальная длина пароля — 12 символов, обязателен "
            "менеджер паролей (KeePassXC / 1Password). Двухфакторная аутентификация "
            "обязательна для всех внешних сервисов.\n\n"
            "3. VPN. Доступ к внутренним ресурсам извне офиса — только через "
            "корпоративный VPN. Запрещено использование публичных Wi-Fi сетей без VPN.\n\n"
            "4. Резервное копирование. Критичные проекты хранятся в Git, бэкапы — "
            "ежедневно, хранение 30 дней. Восстановление проверяется ежеквартально.\n\n"
            "5. Инциденты. Об обнаружении фишинга или утечки сообщать в ИБ "
            "в течение 1 часа: security@company.ru."
        ),
        "onboarding.txt": (
            "Памятка нового сотрудника\n\n"
            "Добро пожаловать в компанию! Первая неделя:\n\n"
            "День 1: получение техники, учётных записей, подписание документов в HR.\n"
            "День 2: ознакомление с регламентами ИБ и корпоративной культурой.\n"
            "День 3-5: знакомство с командой и рабочими процессами.\n\n"
            "Корпоративные коммуникации: почта, мессенджер корпоративный, "
            "видеоконференции через внутренний сервис.\n\n"
            "Отпуск: заявка через HR-портал, за 2 недели до начала. "
            "Стандартный отпуск — 28 календарных дней.\n\n"
            "Больничный: сообщить руководителю в первый день, электронный "
            "лист нетрудоспособности поступает в HR автоматически."
        ),
    },
    "hr": {
        "kadrovaya_politika.txt": (
            "Кадровая политика и compensation & benefits (КОНФИДЕНЦИАЛЬНО)\n\n"
            "1. Система грейдов. В компании действует 7 грейдов: junior, middle, "
            "senior, lead, principal, head, director. Окладные вилки:\n"
            "- junior: 90 000 - 130 000 руб.\n"
            "- middle: 140 000 - 210 000 руб.\n"
            "- senior: 220 000 - 320 000 руб.\n"
            "- lead: 330 000 - 450 000 руб.\n"
            "- head: 460 000 - 600 000 руб.\n\n"
            "2. Годовая премия. Целевая премия — 20% от оклада, при превышении "
            "KPI — до 40%. Выплата в марте по итогам года.\n\n"
            "3. Персональные данные сотрудников обрабатываются в соответствии "
            "с 152-ФЗ. Доступ к окладам имеют только сотрудники HR и бухгалтерии.\n\n"
            "4. Пересмотр окладов — ежегодно в апреле, по результатам performance review."
        ),
        "prikaz_perevod.txt": (
            "Приказ №142-к о переводе сотрудника\n\n"
            "Перевести Смирнова Дмитрия Александровича с должности "
            "«Ведущий аналитик» (грейд senior) на должность «Руководитель "
            "направления аналитики» (грейд lead) с 01.09.2026.\n\n"
            "Установить оклад 350 000 руб. в соответствии с вилкой грейда lead.\n\n"
            "Основание: служебная записка директора департамента финансов, "
            "протокол аттестации №7 от 15.08.2026.\n\n"
            "Документ содержит персональные данные. Распространение запрещено."
        ),
    },
    "fin": {
        "budget_2026.txt": (
            "Бюджет компании на 2026 год (КОНФИДЕНЦИАЛЬНО)\n\n"
            "1. Выручка: план 850 млн руб., в т.ч. основные контракты — 620 млн, "
            "новые направления — 230 млн.\n\n"
            "2. ФОТ: 412 млн руб. (48% выручки). Штатное расписание — 180 человек.\n\n"
            "3. Инфраструктура: 64 млн руб., в том числе:\n"
            "- AI-платформа (GPU, лицензии, Yandex GPT API): 12 млн руб.\n"
            "- Перевод на on-premise LLM окупается за 7 месяцев (см. ROI-анализ).\n\n"
            "4. Маркетинг: 38 млн руб.\n"
            "5. Резерв: 25 млн руб.\n\n"
            "Доступ: департамент финансов, совет директоров."
        ),
        "audit_report.txt": (
            "Отчёт внутреннего аудита за II полугодие 2025\n\n"
            "Проверка закупочных процедур: отклонений не выявлено.\n\n"
            "Проверка расходования бюджета AI-платформы: экономия 18% против "
            "плана за счёт перехода с GPU-инференса на внешний API в "
            "непиковые часы.\n\n"
            "Рекомендации: 1) усилить контроль за подписками SaaS; "
            "2) внедрить обязательный тендер для закупок свыше 500 тыс. руб.\n\n"
            "Документ содержит коммерческую тайну."
        ),
    },
}


async def _get_or_create(db, model, **kwargs):
    obj = (await db.execute(select(model).filter_by(**kwargs))).scalar_one_or_none()
    if obj is None:
        obj = model(**kwargs)
        db.add(obj)
        await db.flush()
    return obj


async def seed() -> None:
    async with SessionLocal() as db:
        # --- Департаменты (справочник по кодам ДЕП-хххх) ---
        depts: dict[str, Department] = {}
        for key, (parent_key, code, name) in DEPARTMENTS.items():
            dept = (
                await db.execute(select(Department).where(Department.code == code))
            ).scalar_one_or_none()
            if dept is None:
                dept = Department(code=code, name=name)
                db.add(dept)
                await db.flush()
            else:
                dept.name = name  # переименование подтягивается при повторном сиде
            if parent_key:
                parent = depts.get(parent_key)
                if parent is None:
                    parent = await _get_or_create(db, Department, code=DEPARTMENTS[parent_key][1])
                    parent.name = DEPARTMENTS[parent_key][2]
                dept.parent_id = parent.id
            depts[key] = dept
        await db.flush()

        # --- Теневые записи пользователей Keycloak (идентичность — в Keycloak) ---
        users: dict[str, User] = {}
        for key, spec in USERS.items():
            uid = user_id_from_employee_no(spec["employee_no"])
            user = await db.get(User, uid)
            if user is None:
                user = User(
                    id=uid,
                    employee_no=spec["employee_no"],
                    email=spec["email"],
                    full_name=spec["full_name"],
                    department_id=depts[spec["department"]].id if spec["department"] else None,
                    is_active=True,
                )
                db.add(user)
                await db.flush()
            users[key] = user

        # --- Workspace'ы + права ---
        async def _ensure_perm(user_id=None, dept_id=None, ws_id=None, level="read"):
            """Точечно: нет права — добавить (идемпотентность по паре)."""
            cond = [Permission.workspace_id == ws_id, Permission.level == level]
            cond.append(
                Permission.user_id == user_id
                if user_id
                else Permission.department_id == dept_id
            )
            exists = (
                await db.execute(select(Permission).where(*cond))
            ).scalar_one_or_none()
            if not exists:
                db.add(
                    Permission(
                        user_id=user_id,
                        department_id=dept_id,
                        workspace_id=ws_id,
                        level=level,
                    )
                )

        ws_ids: dict[str, uuid.UUID] = {}
        for key, (name, desc, access) in WORKSPACES.items():
            ws = (
                await db.execute(select(Workspace).where(Workspace.name == name))
            ).scalar_one_or_none()
            if ws is None:
                ws = Workspace(name=name, description=desc, owner_id=users["ivanov"].id)
                db.add(ws)
                await db.flush()
            ws_ids[key] = ws.id

            # Администратор системы (Иванов) — полный доступ
            await _ensure_perm(user_id=users["ivanov"].id, ws_id=ws.id, level="admin")
            if access == "all":
                for ukey in ("petrova", "smirnov", "sidorov", "kuznetsova"):
                    await _ensure_perm(
                        user_id=users[ukey].id, ws_id=ws.id, level="read"
                    )
            elif access == "hr":
                await _ensure_perm(dept_id=depts["hr"].id, ws_id=ws.id, level="read")
            elif access == "fin":
                await _ensure_perm(dept_id=depts["fin"].id, ws_id=ws.id, level="read")
            await db.flush()

        await db.commit()
        print(f"Департаментов: {len(depts)}, теневых пользователей: {len(users)}. Права созданы/проверены.")
        print("Администратор системы (роль ADMIN в Keycloak): Иванов Иван Иванович (ТАБ-0001).")

    # --- Документы (MinIO + очередь индексации) ---
    import os

    if os.environ.get("SEED_SKIP_INGEST"):
        print("SEED_SKIP_INGEST=1 — документы не загружались.")
        return

    from app.services.tasks import ingest_document

    async with SessionLocal() as db:
        from miniopy_async import Minio

        from app.core.config import get_settings

        settings = get_settings()
        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=False,
        )
        try:
            await client.bucket_exists("documents")
        except Exception:
            await client.make_bucket("documents")

        for ws_key, docs in DOC_CONTENTS.items():
            ws_id = ws_ids[ws_key]
            for fname, content in docs.items():
                exists = (
                    await db.execute(
                        select(Document).where(
                            Document.title == fname, Document.workspace_id == ws_id
                        )
                    )
                ).scalar_one_or_none()
                if exists:
                    print(f"  = {ws_key}/{fname} уже есть (status={exists.status})")
                    continue

                minio_key = f"documents/{ws_id}/{uuid.uuid4()}/{fname}"
                data = content.encode("utf-8")
                await client.put_object(
                    "documents",
                    minio_key.removeprefix("documents/"),
                    io.BytesIO(data),
                    length=len(data),
                    content_type="text/plain",
                )
                doc = Document(
                    workspace_id=ws_id,
                    minio_key=minio_key,
                    title=fname,
                    mime_type="text/plain",
                    status="pending",
                    uploaded_by=users["ivanov"].id,
                )
                db.add(doc)
                await db.commit()
                ingest_document.delay(str(doc.id))
                print(f"  + {ws_key}/{fname}: поставлен в очередь индексации")

    print("Сид завершён.")


if __name__ == "__main__":
    asyncio.run(seed())