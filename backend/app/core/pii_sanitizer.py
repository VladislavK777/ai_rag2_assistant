"""Санитизация текста для observability-каналов (Loki, Jaeger).

Промпты и ответы содержат ПДн; логи и трейсы доступны шире, чем БД
(SRE, дашборды, дампы), поэтому перед записью текст маскируется.
Регулярки те же, что в guardrails (PII_REGEXES) — единый источник правды.
"""
import re

from app.guardrails.guardrails import PII_REGEXES, mask_pii_regex

# Длинные тексты в логи не пишем целиком — только маскированный хвост
MAX_LOG_TEXT = 200

_WS_RE = re.compile(r"\s+")


def sanitize_for_logs(text: str | None, max_len: int = MAX_LOG_TEXT) -> str:
    """Маскирование ПДн + усечение для безопасной записи в логи/спаны.

    None/пустота → пустая строка; многострочный текст схлопывается.
    """
    if not text:
        return ""
    masked = mask_pii_regex(_WS_RE.sub(" ", text))
    return masked[:max_len] + ("…" if len(masked) > max_len else "")


def span_text_attrs(text: str | None, prefix: str) -> dict[str, str]:
    """Атрибуты спана вместо сырого текста: маскированный фрагмент + метрики.

    Сырой промпт/ответ в Jaeger не попадает — только метаданные и
    маскированный превью.
    """
    if not text:
        return {f"{prefix}.chars": 0}
    return {
        f"{prefix}.chars": len(text),
        f"{prefix}.preview": sanitize_for_logs(text, 80),
    }
