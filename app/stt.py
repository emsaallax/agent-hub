"""Speech-to-text через OpenRouter Whisper.

Скачивает аудиофайл из Green API и отдаёт транскрипт через
openrouter.ai/api/v1/audio/transcriptions (OpenAI-совместимый эндпоинт).
"""

import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)

MODEL = "openai/whisper-large-v3"


async def transcribe_url(audio_url: str, mime_type: str = "audio/ogg") -> str:
    """Скачать аудио по URL и вернуть транскрипт. Пустая строка если не получилось."""
    if not settings.openrouter_api_key:
        log.warning("STT: OPENROUTER_API_KEY не задан, пропускаю голосовое")
        return ""

    async with httpx.AsyncClient(timeout=60) as client:
        audio_resp = await client.get(audio_url)
        audio_resp.raise_for_status()
        audio_data = audio_resp.content

    ext = "ogg"
    if "mp3" in mime_type:
        ext = "mp3"
    elif "mp4" in mime_type or "m4a" in mime_type:
        ext = "mp4"
    elif "webm" in mime_type:
        ext = "webm"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            data={"model": MODEL, "language": "ru"},
            files={"file": (f"audio.{ext}", audio_data, mime_type.split(";")[0])},
        )
        resp.raise_for_status()
        return (resp.json().get("text") or "").strip()
