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
        KC["Keycloak (SSO)<br/>[External System]<br/>аутентификация · realm rag2<br/>LDAP federation"]:::external
        LDAP["AD / LDAP<br/>[External System]<br/>источник идентичности"]:::external
        FS["Файловые хранилища<br/>[External System]<br/>SharePoint · Samba"]:::external
        YAN["Yandex LLM API<br/>[External System]<br/>LLM без GPU (опция)"]:::external
    end

    U -->|"вход через SSO · вопросы · документы"| KC
    KC -->|"JWT (claims: ТАБ, ДЕП, ROLE)"| RS
    RS -->|"ответы с цитатами<br/>транскрипты"| U
    A -->|"управление"| RS
    I -->|"просмотр"| RS

    KC -.->|"federation: пользователи, группы"| LDAP
    FS -.->|"синхронизация (Фаза 2)"| RS
    YAN -.->|"fallback LLM (опция)"| RS
```

## Комментарии

- **Персоны** — сотрудник (основной сценарий: вопрос → ответ с цитатами), администратор (workspace, права; пользователи управляются в Keycloak), аудитор ИБ (просмотр append-only аудит-лога).
- **Keycloak** — единственная точка аутентификации (SSO). Токен содержит claims из LDAP: `employee_no` (ТАБ-хххх), `email`, `full_name`, `dept_code` (ДЕП-хххх), `role: [ADMIN|EMPLOYEE]`. Backend — resource server: валидация JWT по JWKS, локальных паролей нет.
- **AD / LDAP** — источник идентичности через federation Keycloak; в dev/демо пользователи создаются прямо в realm.
- **Файловые хранилища** — интеграция Фазы 2: документы загружаются через UI/ingestion API.
- **Yandex LLM API** — фактическая внешняя система в MVP при работе без GPU: LLM-инференс уходит наружу (fallback-провайдер). На целевом on-premise контуре заменяется внутренним vLLM и из списка исчезает.
- **Календарь встреч (Exchange)** убран из MVP-контура: приём аудио — через загрузку файла в UI, а не интеграцию с календарём.