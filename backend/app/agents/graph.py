"""Когнитивная архитектура агента: LangGraph state machine.

Паттерн: Planner (Plan-and-Solve) + ReAct-цикл + Guardrails-узлы.
Линейные цепочки не используются — явный граф с условными переходами.
"""
import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, AsyncGenerator, Literal

import httpx
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from opentelemetry import trace
from pydantic import BaseModel

from app.core.config import get_settings
from app.db.models import Session as ChatSession
from app.guardrails.guardrails import input_guardrails, output_guardrails
from app.rag.retriever import hybrid_retrieve

tracer = trace.get_tracer("rag2.agent")

MAX_ITERATIONS = get_settings().max_react_iterations


# ---------- Состояние графа ----------


class AgentState(BaseModel):
    """Рабочая память агента (Working Memory)."""

    messages: Annotated[list, add_messages] = []
    user_input: str = ""
    acl: dict = {}
    workspace_id: str = ""
    session_history: list[dict] = []  # Session Memory: последние сообщения
    blocked: bool = False
    block_reason: str | None = None
    plan: list[str] = []
    observations: Annotated[list[str], operator.add] = []
    iterations: int = 0
    tool_trace: Annotated[list[dict], operator.add] = []
    answer: str | None = None
    citations: list[dict] = []
    final_text: str | None = None


# ---------- Узлы ----------


async def input_guardrails_node(state: AgentState) -> dict:
    """Контур 1: проверка запроса до планировщика."""
    verdict = await input_guardrails(state.user_input)
    if not verdict.allowed:
        return {"blocked": True, "block_reason": verdict.reason}
    return {"user_input": verdict.sanitized_text or state.user_input}


async def planner_node(state: AgentState) -> dict:
    """Plan-and-Solve: классификация сложности и декомпозиция.

    Простые вопросы идут напрямую в retrieval (экономия latency),
    сложные — получают план из нескольких шагов для ReAct-цикла.
    """
    with tracer.start_as_current_span("agent.planner") as span:
        history = "\n".join(
            f"{m['role']}: {m['content'][:200]}" for m in state.session_history[-6:]
        )
        prompt = (
            "Ты планировщик корпоративного ассистента. Оцени запрос пользователя.\n"
            f"История диалога:\n{history}\n\n"
            f"Запрос: {state.user_input}\n\n"
            "Ответь строго в JSON: "
            '{"complexity": "simple"|"complex", "plan": ["шаг 1", "шаг 2", ...]}\n'
            "simple — если достаточно одного поиска по документам; "
            "complex — если нужно сравнение, поиск нескольких сущностей, "
            "транскрипция или многошаговое рассуждение (максимум 3 шага)."
        )
        content = await _llm_complete(prompt, max_tokens=200)
        plan, complexity = _parse_plan(content)
        span.set_attribute("agent.complexity", complexity)
        span.set_attribute("agent.plan_steps", len(plan))
        return {"plan": plan}


async def retriever_node(state: AgentState) -> dict:
    """Один шаг поиска: гибридный retrieval с ACL pre-filter + rerank."""
    from qdrant_client import AsyncQdrantClient

    settings = get_settings()
    query = state.plan[0] if state.plan else state.user_input
    # AsyncQdrantClient не поддерживает async with — явное создание/закрытие
    qdrant = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    try:
        collection = f"ws_{state.workspace_id.replace('-', '_')}"
        try:
            chunks, metrics = await hybrid_retrieve(qdrant, collection, query, state.acl)
        except Exception as exc:
            # Ошибка retrieval не должна глушиться молча — логируем и отдаём пустой результат
            import logging

            from app.core.pii_sanitizer import sanitize_for_logs

            # В лог — маскированный запрос: ПДн из вопроса не утекает в Loki
            logging.getLogger("rag2.agent").warning(
                "retrieval failed: %s: %s (query: %s)",
                type(exc).__name__,
                exc,
                sanitize_for_logs(query),
            )
            chunks, metrics = [], {}
    finally:
        await qdrant.close()
    return {
            "observations": [_format_chunks(chunks)],
            "citations": [
                {
                    "chunk_id": c["chunk_id"],
                    "document_id": c["document_id"],
                    "title": c["title"],
                    "page": c["page"],
                }
                for c in chunks
            ],
            "tool_trace": [{"tool": "hybrid_search", "query": query, **metrics}],
        }


