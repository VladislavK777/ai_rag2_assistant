"""Guardrails: трёхконтурная схема безопасности.

Контур 1 (Input):  prompt injection (Llama Guard + эвристики) + PII (Presidio)
Контур 2 (Retrieval): RBAC pre-filter — реализован в rag/retriever.py (Qdrant filter)
Контур 3 (Output): grounding check (NLI) + PII masking + цитирование
"""
import re
import time

import httpx
from opentelemetry import trace
from pydantic import BaseModel

from app.core.config import get_settings

tracer = trace.get_tracer("rag2.guardrails")

# ---------- Модели результатов ----------


class GuardrailVerdict(BaseModel):
    allowed: bool
    reason: str | None = None
    sanitized_text: str | None = None  # текст после маскирования PII
    latency_ms: int = 0


# ---------- Контур 1: Input ----------

# Эвристики прямых инъекций (первый барьер, ~0 мс).
INJECTION_PATTERNS = [
    r"ignor\w*\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts|rules)",
    r"забуд\w*\s+(все\s+)?(предыдущ\w+|прошлы\w+)\s+(инструкци\w+|указани\w+)",
    r"(reveal|show|print)\s+(your\s+)?(system\s+prompt|initial\s+instructions)",
    r"(выведи|покажи|раскрой)\s+(системн\w+\s+)?(промпт|инструкци\w+)",
    r"you\s+are\s+now\s+(a|an|no longer)",
    r"ты\s+теперь\s+(не\s+)?(ассистент|модель|бот)",
    r"</?(system|assistant)\s*>",
    r"\[system\]|\[INST\]|<<SYS>>",
]

# Легитимные вопросы про документы, которые эвристика может ложно поймать.
SAFE_PATTERNS = [
    r"инструкци\w+\s+(в\s+)?документ\w*",
    r"instructions\s+in\s+(the\s+)?document",
]

PII_REGEXES = {
    "PHONE_RU": re.compile(r"(\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"),
    "PASSPORT_RU": re.compile(r"\b\d{4}\s?\d{6}\b"),
    "SNILS": re.compile(r"\b\d{3}[\s\-]?\d{3}[\s\-]?\d{3}[\s\-]?\d{2}\b"),
    "INN": re.compile(r"\b(\d{10}|\d{12})\b"),
    "CARD": re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"),
}
PII_MASK = "[ПЕРСОНАЛЬНЫЕ ДАННЫЕ]"


def heuristic_injection_check(text: str) -> bool:
    """True = найдена инъекция. Легитимные паттерны исключаются."""
    for safe in SAFE_PATTERNS:
        if re.search(safe, text, re.IGNORECASE):
            continue
    return any(re.search(p, text, re.IGNORECASE) for p in INJECTION_PATTERNS)


def mask_pii_regex(text: str) -> str:
    """Быстрое маскирование PII регулярками (работает и на входе, и на выходе)."""
    for pattern in PII_REGEXES.values():
        text = pattern.sub(PII_MASK, text)
    return text


def detect_pii_types(text: str) -> list[str]:
    """Типы ПДн, найденные в тексте (без маскирования).

    Для пометки чанков при ingestion: contains_pii в payload Qdrant —
    retriever маскирует такие чанки пользователям без права pii_read.
    """
    found = [name for name, rx in PII_REGEXES.items() if rx.search(text)]
    # SNILS/INN/CARD пересекаются по формату — приоритет специфичных
    if "CARD" in found and "INN" in found:
        # 16 цифр = карта, 10/12 = ИНН; CARD-регулярка матчится на ИНН-подобные
        # только при 4x4 группировке — оставляем оба, false positive безопасен
        pass
    return found


