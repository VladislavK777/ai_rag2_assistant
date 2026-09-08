-- RAG2: инициализация PostgreSQL (выполняется при ПЕРВОМ старте пустого тома)
-- ВАЖНО: таблицы и справочники (roles и т.д.) создаёт backend при bootstrap
-- (docker compose exec backend python -m app.db.bootstrap) — здесь только расширения.

CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- sparse-поиск (BM25-подобный полнотекст)
CREATE EXTENSION IF NOT EXISTS vector;    -- резерв: pgvector для экспериментов