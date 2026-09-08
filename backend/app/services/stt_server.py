"""STT-сервер: GigaAM v2 (NeMo) как HTTP-сервис на GPU 1."""
import io
import logging
import tempfile
import time

import torch
import tornado.ioloop
import tornado.web
import nemo.collections.asr as nemo_asr

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rag2.stt")

_model = None


def load_model(model_name: str):
    global _model
    logger.info("Загрузка STT-модели: %s", model_name)
    _model = nemo_asr.models.ASRModel.from_pretrained(model_name)
    _model = _model.to("cuda").eval()
    logger.info("STT-модель загружена")


class TranscribeHandler(tornado.web.RequestHandler):
    async def post(self):
        body = __import__("json").loads(self.request.body)
        minio_key = body.get("minio_key", "")
        if not minio_key:
            self.set_status(400)
            self.write({"error": "minio_key обязателен"})
            return

        # Скачивание аудио из MinIO
        import asyncio
        from miniopy_async import Minio

        from app.core.config import get_settings

        s = get_settings()
        client = Minio(s.minio_endpoint, s.minio_access_key, s.minio_secret_key, secure=False)
        key = minio_key.removeprefix("audio/")
        data = await client.get_object("audio", key)
        raw = await data.read()
        data.close()
        await data.release()

        start = time.monotonic()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(raw)
            path = f.name

        loop = asyncio.get_event_loop()
        transcript = await loop.run_in_executor(None, _transcribe_sync, path)

        self.write(
            {
                "transcript": transcript,
                "latency_sec": round(time.monotonic() - start, 2),
                "audio_sec": len(raw) // 32000,
            }
        )


def _transcribe_sync(path: str) -> str:
    with torch.no_grad():
        result = _model.transcribe([path])
    return result[0][0] if isinstance(result[0], (list, tuple)) else result[0]


def make_app():
    return tornado.web.Application([("/transcribe", TranscribeHandler)])


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="nvidia/stt_ru_conformer_ctc_large")
    parser.add_argument("--port", default=8080, type=int)
    args = parser.parse_args()

    load_model(args.model)
    make_app().listen(args.port)
    tornado.ioloop.IOLoop.current().start()
