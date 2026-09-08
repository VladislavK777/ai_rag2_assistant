# RAG2 — Корпоративная AI-платформа (MVP ядра)

AI-ассистент для 4 000+ сотрудников на полностью автономной (On-premise) инфраструктуре.
Два ключевых сценария: **поиск по корпоративным документам (Advanced RAG)** и **транскрипция встреч (STT)**.
Канал доступа — только веб-UI (Telegram-чат исключён из скоупа).

## Что в репозитории

| Каталог | Содержимое |
|---|---|
| `docs/` | Полный пакет проектной документации (ADR-000 и ADR, C4-модель, ТЗ, сайзинг, нагрузочный отчёт, ROI) |
| `docs/adr/` | Architecture Decision Records: [ADR-000](docs/adr/ADR-000_контекст_и_обоснование.md) - [ADR-007](docs/adr/ADR-007_Модель_данных.md) |
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

bash
# 1. Клонировать и войти
cd RAG2

# 2. Конфигурация
cp infra/.env.example infra/.env   # заполнить секреты (либо использовать Vault)
# минимально: POSTGRES_PASSWORD, JWT_SECRET_KEY, ADMIN_PASSWORD

# 3. Поднять весь стек
cd infra && docker compose up -d

# 4. Проверка здоровья
curl http://localhost:8000/health       # backend
curl http://localhost:8000/metrics      # Prometheus-метрики
# Grafana: http://localhost:3000 (admin / из .env)
# Qdrant:  http://localhost:6333/dashboard

# 5. Загрузить демо-документы
curl -X POST http://localhost:8000/api/v1/ingest/upload \
  -H "Authorization: Bearer $TOKEN" -F "file=@report.pdf" -F "collection=general"

# 6. Открыть UI
open http://localhost:5173

Подробная инструкция по развёртыванию: **[infra/README.md](infra/README.md)** (включая вариант Yandex Cloud).

## Стек (обоснование — в ADR)

| Слой | Технология | ADR |
|---|---|---|
| LLM Serving | vLLM 0.28 + Qwen2.5-14B-Instruct-AWQ | [ADR-002](docs/adr/ADR-002_LLM_Serving.md) |
| Embeddings | BGE-M3 (dense + sparse, 1024d) | [ADR-003](docs/adr/ADR-003_Vector_DB_и_Embeddings.md) |
| Reranker | BGE-reranker-v2-m3 | [ADR-003](docs/adr/ADR-003_Vector_DB_и_Embeddings.md) |
| Vector DB | Qdrant 1.19 (self-hosted) | [ADR-003](docs/adr/ADR-003_Vector_DB_и_Embeddings.md) |
| Orchestration | LangGraph 1.2 (Stateful Agent) | [ADR-004](docs/adr/ADR-004_Оркестрация_и_когнитивная_архитектура.md) |
| STT | GigaAM v2 CTC (русский, NVIDIA NeMo) | [ADR-002](docs/adr/ADR-002_LLM_Serving.md) |
| Guardrails | Llama Guard 3 8B + Presidio PII + regex | [ADR-006](docs/adr/ADR-006_Guardrails_и_безопасность.md) |
| API Gateway | Kong 3.x | [ADR-001](docs/adr/ADR-001_Общий_стиль_архитектуры.md) |
| БД | PostgreSQL 18 (метаданные, RBAC, сессии) | [ADR-007](docs/adr/ADR-007_Модель_данных.md) |
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

bash
# Unit/интеграционные
cd backend && pytest -v

# Нагрузочный (k6) — методика и результаты в docs/Нагрузочный_отчёт.md
k6 run backend/tests/load/k6_chat.js
