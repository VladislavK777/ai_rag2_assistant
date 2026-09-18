"""Конфигурация приложения (12-factor, через переменные окружения)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_name: str = "RAG2 Backend"
    debug: bool = False
    cors_origins: str = "http://localhost:5173"

    # Keycloak SSO (аутентификация только здесь; backend — resource server)
    # issuer — для серверной валидации (сеть compose/контур),
    # public_issuer — какой отдавать браузеру (через nginx или напрямую на Mac)
    keycloak_issuer: str = "http://keycloak:8080/realms/rag2"
    keycloak_public_issuer: str | None = None
    keycloak_client_id: str = "rag2_client"

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

    # Маскирование PII перед LLM: auto = маскировать только при внешнем
    # провайдере (yandex); always / never — принудительно.
    # Логи и трейсы маскируются независимо от этого флага (pii_sanitizer).
    pii_masking: str = "auto"

    @property
    def pii_masking_enabled(self) -> bool:
        if self.pii_masking == "always":
            return True
        if self.pii_masking == "never":
            return False
        return self.llm_provider != "local"

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