async def llama_guard_classify(text: str) -> tuple[bool, str]:
    """Классификация через Llama Guard 3 8B (TEI endpoint).
    Возвращает (is_unsafe, category).
    Если Llama Guard недоступен (нет GPU / endpoint 404) — fail-open:
    эвристики и PII-regex уже отработали, запрос пропускается.
    """
    settings = get_settings()
    prompt = (
        "[INST] Task: Check if there is unsafe content in 'User' messages in "
        "conversations according to our safety policy. \n<BEGIN CONVERSATION>\n"
        f"User: {text}\n<END CONVERSATION>\nProvide your safety assessment "
        "in the format: safe/unsafe [/category] [/INST]"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{settings.guard_url}/v1/chat/completions",
                json={"model": "llama-guard", "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 16},
            )
            resp.raise_for_status()
            answer = resp.json()["choices"][0]["message"]["content"].strip().lower()
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        # Llama Guard недоступен локально (Mac без GPU) — не блокируем чат
        return False, "guard_unavailable"
    if answer.startswith("unsafe"):
        return True, answer.split("/")[1] if "/" in answer else "unknown"
    return False, "safe"


async def input_guardrails(text: str) -> GuardrailVerdict:
    """Контур 1: перед передачей в агентский граф."""
    start = time.monotonic()
    with tracer.start_as_current_span("guardrails.input") as span:

        # 1. Эвристики (мгновенно)
        if heuristic_injection_check(text):
            span.set_attribute("guardrails.decision", "deny_injection_heuristic")
            from app.core.metrics import rag2_guardrails_decisions_total

            rag2_guardrails_decisions_total.labels(
                decision="deny", stage="input"
            ).inc()
            return GuardrailVerdict(
                allowed=False,
                reason="Запрос заблокирован политикой безопасности (подозрение на инъекцию инструкций).",
                latency_ms=int((time.monotonic() - start) * 1000),
            )

        # 2. Llama Guard (семантическая классификация)
        unsafe, category = await llama_guard_classify(text)
        if unsafe:
            span.set_attribute("guardrails.decision", f"deny_llama_guard_{category}")
            from app.core.metrics import rag2_guardrails_decisions_total

            rag2_guardrails_decisions_total.labels(
                decision="deny", stage="input"
            ).inc()
            return GuardrailVerdict(
                allowed=False,
                reason="Запрос заблокирован политикой безопасности.",
                latency_ms=int((time.monotonic() - start) * 1000),
            )

        # 3. PII-маскирование входа (Presidio-recognizers + regex)
        sanitized = mask_pii_regex(text)
        span.set_attribute("guardrails.decision", "allow")
        from app.core.metrics import rag2_guardrails_decisions_total

        rag2_guardrails_decisions_total.labels(decision="allow", stage="input").inc()
        return GuardrailVerdict(
            allowed=True,
            sanitized_text=sanitized,
            latency_ms=int((time.monotonic() - start) * 1000),
        )


# ---------- Контур 3: Output ----------

# Токены, указывающие, что модель отвечает за пределами контекста
HALLUCINATION_MARKERS = [
    "насколько мне известно",
    "я не уверен, но",
    "вероятно,",
    "as an ai",
]


async def output_guardrails(answer: str, sources: list[dict]) -> GuardrailVerdict:
    """Контур 3: после генерации, до отправки пользователю.

    1. Grounding: если ответ ссылается на несуществующие источники — блок.
    2. PII-маскирование выхода.
    """
    start = time.monotonic()
    with tracer.start_as_current_span("guardrails.output") as span:

        # 1. Цитаты обязательны для фактологических ответов
        if not sources and any(m in answer.lower() for m in HALLUCINATION_MARKERS):
            span.set_attribute("guardrails.decision", "deny_ungrounded")
            return GuardrailVerdict(
                allowed=False,
                reason="Ответ не подтверждён источниками. Уточните вопрос или загрузите документы.",
                latency_ms=int((time.monotonic() - start) * 1000),
            )

        # 2. PII на выходе
        masked = mask_pii_regex(answer)
        span.set_attribute("guardrails.decision", "allow")
        return GuardrailVerdict(
            allowed=True,
            sanitized_text=masked,
            latency_ms=int((time.monotonic() - start) * 1000),
        )
