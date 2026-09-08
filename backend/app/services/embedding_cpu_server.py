"""CPU-версия embedding + rerank сервиса для локальной разработки (Mac / без GPU).

Мини-сервер на FastAPI с тем же API, что и TEI (text-embeddings-inference):
  POST /embed        {"inputs": [...]}        → [[floats]]
  POST /rerank       {"query","texts","top_n"} → [{"index","score"}]
  POST /embed_query  {"inputs": "..."}         → [floats]

Модели качаются с HuggingFace при первом старте (~2.3GB суммарно).
Запускается только в локальном профиле (compose override).
"""
import os
import time

from fastapi import FastAPI
from pydantic import BaseModel
from sentence_transformers import CrossEncoder, SentenceTransformer

app = FastAPI(title="RAG2 CPU Embedding")

EMBED_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")
RERANK_MODEL = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")

print(f"Загрузка эмбеддинг-модели: {EMBED_MODEL}")
embedder = SentenceTransformer(EMBED_MODEL, device="cpu")
print(f"Загрузка reranker: {RERANK_MODEL}")
reranker = CrossEncoder(RERANK_MODEL, device="cpu", max_length=512)
print("CPU-модели готовы")


class EmbedRequest(BaseModel):
    inputs: list[str] | str
    truncate: bool = True


class RerankRequest(BaseModel):
    query: str
    texts: list[str]
    top_n: int | None = None
    raw_scores: bool = False


@app.get("/health")
def health():
    return {"status": "ok", "device": "cpu"}


@app.post("/embed")
def embed(req: EmbedRequest):
    start = time.monotonic()
    texts = [req.inputs] if isinstance(req.inputs, str) else req.inputs
    vectors = embedder.encode(texts, normalize_embeddings=True).tolist()
    return vectors


@app.post("/rerank")
def rerank(req: RerankRequest):
    start = time.monotonic()
    pairs = [(req.query, t) for t in req.texts]
    scores = reranker.predict(pairs).tolist()
    ranked = sorted(
        ({"index": i, "score": s} for i, s in enumerate(scores)),
        key=lambda x: -x["score"],
    )
    if req.top_n:
        ranked = ranked[: req.top_n]
    return ranked
