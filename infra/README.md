# Инфраструктура RAG2

## Состав

| Файл/каталог | Назначение |
|---|---|
| `docker-compose.yml` | Полный стек MVP (on-premise, 1 сервер, GPU-сервисы под профилем `gpu`) |
| `.env.example` | Шаблон конфигурации |
| `nginx/` | TLS-терминация и маршрутизация DMZ |
| `prometheus/` | Конфиг скрейпинга метрик |
| `grafana/provisioning/` | Автоматические дашборды и datasource |
| `init/` | Инициализация Qdrant-коллекций, бакетов MinIO |
| `terraform-yandex/` | (Опцион) Развёртывание в Yandex Cloud |

## Быстрый старт

**На сервере с NVIDIA GPU (целевая схема):**

```bash
# Требования: Docker 24+, docker compose v2, nvidia-container-toolkit

cp .env.example .env
# Отредактируйте: POSTGRES_PASSWORD, JWT_SECRET_KEY, MINIO_ROOT_PASSWORD, GRAFANA_ADMIN_PASSWORD
# LLM_PROVIDER=local (по умолчанию)

docker compose --profile gpu up -d

# Проверка
docker compose ps
curl http://localhost:8000/health
```

## Порты (внутренняя сеть / публикуемые)

| Сервис | Порт | Публикуется |
|---|---|---|
| nginx (DMZ) | 80/443 | **да** |
| frontend (dev-режим) | 5173 | да (только dev; в prod раздаётся nginx'ом) |
| backend API | 8000 | через nginx |
| Grafana | 3000 | **да** (за VPN!) |
| Qdrant | 6333 | нет (internal) |
| PostgreSQL | 5432 | нет |
| MinIO | 9000/9001 | нет (консоль — через VPN) |
| Vault | 8200 | нет |
| vLLM | 8010 | нет |
| OTel Collector | 4317/4318 | нет |

## Порядок первого запуска

1. `docker compose up -d postgres redis minio vault qdrant` — инфраструктура данных.
2. `docker compose --profile gpu up -d vllm embedding guardrail stt` — GPU-сервисы (на Mac пропускается, вместо них CPU-embedding из local-override).
3. `docker compose up -d backend ingestion-worker stt-worker frontend nginx` — приложения.
4. `docker compose up -d otel-collector prometheus grafana loki jaeger` — наблюдаемость.
5. Создать администратора: `docker compose exec backend python -m app.db.bootstrap` (создаёт admin из переменных `ADMIN_EMAIL`/`ADMIN_PASSWORD`).
6. Загрузить тестовые документы через UI или API.

## Yandex Cloud (Фаза 3)

- `terraform-yandex/`: Managed K8s, Managed PostgreSQL, Object Storage, Lockbox, GPU node-group.
- Изменения кода не требуются — только провижининг и переменные окружения.
- MinIO → Object Storage (S3 API совместим), Vault → Lockbox, Redis → Managed Redis.

## Безопасность эксплуатации

- Grafana/MinIO-консоль — только из VPN-сегмента.
- TLS-сертификаты: `nginx/certs/` (в MVP — self-signed, сгенерировать: `make certs`).
- Секреты: в MVP — `.env` (chmod 600), целевое состояние — Vault KV v2.
- Бэкапы: `./scripts/backup.sh` (pg_dump + Qdrant snapshot + MinIO mirror) — cron daily.