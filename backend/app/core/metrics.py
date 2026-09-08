"""Кастомные Prometheus-метрики RAG2 (дашборд SLO Overview).

Все метрики объявлены здесь в едином реестре; инкрементируются из
агентского графа (TTFT, guardrails) и роутеров. Экспорт — через
app.main: app.mount("/metrics", make_asgi_app()).
"""
from prometheus_client import Counter, Histogram

# TTFT: время от получения вопроса до первого байта ответа (SLO p95 3.1c)
rag2_ttft_seconds = Histogram(
    "rag2_ttft_seconds",
    "Time to first token of chat answer",
    buckets=(0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0),
)

# Решения guardrails (контур 1 — input; для спанов output — тот же счётчик
# с лейблом stage)
rag2_guardrails_decisions_total = Counter(
    "rag2_guardrails_decisions_total",
    "Guardrails allow/deny decisions",
    ["decision", "stage"],
)

# Исходящие LLM-токены (локальный провайдер или Yandex)
rag2_llm_tokens_total = Counter(
    "rag2_llm_tokens_total",
    "LLM tokens processed",
    ["direction"],  # prompt | completion
)

# HTTP-запросы API: латентность и счётчик по роутам
# (панели RPS / Latency p50-p95 / Error rate дашборда SLO)
http_requests_total = Counter(
    "http_requests_total",
    "HTTP requests",
    ["service", "route", "status"],
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["service", "route"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)
