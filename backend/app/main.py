"""Точка входа FastAPI. Инструментация OTel, роутеры, health/metrics."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app

from app.core.config import get_settings
from app.core.observability import setup_otel
from app.api.routes_auth import router as auth_router
from app.api.routes_chat import router as chat_router
from app.api.routes_documents import router as documents_router
from app.api.routes_admin import router as admin_router
from app.api.routes_admin import workspaces_router
from app.api.routes_admin_directory import router as directory_router

logger = logging.getLogger("rag2")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_otel(settings.otel_endpoint)
    logger.info("RAG2 backend started (otel: %s)", settings.otel_endpoint)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# HTTP-сервер-спаны (POST /api/v1/chat/stream и т.д.) — корни трейсов.
# Вызов на уровне модуля: при uvicorn --workers 2 lifespan-порядок
# инструментации ненадёжен, здесь — детерминированно до старта сервера.
from app.core.observability import instrument_app  # noqa: E402

instrument_app(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(chat_router, prefix="/api/v1/chat", tags=["chat"])
app.include_router(documents_router, prefix="/api/v1/ingest", tags=["ingest"])
app.include_router(admin_router, prefix="/api/v1/admin", tags=["admin"])
# GET /admin/workspaces доступен всем авторизованным (селектор workspace в UI)
app.include_router(workspaces_router, prefix="/api/v1/admin", tags=["admin"])
app.include_router(directory_router, prefix="/api/v1/admin", tags=["admin"])

# Prometheus /metrics (trailing slash — иначе ASGI mount даёт 307-редирект,
# за которым scrape-клиенты Prometheus не следуют)
app.mount("/metrics", make_asgi_app())
metrics_asgi = make_asgi_app()
app.mount("/metrics/", metrics_asgi)


@app.middleware("http")
async def http_metrics_middleware(request, call_next):
    """Метрики HTTP для SLO-дашборда: RPS / latency / error rate по роутам."""
    import time

    from app.core.metrics import http_request_duration_seconds, http_requests_total

    started = time.monotonic()
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    except Exception:
        status = 500
        raise
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        # health/metrics не считаем — шум
        if not request.url.path.startswith(("/health", "/metrics")):
            dur = time.monotonic() - started
            http_requests_total.labels(
                service="rag2-backend", route=path, status=str(status)
            ).inc()
            http_request_duration_seconds.labels(
                service="rag2-backend", route=path
            ).observe(dur)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "rag2-backend"}
