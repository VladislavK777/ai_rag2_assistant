"""Экспортёр глубины очереди Celery для Prometheus.

Лёгкий HTTP-сервер на :9808 (метрика rag2_ingestion_queue_depth).
Llama-индекс LLEN по Redis — никаких зависимостей от celery-инспекции.
"""
import os

from prometheus_client import Counter, Gauge, start_http_server
import redis as redis_sync

QUEUE = os.environ.get("INGEST_QUEUE", "ingestion")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

queue_depth = Gauge(
    "rag2_ingestion_queue_depth",
    "Celery ingestion queue length (LLEN)",
    ["queue"],
)
scrape_errors = Counter(
    "rag2_queue_exporter_errors_total", "Redis LLEN scrape errors"
)


def collect() -> None:
    try:
        # Пароль из REDIS_URL (redis://:pass@redis:6379/0)
        r = redis_sync.from_url(REDIS_URL, decode_responses=True)
        depth = r.llen(QUEUE)
        queue_depth.labels(queue=QUEUE).set(depth)
    except Exception:  # noqa: BLE001 — экспортер не должен падать
        scrape_errors.inc()


if __name__ == "__main__":
    start_http_server(9808)
    print(f"queue exporter on :9808, queue={QUEUE}")
    import time

    while True:
        collect()
        time.sleep(10)
