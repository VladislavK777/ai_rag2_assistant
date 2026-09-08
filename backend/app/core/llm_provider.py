"""Абстракция LLM-провайдера: локальный vLLM (on-prem) или внешний Yandex GPT.

Переключение — одной переменной окружения LLM_PROVIDER (local | yandex).
Оба провайдера используют OpenAI-compatible протокол, поэтому весь остальной
код (агент, STT-резюме) не знает, какой провайдер активен.

Безопасность: API-ключ Yandex — секрет. Хранится в .env (chmod 600) в MVP
или в Vault KV в целевом режиме. Не логируется, не попадает в трейсы.
"""
import httpx
from opentelemetry import trace

from app.core.config import get_settings

tracer = trace.get_tracer("rag2.llm")


async def llm_chat(
    messages: list[dict],
    max_tokens: int = 512,
    temperature: float = 0.2,
) -> str:
    """Единая точка вызова LLM для всего backend.

    messages — список {"role": ..., "content": ...} (OpenAI-формат).
    """
    settings = get_settings()

    with tracer.start_as_current_span("llm.chat") as span:
        span.set_attribute("llm.provider", settings.llm_provider)

        if settings.llm_provider == "yandex":
            if not settings.yandex_api_key:
                raise RuntimeError("LLM_PROVIDER=yandex, но YANDEX_API_KEY не задан")
            url = f"{settings.yandex_base_url}/chat/completions"
            headers = {
                "Authorization": f"Api-Key {settings.yandex_api_key}",
                **(
                    {"x-folder-id": settings.yandex_folder_id}
                    if settings.yandex_folder_id
                    else {}
                ),
            }
            model = settings.yandex_model
        else:
            url = f"{settings.vllm_base_url}/chat/completions"
            headers = {}
            model = settings.vllm_model

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]

        # Учёт токенов для SLO-дашборда (оценка: ~4 символа/токен для ru/en)
        try:
            from app.core.metrics import rag2_llm_tokens_total

            prompt_chars = sum(len(m.get("content", "")) for m in messages)
            rag2_llm_tokens_total.labels(direction="prompt").inc(prompt_chars // 4)
            rag2_llm_tokens_total.labels(direction="completion").inc(len(content) // 4)
        except Exception:  # noqa: BLE001 — метрики не должны ломать чат
            pass

        # В спан уходят метаданные и маскированный превью — сырой промпт
        # с ПДн в Jaeger не попадает
        from app.core.pii_sanitizer import span_text_attrs

        span.set_attributes(
            span_text_attrs(
                " ".join(m.get("content", "") for m in messages), "llm.prompt"
            )
        )
        span.set_attributes(span_text_attrs(content, "llm.response"))
        return content
