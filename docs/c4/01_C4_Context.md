# C4 Level 1 — Context Diagram

Система в окружении: пользователи, администратор, аудитор и внешние системы.

```mermaid
flowchart TB
    classDef person fill:#08427B,stroke:#052E56,color:#fff
    classDef system fill:#1168BD,stroke:#0B4884,color:#fff
    classDef external fill:#999999,stroke:#8A8A8A,color:#fff,stroke-dasharray:4 3

    U("Сотрудник"):::person
    A("Администратор<br/>workspace и права"):::person
    I("Аудитор ИБ<br/>аудит-лог"):::person

    RS["RAG2 — корпоративная AI-платформа<br/>[Software System]<br/>RAG-поиск по документам · транскрипция встреч"]:::system

    subgraph EXT["Внешние системы"]
        direction LR
        LDAP["AD / LDAP<br/>[External System]<br/>аутентификация"]:::external
        FS["Файловые хранилища<br/>[External System]<br/>SharePoint · Samba"]:::external
        YAN["Yandex LLM API<br/>[External System]<br/>LLM без GPU (опция)"]:::external
    end

    U -->|"вопросы · документы"| RS
    RS -->|"ответы с цитатами<br/>транскрипты"| U
    A -->|"управление"| RS
    I -->|"просмотр"| RS

    LDAP -.->|"SSO / JWT (Фаза 2)"| RS
    FS -.->|"синхронизация (Фаза 2)"| RS
    YAN -.->|"fallback LLM (опция)"| RS
```

## Комментарии

- **Персоны** — сотрудник (основной сценарий: вопрос → ответ с цитатами), администратор (workspace, права, пользователи), аудитор ИБ (просмотр append-only аудит-лога).
- **AD / LDAP и файловые хранилища** — интеграции целевой архитектуры (Фаза 2): сейчас аутентификация — собственная (JWT, bcrypt), документы загружаются через UI/ingestion API. Показаны на контексте, чтобы границы системы были видны сразу.
- **Yandex LLM API** — фактическая внешняя система в MVP при работе без GPU: LLM-инференс уходит наружу (fallback-провайдер). На целевом on-premise контуре заменяется внутренним vLLM и из списка исчезает.
- **Календарь встреч (Exchange)** убран из MVP-контура: приём аудио — через загрузку файла в UI, а не интеграцию с календарём.