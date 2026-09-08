# C4 Level 2 — Container Diagram

Контейнеры соответствуют сервисам `infra/docker-compose.yml` — фактический MVP-деплой.

Компоновка блоками: сверху вход (DMZ), ниже Control Plane, ниже Data Plane; стрелки между блоками — вертикальные.

```mermaid
flowchart TB
    classDef person fill:#08427B,stroke:#052E56,color:#fff
    classDef container fill:#85BBF0,stroke:#5D82A6,color:#000
    classDef db fill:#85BBF0,stroke:#5D82A6,color:#000
    classDef external fill:#999999,stroke:#8A8A8A,color:#fff,stroke-dasharray:4 3

    U("Сотрудник"):::person --> NG["nginx<br/>TLS · proxy"]:::container
    NG -->|"статика"| FE["Frontend<br/>React 19"]:::container
    NG -->|"API · SSE"| API["Backend FastAPI<br/>Agent Graph · Guardrails"]:::container
    NG ~~~ ING
    NG ~~~ STT

    ING["Ingestion Worker<br/>Celery"]:::container
    STT["STT Worker<br/>Celery"]:::container

    PG[("PostgreSQL 18<br/>RBAC · аудит · KG")]:::db
    QD[("Qdrant<br/>вектора + ACL")]:::db
    S3[("MinIO<br/>файлы")]:::db
    RDS[("Redis 8<br/>брокер Celery ·<br/>ACL-кэш")]:::db
    LLM["vLLM · GPU 0<br/>Qwen2.5-14B"]:::container
    EMB["TEI · GPU 0<br/>BGE-M3 · reranker"]:::container
    GUA["TEI · GPU 1<br/>Llama Guard"]:::container
    STTG["GigaAM · GPU 1<br/>STT"]:::container
    VLT["Vault<br/>секреты"]:::container

    API --> PG
    API --> QD
    API --> RDS
    API --> LLM
    API --> GUA

    ING --> S3
    ING --> EMB
    ING --> QD

    STT --> STTG
    STT --> LLM

    API -.-> VLT
    API -.->|"fallback"| YAN["Yandex LLM API<br/>[External System]"]:::external
```

## Комментарии

- **Agent Graph (LangGraph) и Guardrails — модули Backend API**, а не отдельные контейнеры: guardrails-узлы и planner/retrieve/react работают внутри процесса uvicorn; при масштабировании выносятся без изменения кода.
- **Redis** — брокер очередей Celery (задачи ingest/stt попадают в workers через него) и кэш ACL (TTL 60с); на диаграмме это отражено в подписи, стрелки брокера опущены для читаемости.
- **Упрощения потоков:** Ingestion Worker пишет метаданные в PostgreSQL, STT Worker сохраняет аудио в MinIO — эти стрелки опущены (дублируют уже показанные связи с теми же БД); полная матрица потоков — в Deployment Diagram (04).
- **Control Plane** (Backend, workers) — stateless, масштабируется репликами; **Data Plane** — в изолированной сети (`internal: true`), GPU-сервисы поднимаются профилем `gpu`.
- **Yandex LLM API** — fallback-провайдер для работы без GPU (локальная разработка); на целевом on-premise контуре не используется.