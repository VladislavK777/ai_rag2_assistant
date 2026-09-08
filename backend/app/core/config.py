"""Конфигурация приложения (12-factor, через переменные окружения)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_name: str = "RAG2 Backend"
    debug: bool = False
    cors_origins: str = "http://localhost:5173"

    # Auth
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480

    # Data plane
    database_url: str
    redis_url: str
    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str | None = None
    minio_endpoint: str = "minio:9000"
    minio_access_key: str
    minio_secret_key: str

    # Наблюдаемость: OTLP endpoint (traces + metrics → otel-collector)
    otel_endpoint: str = "http://otel-collector:4317"

    # LLM-провайдер: local (vLLM, on-prem) | yandex (внешний Yandex GPT)
    llm_provider: str = "local"

    # Локальный vLLM (целевая on-prem схема)
    vllm_base_url: str = "http://vllm:8000/v1"
    vllm_model: str = "rag2-llm"

    # Внешний Yandex GPT (переходный/лёгкий режим, OpenAI-compatible)
    yandex_base_url: str = "https://ai.api.cloud.yandex.net/v1"
    yandex_api_key: str | None = None
    yandex_folder_id: str | None = None
    yandex_model: str = "yandexgpt/latest"

    # Прочие GPU-сервисы
    embedding_url: str = "http://embedding:80"
    reranker_url: str = "http://reranker:80"
    guard_url: str = "http://guardrail:80"
    stt_url: str = "http://stt:8080"

    # Vault
    vault_addr: str = "http://vault:8200"
    vault_token: str | None = None

    # Agent
    max_react_iterations: int = 5
    rag_top_k: int = 20
    rerank_top_n: int = 8

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]