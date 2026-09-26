# RAG2 — Корпоративная AI-платформа (MVP ядра)
##[Ссылка на виледопрезентацию](https://cloud.mail.ru/public/bcqv/sCsKaqhu8)

AI-ассистент для 4 000+ сотрудников на полностью автономной (On-premise) инфраструктуре.
Два ключевых сценария: **поиск по корпоративным документам (Advanced RAG)** и **транскрипция встреч (STT)**.
Канал доступа — только веб-UI (Telegram-чат исключён из скоупа).

## Что в репозитории

| Каталог | Содержимое |
|---|---|
| `docs/` | Полный пакет проектной документации (ADR-000 и ADR, C4-модель, ТЗ, сайзинг, нагрузочный отчёт, ROI) |
| `docs/adr/` | Architecture Decision Records: ADR-000 - ADR-007 |
| `docs/c4/` | Диаграммы C4 L1–L3, Deployment, Sequence, ER (Mermaid) |
| `infra/` | Docker Compose для MVP + Helm values / Terraform-схема для Yandex Cloud |
| `backend/` | Python 3.12 / FastAPI / LangGraph — агенты, RAG, Guardrails, API |
| `frontend/` | Веб-UI чат (стриминг ответов, SSE), React + Vite |

## Точки входа в документацию

0. **[docs/ADR-000 — Контекст и обоснование архитектуры](docs/adr/ADR-000_контекст_и_обоснование.md)** — главный документ: зачем, почему On-premise, ключевые решения, Trade-off analysis.
1. **[docs/ТЗ — Техническое задание](docs/ТЗ_Техническое_задание.md)** — функциональные и нефункциональные требования, критерии приёмки.
2. **[docs/c4/ — Архитектурные диаграммы](docs/c4/)** — C4 L1/L2/L3, Deployment, Sequence, ER.
3. **[docs/Сайзинг_и_масштабирование.md](docs/Сайзинг_и_масштабирование.md)** — расчёт GPU/CPU/RAM, план масштабирования до прода.
4. **[docs/Нагрузочный_отчёт.md](docs/Нагрузочный_отчёт.md)** — RPS/латентность на тестовом железе, методика (k6).
5. **[docs/Экономический_эффект_ROI.md](docs/Экономический_эффект_ROI.md)** — TCO, ROI, payback.
6. **[docs/adr/ADR-006 — Guardrails](docs/adr/ADR-006_Guardrails_и_безопасность.md)** — детальное описание защитных контуров.
7. **[docs/OWASP_Top10_LLM.md](docs/OWASP_Top10_LLM.md)** — соответствие OWASP Top 10 для LLM-приложений.
8. **[docs/adr/ADR-005 — Наблюдаемость](docs/adr/ADR-005_Наблюдаемость_и_логирование.md)** — OTel-трейсинг, метрики, логи.

## Быстрый старт MVP (On-premise / один сервер)

# 1. Клонировать и войти
cd RAG2

# 2. Конфигурация
cp infra/.env.example infra/.env   # заполнить секреты (либо использовать Vault)
# минимально: POSTGRES_PASSWORD, KEYCLOAK_ADMIN_PASSWORD, MINIO_ROOT_PASSWORD

# 3. Поднять весь стек (включая Keycloak с realm rag2)
cd infra && docker compose up -d

# 4. Проверка здоровья
curl http://localhost:8000/health       # backend
curl http://localhost:8000/metrics      # Prometheus-метрики
curl http://localhost:8080/health/ready # Keycloak (SSO)
# Grafana: http://localhost:3000 (admin / из .env)
# Qdrant:  http://localhost:6333/dashboard

# 5. Справочники и права (департаменты ДЕП-хххх, workspace'ы, ACL)
docker compose exec backend python -m app.db.bootstrap
docker compose exec backend python -m app.db.seed_demo

# 6. Открыть UI — вход через SSO (демо-пользователи в Keycloak, пароль Demo-2026!)
open https://localhost
# Администратор системы: Иванов Иван Иванович (ivanov@company.ru) — роль ADMIN

Подробная инструкция по развёртыванию: **[infra/README.md](infra/README.md)** (включая вариант Yandex Cloud).

## Стек (обоснование — в ADR)

| Слой | Технология | ADR |
|---|---|---|
| LLM Serving | vLLM 0.28 + Qwen3-8B-Instruct-AWQ | [ADR-002](docs/adr/ADR-002_LLM_Serving.md) |
| Embeddings | BGE-M3 (dense + sparse, 1024d) | [ADR-003](docs/adr/ADR-003_Vector_DB_и_Embeddings.md) |
| Reranker | BGE-reranker-v2-m3 | [ADR-003](docs/adr/ADR-003_Vector_DB_и_Embeddings.md) |
| Vector DB | Qdrant 1.19 (self-hosted) | [ADR-003](docs/adr/ADR-003_Vector_DB_и_Embeddings.md) |
| Orchestration | LangGraph 1.2 (Stateful Agent) | [ADR-004](docs/adr/ADR-004_Оркестрация_и_когнитивная_архитектура.md) |
| STT | GigaAM v2 CTC (русский, NVIDIA NeMo) | [ADR-002](docs/adr/ADR-002_LLM_Serving.md) |
| Guardrails | Llama Guard 3 8B + Presidio PII + regex | [ADR-006](docs/adr/ADR-006_Guardrails_и_безопасность.md) |
| API Gateway | nginx 1.29 (TLS, маршрутизация, rate limiting) | [ADR-001](docs/adr/ADR-001_Общий_стиль_архитектуры.md) |
| SSO / Аутентификация | Keycloak (OIDC, PKCE; LDAP federation в контуре) — realm rag2, client rag2_client | [ADR-001](docs/adr/ADR-001_Общий_стиль_архитектуры.md) |
| БД | PostgreSQL 18 (метаданные, RBAC, сессии) | [ADR-007](docs/adr/007_Модель_данных.md) |
| Observability | OpenTelemetry + Prometheus + Grafana + Loki | [ADR-005](docs/adr/ADR-005_Наблюдаемость_и_логирование.md) |
| Secrets | HashiCorp Vault | [ADR-001](docs/adr/ADR-001_Общий_стиль_архитектуры.md) |
| Backend | Python 3.13, FastAPI, Pydantic v2 | [ADR-001](docs/adr/ADR-001_Общий_стиль_архитектуры.md) |
| Frontend | React 19 + Vite 8 + TypeScript | [ADR-001](docs/adr/ADR-001_Общий_стиль_архитектуры.md) |

## Критерии приёмки (как проверяем)

- **Критично:** User B не получает ответ по секретному документу User A → тест `backend/tests/test_rbac_access.py`, RBAC-фильтр на уровне Qdrant retrieval (pre-filter), не пост-фильтрация.
- **Критично:** Control Plane (агенты/API) физически отделён от Data Plane (БД, модели, векторная БД) → Deployment-диаграмма + сетевые политики.
- **Критично:** Оркестрация через LangGraph (state machine), не линейные скрипты → `backend/app/agents/graph.py`.
- Deployment-диаграмма и Data Flow присутствуют → `docs/c4/`.
- Streaming-ответов (SSE) — реализовано → `backend/app/api/routes_chat.py`.

## Тесты и нагрузочный прогон

# Unit/интеграционные
cd backend && pytest -v

# Нагрузочный — методика и результаты в docs/Нагрузочный_отчёт.md
