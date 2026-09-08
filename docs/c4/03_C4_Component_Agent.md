# C4 Level 3 — Component Diagram: Backend API (Agent Graph)

**Разбираемый компонент (по C2):** Backend API (FastAPI) — его внутренняя часть **Agent Graph (LangGraph)**, включая модули Guardrails.

```mermaid
flowchart TB
    classDef component fill:#85BBF0,stroke:#5D82A6,color:#000
    classDef guard fill:#85BBF0,stroke:#5D82A6,color:#000
    classDef external fill:#999999,stroke:#8A8A8A,color:#fff,stroke-dasharray:4 3

    REQ["Вход: запрос + ACL<br/>session_id · user_id · workspace_id"]:::component

    subgraph CORE["Agent Graph (LangGraph) — внутри Backend API"]
        IG["Input Guardrails Node<br/>инъекции (эвристики) + PII-маскирование"]:::guard
        PL["Planner<br/>Plan-and-Solve: simple | complex → план ≤ 3 шагов"]:::component
        RET["Retriever Node<br/>hybrid_search: dense + sparse → RRF → rerank"]:::component
        RT["ReAct Reasoning Node<br/>Thought: достаточно данных? → выбор инструмента"]:::component
        SYN["Synthesize Node<br/>ответ строго по контексту + цитаты"]:::component
        OG["Output Guardrails Node<br/>grounding (NLI) + PII-маскирование"]:::guard

        subgraph MEM["Memory (контекст графа)"]
            direction LR
            WM["Working Memory<br/>AgentState (TypedState)"]:::component
            SM["Session Memory<br/>история диалога (PG)"]:::component
        end

        subgraph TOOLS["Tools (инструменты узлов)"]
            direction LR
            T1["hybrid_search<br/>Qdrant dense + PG sparse + RRF + rerank"]:::component
            T2["graph_query<br/>Knowledge Graph (PG)"]:::component
            T3["stt_transcribe<br/>GigaAM-сервис"]:::component
        end
    end

    LLM["LLM<br/>vLLM (GPU) / Yandex API<br/>[External System]"]:::external
    QDR["Qdrant<br/>[External System]"]:::external
    PGS["PostgreSQL 18<br/>[External System]"]:::external
    STTG["GigaAM STT<br/>[External System]"]:::external

    OUT["Выход: ответ + citations<br/>SSE-стриминг"]:::component

    REQ --> IG
    IG -->|"blocked → END"| OUT
    IG --> PL
    PL --> RET
    RET --> RT
    RT -->|"нужно ещё · < MAX_ITERATIONS"| RET
    RT -->|"достаточно"| SYN
    SYN --> OG
    OG -->|"pass"| OUT

    PL -.->|"инференс"| LLM
    RT -.->|"инференс"| LLM
    SYN -.->|"генерация"| LLM
    RET -.->|"dense-плечо"| QDR
    RET -.->|"sparse-плечо (pg_trgm)"| PGS
    T2 -.->|"факты графа"| PGS
    T3 -.->|"транскрипция"| STTG
    SM -.->|"история ≤ 6 сообщений"| SYN
```

## Поток выполнения (state machine)

```mermaid
flowchart LR
    classDef component fill:#85BBF0,stroke:#5D82A6,color:#000
    S([START]) --> IG["input_guardrails"]:::component
    IG -->|"blocked"| E([END])
    IG --> PL["planner"]:::component
    PL --> RET["retrieve"]:::component
    RET --> RR{"react_router:<br/>достаточно данных?"}
    RR -->|"нет · iterations < MAX"| TH["react_reasoning"]:::component
    TH --> RET
    RR -->|"да · или лимит"| SYN["synthesize"]:::component
    SYN --> OG["output_guardrails"]:::component
    OG --> E
```

## Комментарии

- **Planner** — первый LLM-вызов: классифицирует запрос (simple/complex); простой идёт в retrieval напрямую (экономия latency), сложный получает план до 3 шагов.
- **ReAct Reasoning** — LLM-оценка достаточности наблюдений; если векторное плечо вернуло мало (короткие наблюдения) и граф ещё не использовался — автоматически пробует `graph_query` один раз (реляционные вопросы решаются обходом связей).
- **Memory** — два уровня: рабочая память (AgentState графа: план, наблюдения, citations, tool_trace) и сессионная (последние ≤6 сообщений из PG, подмешиваются в planner/synthesize). Checkpointing — roadmap (Фаза 2).
- **Tools** — `hybrid_search` (Qdrant + pg_trgm sparse + RRF + rerank, с ACL pre-filter и расширением секций), `graph_query` (Knowledge Graph в PostgreSQL, фильтр по workspace), `stt_transcribe`. Каждый tool работает в границах ACL пользователя.
- **Guardrails** — узлы графа, а не внешние вызовы: input (инъекции + PII) до planner, output (grounding + PII) после synthesize; блокировка останавливает выполнение до выхода из графа.
- **LLM-провайдер** — vLLM (on-premise, GPU) либо Yandex API (fallback без GPU) — за графом, выбирается конфигурацией `LLM_PROVIDER`.