async def react_router(state: AgentState) -> Literal["tools", "synthesize"]:
    """Условный переход ReAct-цикла: продолжать или синтезировать."""
    if state.iterations >= MAX_ITERATIONS:
        return "synthesize"
    # MVP-эвристика: после двух поисков — к синтезу.
    # Полная ReAct-логика (LLM решает) — в react_reasoning_node Фазы 2.
    if len(state.observations) >= 2:
        return "synthesize"
    return "tools"


async def react_reasoning_node(state: AgentState) -> dict:
    """ReAct: Thought — анализ наблюдений и выбор следующего действия.

    Выбор инструмента: если векторный поиск малоинформативен (короткие/
    пустые наблюдения), пробуем граф знаний — реляционные вопросы
    («в каких отделах работает X») решаются обходом связей, а не поиском.
    """
    with tracer.start_as_current_span("agent.react_thought") as span:
        prompt = (
            "Ты Reason-модуль агента. План:\n"
            + "\n".join(f"- {p}" for p in state.plan)
            + "\n\nНаблюдения:\n"
            + "\n\n".join(state.observations)
            + f"\n\nИсходный вопрос: {state.user_input}\n"
            "Достаточно ли данных для ответа? Ответь строго JSON: "
            '{"enough": true|false, "next_query": "поисковый запрос для следующего шага"}'
        )
        content = await _llm_complete(prompt, max_tokens=150)
        span.set_attribute("agent.iteration", state.iterations)
        import json

        # Векторное плечо вернуло мало — пробуем граф знаний один раз
        first_obs = state.observations[0] if state.observations else ""
        if (
            len(first_obs) < 200
            and "graph_query" not in json.dumps(state.tool_trace)
            and state.iterations < MAX_ITERATIONS - 1
        ):
            try:
                result = await tool_graph_query(state)
                span.set_attribute("agent.tool", "graph_query")
                return {
                    "observations": result["observations"],
                    "tool_trace": result["tool_trace"],
                    "iterations": state.iterations + 1,
                }
            except Exception:  # noqa: BLE001 — граф недоступен, идём дальше
                pass

        try:
            parsed = json.loads(content[content.index("{") : content.rindex("}") + 1])
            if parsed.get("enough"):
                return {"iterations": state.iterations + 1}
            return {
                "plan": [parsed.get("next_query", state.plan[0] if state.plan else state.user_input)],
                "iterations": state.iterations + 1,
            }
        except (ValueError, KeyError):
            return {"iterations": MAX_ITERATIONS}


async def synthesize_node(state: AgentState) -> dict:
    """Генерация ответа с обязательными цитатами."""
    with tracer.start_as_current_span("agent.synthesize") as span:
        context = "\n\n".join(state.observations)
        history = "\n".join(
            f"{m['role']}: {m['content'][:300]}" for m in state.session_history[-6:]
        )
        prompt = (
            "Ты корпоративный ассистент. Отвечай ТОЛЬКО на основе приведённого "
            "контекста из документов. Обязательно указывай источники в формате "
            "[Источник: название, стр. N]. Если данных недостаточно — так и скажи.\n\n"
            f"История диалога:\n{history}\n\n"
            f"Контекст из документов:\n{context}\n\n"
            f"Вопрос: {state.user_input}\n\nОтвет:"
        )
        answer = await _llm_complete(prompt, max_tokens=1024)
        span.set_attribute("agent.answer_chars", len(answer))
        # Метаданные + маскированный превью вместо сырого ответа (ПДн)
        from app.core.pii_sanitizer import span_text_attrs

        span.set_attributes(span_text_attrs(answer, "agent.answer"))
        return {"answer": answer}


async def output_guardrails_node(state: AgentState) -> dict:
    """Контур 3: grounding + PII перед отправкой пользователю."""
    verdict = await output_guardrails(state.answer or "", state.citations)
    if not verdict.allowed:
        return {"final_text": verdict.reason, "citations": []}
    return {"final_text": verdict.sanitized_text}


# ---------- Инструменты (Tools Interface) ----------


async def tool_graph_query(state: AgentState) -> dict:
    """GraphRAG tool: запрос к Knowledge Graph (PostgreSQL MVP)."""
    from sqlalchemy import text as sql_text

    from app.core.security import SessionLocal

    query = state.plan[0] if state.plan else state.user_input
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                sql_text(
                    """
                    SELECT n.name, n.node_type, n.description, e.relation, n2.name AS related
                    FROM graph_nodes n
                    LEFT JOIN graph_edges e ON e.src_id = n.id
                    LEFT JOIN graph_nodes n2 ON n2.id = e.dst_id
                    WHERE n.workspace_id = CAST(:ws AS uuid)
                      AND (n.name % :q OR n.description % :q)
                    LIMIT 15
                    """
                ),
                {"ws": state.workspace_id, "q": query},
            )
        ).mappings()
        facts = [dict(r) for r in rows]
    obs = (
        "\n".join(
            f"{f['name']} ({f['node_type']}) — {f['relation']} → {f['related'] or '—'}"
            for f in facts
        )
        or "В графе знаний ничего не найдено."
    )
    return {"observations": [obs], "tool_trace": [{"tool": "graph_query", "results": len(facts)}]}


