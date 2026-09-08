# Deployment Diagram

Физическое размещение: сегментация сети (DMZ / Internal / Data / Observability), GPU-ресурсы, балансировка, секреты.

## Диаграмма 1 — топология сегментов

Межсегментные потоки (кто куда имеет доступ):

```mermaid
flowchart LR
    classDef person fill:#08427B,stroke:#052E56,color:#fff
    classDef zone fill:#85BBF0,stroke:#5D82A6,color:#000

    U("Пользователи<br/>4 000 сотрудников"):::person

    subgraph DMZ["DMZ"]
        NG["nginx · TLS 443<br/>статика + reverse proxy"]:::zone
    end

    subgraph INT["Internal · app-1 (16 vCPU, 64 GB)"]
        APP["Backend FastAPI +<br/>Celery workers (ingestion, stt)<br/>+ Redis"]:::zone
    end

    subgraph DATA["Data · изолированная сеть"]
        STORE["PostgreSQL 18 · Qdrant · MinIO<br/>GPU-сервисы: vLLM, TEI, GigaAM<br/>Vault"]:::zone
    end

    subgraph OBS["Observability"]
        MON["OTel Collector →<br/>Prometheus · Loki · Jaeger ·<br/>Grafana"]:::zone
    end

    U -->|"HTTPS 443"| DMZ
    DMZ -->|"/api/v1 · SSE"| INT
    DMZ -->|"/grafana"| OBS
    INT -->|"SQL · вектора · S3 · GPU · секреты"| DATA
    INT -.->|"OTLP 4317/4318"| OBS
```

## Диаграмма 2 — потоки внутри стека

Кто какие сервисы вызывает (Control Plane → Data Plane):

```mermaid
flowchart LR
    classDef container fill:#85BBF0,stroke:#5D82A6,color:#000
    classDef db fill:#85BBF0,stroke:#5D82A6,color:#000

    API["Backend FastAPI"]:::container
    ING["Ingestion Worker<br/>(Celery)"]:::container
    STTW["STT Worker<br/>(Celery)"]:::container

    subgraph STORE["Хранилища и брокер"]
        direction LR
        RD[("Redis 8<br/>очереди · ACL-кэш")]:::db
        PG[("PostgreSQL 18<br/>+ pgvector")]:::db
        QD[("Qdrant<br/>вектора")]:::db
        S3[("MinIO<br/>файлы")]:::db
    end

    subgraph GPU["GPU-сервисы · 2× GPU 24 GB (профиль gpu)"]
        direction LR
        LLM["vLLM<br/>Qwen2.5-14B-AWQ · GPU 0"]:::container
        EMB["TEI<br/>BGE-M3 · GPU 0"]:::container
        RR["TEI<br/>reranker · GPU 0"]:::container
        GU["TEI<br/>Llama Guard · GPU 1"]:::container
        STTG["GigaAM v2<br/>STT · GPU 1"]:::container
    end

    VT["Vault<br/>secrets"]:::container
    OTC["OTel Collector"]:::container

    %% приём и очереди
    API --> RD
    RD -->|"ingestion"| ING
    RD -->|"stt"| STTW

    %% Backend
    API --> PG
    API --> QD
    API --> S3
    API --> LLM
    API --> GU

    %% Ingestion
    ING --> S3
    ING --> PG
    ING --> EMB
    ING --> QD

    %% STT
    STTW --> S3
    STTW --> STTG
    STTW --> LLM

    %% сквозное
    VT -.- API
    VT -.- ING
    API -.-> OTC
    ING -.-> OTC
    STTW -.-> OTC
```

## Сегментация и правила доступа

| Из | В | Правило |
|---|---|---|
| Пользователи | DMZ | Только HTTPS 443 (nginx) |
| DMZ | Internal | nginx → backend:8000 (SSE для /chat/stream) |
| Internal | Data | Backend/workers → Postgres:5432, Qdrant:6333, MinIO:9000, Vault:8200, GPU-сервисы |
| DMZ / Internet | Data | **Запрещено** (сеть data — `internal: true`) |
| Data | Internet | **Запрещено** (on-premise) |
| Internal | Observability | OTLP 4317/4318 → OTel Collector |
| DMZ | Observability | nginx → Grafana:3000; Jaeger UI:16686 |

## GPU-ресурсы (MVP: 1 сервер, 2 GPU)

| GPU | VRAM | Нагрузка |
|---|---|---|
| GPU 0 | 24 GB | vLLM Qwen2.5-14B-AWQ (~9 GB) + BGE-M3 (~2.2 GB) + reranker (~2.2 GB) + KV-cache |
| GPU 1 | 24 GB | Llama Guard (~5 GB) + GigaAM STT (~1.5 GB) + резерв под Фазу 2 |

GPU-сервисы запускаются профилем `gpu` (`docker compose --profile gpu up -d`); без GPU (локальная разработка на Mac) используется оверлей `docker-compose.local.yml` — LLM через внешний API (Yandex).

## Балансировка нагрузки

- nginx → backend (SSE-совместимое проксирование для стриминга токенов).
- Celery workers — конкурентность по очередям: ingestion ×2, stt ×1 (`--max-tasks-per-child=50` против утечек).
- vLLM — continuous batching + prefix caching внутри инстанса; при масштабировании — vLLM router на несколько GPU-нод.
- PostgreSQL / Qdrant — single-node в MVP

## Секреты

- Vault 1.21 в dev-mode (MVP); production Raft-режим — Фаза 2.
- Backend/workers получают креды БД/MinIO/Qdrant через окружение, чувствительные значения — из `.env`; в K8s — Vault Agent Injector.
- `.env`, TLS-сертификаты — вне git (`.gitignore`).
