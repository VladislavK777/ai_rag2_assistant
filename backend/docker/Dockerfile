FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl poppler-utils libmagic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY app ./app
COPY docker ./docker

EXPOSE 8000 9808

HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
    CMD curl -sf http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]