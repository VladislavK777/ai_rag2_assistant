"""OpenTelemetry: трейсинг всех сервисов, корреляция через trace_id."""
import logging

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor


def setup_otel(endpoint: str | None = None) -> None:
    """Инициализация OTel: трейсинг + логирование. OTLP → Collector → Jaeger/Loki.

    endpoint — gRPC-адрес otel-collector (например, http://otel-collector:4317).
    Если не задан — экспорт не выполняется, но trace_id генерируется для логов.
    """
    resource = Resource.create(
        {
            "service.name": "rag2-backend",
            "service.version": "0.1.0",
            "deployment.environment": "on-prem",
        }
    )
    provider = TracerProvider(resource=resource)

    if endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )
        # Логи python-logging → OTLP → Collector → Loki.
        # trace_id из активного спана автоматически попадает в запись лога —
        # корреляция лог ↔ трейс в Grafana/Jaeger.
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter,
        )

        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=endpoint, insecure=True)
            )
        )
        set_logger_provider(logger_provider)

        handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        # root-логер по умолчанию на WARNING — INFO-записи отсекаются до handler'а.
        # Ставим уровень на root и вешаем OTLP-handler также на "rag2" и uvicorn.
        logging.basicConfig(level=logging.INFO, handlers=[handler])
        for name in ("rag2", "uvicorn", "uvicorn.access", "uvicorn.error"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.INFO)
            lg.handlers.append(handler)
            lg.propagate = False  # без дублирования в root

    trace.set_tracer_provider(provider)


def instrument_app(app) -> None:
    FastAPIInstrumentor.instrument_app(app)


def current_trace_id() -> str:
    """trace_id для включения в ответы и аудит-лог."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        return format(ctx.trace_id, "032x")
    return "00000000000000000000000000000000"


logger = logging.getLogger("rag2.otel")
