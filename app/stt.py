"""Speech-to-text через OpenRouter Whisper.

Скачивает аудиофайл из Green API и отдаёт транскрипт через
openrouter.ai/api/v1/audio/transcriptions. Формат OpenRouter — JSON
с base64-аудио (НЕ multipart, как у OpenAI).
"""

import base64
import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)

MODEL = "openai/whisper-large-v3"

# Поддерживаемые OpenRouter форматы: wav, mp3, flac, m4a, ogg, webm, aac.
# Green API присылает голосовые как .oga (ogg/opus) — это формат "ogg".
_FORMAT_BY_HINT = {
    "ogg": "ogg", "oga": "ogg", "opus": "ogg",
    "mp3": "mp3", "mpeg": "mp3",
    "m4a": "m4a", "mp4": "m4a",
    "wav": "wav", "webm": "webm", "flac": "flac", "aac": "aac",
}


def _guess_format(audio_url: str, mime_type: str) -> str:
    hint = (audio_url.rsplit(".", 1)[-1] if "." in audio_url.rsplit("/", 1)[-1] else "").lower()
    for source in (hint, mime_type.lower()):
        for key, fmt in _FORMAT_BY_HINT.items():
            if key in source:
                return fmt
    return "ogg"


async def transcribe_url(audio_url: str, mime_type: str = "audio/ogg") -> str:
    """Скачать аудио по URL и вернуть транскрипт. Пустая строка если не получилось."""
    if not settings.openrouter_api_key:
        log.warning("STT: OPENROUTER_API_KEY не задан, пропускаю голосовое")
        return ""

    async with httpx.AsyncClient(timeout=120) as client:
        audio_resp = await client.get(audio_url)
        audio_resp.raise_for_status()
        audio_b64 = base64.b64encode(audio_resp.content).decode()

        resp = await client.post(
            "https://openrouter.ai/api/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            json={
                "model": MODEL,
                "input_audio": {"data": audio_b64, "format": _guess_format(audio_url, mime_type)},
                "language": "ru",
            },
        )
        if resp.status_code >= 400:
            log.error("STT: OpenRouter %s: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
        return (resp.json().get("text") or "").strip()
