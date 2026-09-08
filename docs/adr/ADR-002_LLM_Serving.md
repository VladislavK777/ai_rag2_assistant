# ADR-002. LLM Serving и модели

- **Статус:** принято
- **Дата:** 2026-09-01

## Контекст

Нужен self-hosted инференс LLM на consumer/enterprise GPU с поддержкой русского языка, квантованием и оптимизацией KV-cache.

## Решение

**vLLM 0.28.0 + Qwen2.5-14B-Instruct-AWQ (4-bit).**

### Trade-off: LLM Serving Engine

| Движок | Throughput | KV-cache | Квантование | Экосистема | Вердикт |
|---|---|---|---|---|---|
| **vLLM** | Лучший (PagedAttention, continuous batching) | PagedAttention — фрагментация <1% | AWQ, GPTQ, FP8 | OpenAI-compatible API, широкая поддержка моделей | **Принят** |
| SGLang | Сопоставим, RadixAttention хорош для multi-turn | RadixAttention | AWQ/GPTQ | Моложе, меньше моделей | Альтернатива №1, пересмотр на Фазе 3 |
| TGI | Хороший | FlashAttention | AWQ/GPTQ | Уступает в throughput на 20–30% | Отклонён |
| llama.cpp | Низкий (CPU/GGUF) | Простой | GGUF | Портабельность, но не для 4 000 пользователей | Отклонён (использовать в прототипе) |

### Trade-off analysis: модели

| Модель | RU-качество | Размер | Лицензия | Вердикт |
|---|---|---|---|---|
| **Qwen2.5-14B-Instruct** | Высокое (MMLU-RU, MERA — топ среди open) | 14B → 8.5GB в AWQ | Apache 2.0 | **Принята** |
| Qwen3-14B | Выше, но новее/менее обкатана | 14B | Apache 2.0 | Кандидат на обновление |
| DeepSeek-V3 | Топ-качество, но 671B MoE — нужен кластер | 671B | MIT | Не для MVP-железа; distill-версии — Фаза 3 |
| Llama-3.1-8B | Среднее на RU | 8B | Llama License (ограничения >700M MAU) | Запасной вариант |
| GigaChat/YandexGPT | RU-хорошо | API only | Проприетарная | Нарушает on-prem требование |

### Квантование

- **AWQ 4-bit** — выбран: сохраняет ~99% качества FP16 (по MMLU/GSM8K), работает на consumer GPU (RTX 4090 24GB), поддержан vLLM нативно.
- GGUF — отклонён: формат llama.cpp, в vLLM поддержка хуже.
- FP8 — только Hopper GPU (H100), не наш случай MVP.

### KV-cache оптимизация

- PagedAttention (vLLM) — блочное управление, устраняет фрагментацию.
- `--max-model-len 8192` — ограничение контекста (RAG-чанки не требуют 32k).
- Prefix caching включён — системный промпт и Guardrails-инструкции кэшируются между запросами (экономия ~30% префилла).

### STT (транскрипция встреч)

**GigaAM v2 CTC** (Сбер, NVIDIA NeMo, Apache 2.0):
- Лучшее качество на русском среди open (WER ~5% на корпоративной речи).
- Работает on-prem, ~5× realtime на одной GPU.
- Альтернативы: Whisper-large-v3 (хуже на RU-акцентах/терминах, медленнее), Whisper от OpenAI (не on-prem).

### Эмбединги и reranker

- **BGE-M3** (BAAI, MIT): 1024d, мультиязычный (RU — отлично), поддерживает dense+sparse+colbert в одной модели — основа гибридного поиска.
- **BGE-reranker-v2-m3**: кросс-энкодер для переранжирования top-20 → top-5.

## Последствия

- Одна GPU 24GB вмещает LLM (8.5GB) + BGE-M3 (2.2GB) + reranker (2.2GB) + GigaAM (1.5GB) с запасом.
- Смена модели = изменение одной переменной окружения, код не меняется (OpenAI-compatible API).