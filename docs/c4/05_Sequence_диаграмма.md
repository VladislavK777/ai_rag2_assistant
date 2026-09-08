# Sequence Diagram — обработка сложного запроса

Полный флоу: приём → входной контур безопасности → агентный цикл (Plan-and-Solve + ReAct) → синтез → выходной контур → стриминг.

Сценарий: «Найди оклад middle-разработчика» — запрос классифицируется как complex, требует multi-tool retrieval.

## Структура флоу

| Этап | Участники | Что происходит |
|---|---|---|
| 1. Приём | Frontend → nginx → Backend | SSE-соединение, JWT-аутентификация, проверка ACL |
| 2. Входной контур | Input Guardrails | Инъекции + PII (до агентного цикла) |
| 3. Агентный цикл | Planner, Retriever, ReAct | План → итерации поиска → инструмент graph_query |
| 4. Синтез | LLM, Output Guardrails | Генерация → grounding → маскирование PII |
| 5. Финализация | Backend → Frontend | Сохранение в PostgreSQL, SSE-стриминг |

## Диаграмма

```mermaid
sequenceDiagram
    autonumber
    actor U as Пользователь
    participant FE as Frontend
    participant NG as nginx
    participant API as Backend API
    participant RC as Redis (ACL-кэш)
    participant AG as Agent Graph (LangGraph)
    participant QD as Qdrant
    participant RR as Reranker
    participant PG as PostgreSQL (Graph)
    participant LL as LLM
    participant GO as Output Guardrails

    rect rgb(240, 248, 255)
        note over U,API: Этап 1 — приём запроса
        U->>FE: вопрос
        FE->>NG: POST /api/v1/chat/stream + JWT
        NG->>API: проксирование (SSE)
        API->>API: JWT → пользователь
        API->>RC: get_user_acl (TTL 60с)
        RC-->>API: workspace_ids, dept_ids
    end

    rect rgb(240, 255, 240)
        note over API,AG: Этап 2 — входной контур безопасности
        API->>AG: запуск графа (query, acl, workspace)
        AG->>AG: input_guardrails: инъекции (эвристики) + PII
        AG->>AG: planner: simple | complex → план
    end

    rect rgb(255, 248, 240)
        note over AG,RR: Этап 3 — ReAct итерация 1 (шаг 1 плана)
        AG->>QD: hybrid_search (dense + sparse, ACL pre-filter)
        QD-->>AG: top-20 кандидатов
        AG->>RR: rerank (cross-encoder)
        RR-->>AG: top-8 релевантных
    end

    rect rgb(255, 248, 240)
        note over AG,PG: Этап 3 — ReAct итерация 2 (данных не хватает)
        AG->>AG: react_reasoning: решить следующий шаг
        AG->>QD: hybrid_search (следующий шаг плана)
        QD-->>AG: top-20 → rerank → top-8
        AG->>PG: tool_graph_query (связи Knowledge Graph)
        PG-->>AG: факты графа
    end

    rect rgb(250, 240, 255)
        note over AG,GO: Этап 4 — синтез и выходной контур
        AG->>LL: генерация (чанки + факты графа + история)
        LL-->>AG: ответ с черновыми цитатами
        AG->>GO: grounding check (NLI)
        GO->>GO: маскирование PII (Presidio)
        GO-->>AG: pass
    end

    rect rgb(240, 248, 255)
        note over API,U: Этап 5 — финализация
        API->>PG: сохранение messages + citations
        API-->>NG: SSE: статусы, токены, citations
        NG-->>FE: SSE
        FE-->>U: ответ + ссылки на источники
    end

    note over AG,GO: Весь путь — под единым trace_id (OTel)
```

## Ключевые точки

1. **ACL до графа** — права пользователя собираются в Backend API (Redis-кэш TTL 60с) и передаются в агент как контекст; retrieval фильтрует чанки физически на уровне Qdrant.
2. **Guardrails встроены в граф** — input_guardrails и output_guardrails являются узлами LangGraph, а не внешними вызовами: блокировка запроса останавливает выполнение до planner'а.
3. **Plan-and-Solve + ReAct** — planner классифицирует сложность: simple идёт в retrieval напрямую, complex получает план (до 3 шагов); максимум итераций — `MAX_ITERATIONS` из конфигурации.
4. **Rerank после каждого retrieval** — top-8 после cross-encoder'а существенно точнее top-20 по гибридному поиску.
5. **Инструменты** — `hybrid_search` (Qdrant) и `graph_query` (Knowledge Graph в PostgreSQL); STT-транскрипция доступна как отдельный tool.
6. **Стриминг** — статусы этапов и токены ответа уходят пользователю по SSE по мере выполнения графа.