async def tool_stt_transcribe(state: AgentState) -> dict:
    """STT tool: транскрипция аудио через GigaAM-сервис."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=1800) as client:
        resp = await client.post(
            f"{settings.stt_url}/transcribe", json={"minio_key": state.plan[0] if state.plan else ""}
        )
    return {
        "observations": [resp.json().get("transcript", "")[:4000]],
        "tool_trace": [{"tool": "stt_transcribe"}],
    }


# ---------- Вспомогательные ----------


async def _llm_complete(prompt: str, max_tokens: int = 512) -> str:
    """Вызов LLM через провайдер-абстракцию (vLLM или Yandex GPT)."""
    from app.core.llm_provider import llm_chat

    return await llm_chat(
        [{"role": "user", "content": prompt}], max_tokens=max_tokens
    )


def _parse_plan(content: str) -> tuple[list[str], str]:
    import json

    try:
        start, end = content.index("{"), content.rindex("}") + 1
        parsed = json.loads(content[start:end])
        plan = parsed.get("plan") or [""]
        return [str(p) for p in plan[:3]], parsed.get("complexity", "simple")
    except ValueError:
        return [""], "simple"


def _format_chunks(chunks: list[dict]) -> str:
    if not chunks:
        return "В документах по этому запросу ничего не найдено."
    parts = []
    for c in chunks:
        page = f", стр. {c['page']}" if c.get("page") else ""
        parts.append(f"[Источник: {c['title']}{page}]\n{c['content']}")
    return "\n\n".join(parts)


# ---------- Сборка графа ----------


def build_graph():
    """LangGraph state machine:

    START → input_guardrails → planner → retrieve → (ReAct loop) → synthesize
          → output_guardrails → END
    """
    g = StateGraph(AgentState)
    g.add_node("input_guardrails", input_guardrails_node)
    g.add_node("planner", planner_node)
    g.add_node("retrieve", retriever_node)
    g.add_node("react_reasoning", react_reasoning_node)
    g.add_node("synthesize", synthesize_node)
    g.add_node("output_guardrails", output_guardrails_node)

    g.add_edge(START, "input_guardrails")
    g.add_conditional_edges(
        "input_guardrails",
        lambda s: END if s.blocked else "planner",
        {END: END, "planner": "planner"},
    )
    g.add_edge("planner", "retrieve")
    g.add_conditional_edges(
        "retrieve",
        react_router,
        {"tools": "react_reasoning", "synthesize": "synthesize"},
    )
    g.add_edge("react_reasoning", "retrieve")  # ReAct-петля
    g.add_edge("synthesize", "output_guardrails")
    g.add_edge("output_guardrails", END)
    return g.compile()


graph = build_graph()


# ---------- Публичный API для роутера ----------


async def run_agent(
    user_input: str, acl: dict, workspace_id: str, session_history: list[dict]
) -> AsyncGenerator[dict, None]:
    """Потоковое выполнение графа с событиями статуса для SSE."""
    initial: dict[str, Any] = {
        "user_input": user_input,
        "acl": acl,
        "workspace_id": workspace_id,
        "session_history": session_history,
        "messages": [HumanMessage(content=user_input)],
    }
    final_state = None
    async for event in graph.astream(initial, stream_mode="updates"):
        for node_name, update in event.items():
            if node_name == "planner":
                yield {"type": "status", "data": "Анализирую запрос…"}
            elif node_name == "retrieve":
                yield {"type": "status", "data": "Ищу в документах…"}
            elif node_name == "react_reasoning":
                yield {"type": "status", "data": "Уточняю поиск…"}
            elif node_name == "synthesize":
                yield {"type": "status", "data": "Формирую ответ…"}
        final_state = update

    # Финальный ответ (собираем состояние через aget_state при checkpointing)
    text = (final_state or {}).get("final_text") or "Не удалось получить ответ."
    yield {"type": "answer", "data": text}
    yield {
        "type": "citations",
        "data": (final_state or {}).get("citations", []),
    }
    yield {"type": "done", "data": ""}
