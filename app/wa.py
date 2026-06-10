"""WhatsApp через Green API (облачный шлюз — Docker не нужен).

Два инстанса: ассистент (общение с владельцем) и рассыльный (аутрич клиентам).
Пока рассыльный не настроен — всё уходит с инстанса ассистента.
"""

import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)

MAX_CHUNK = 3500


def _split(text: str, size: int = MAX_CHUNK) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks: list[str] = []
    while text:
        if len(text) <= size:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    return chunks


class GreenApi:
    def __init__(self, id_instance: str, token: str):
        self.id_instance = id_instance
        self.token = token

    @property
    def configured(self) -> bool:
        return bool(self.id_instance and self.token)

    async def send_text(self, chat_id: str, text: str) -> None:
        if not self.configured:
            raise RuntimeError(
                "Green API не настроен: задай GREEN_API_ID_INSTANCE и GREEN_API_TOKEN в .env"
            )
        url = (
            f"{settings.green_api_url.rstrip('/')}"
            f"/waInstance{self.id_instance}/sendMessage/{self.token}"
        )
        async with httpx.AsyncClient(timeout=60) as client:
            for chunk in _split(text):
                resp = await client.post(url, json={"chatId": chat_id, "message": chunk})
                resp.raise_for_status()


assistant = GreenApi(settings.green_api_id_instance, settings.green_api_token)
outreach = GreenApi(
    settings.green_api_outreach_id_instance or settings.green_api_id_instance,
    settings.green_api_outreach_token or settings.green_api_token,
)


async def notify_owner(text: str) -> None:
    """Сообщение владельцу. Ошибки логируем, но не роняем фоновые задачи."""
    try:
        await assistant.send_text(settings.owner_chat_id, text)
    except Exception:
        log.exception("Failed to notify owner")
