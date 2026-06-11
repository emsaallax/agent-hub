"""InboxAgent: разбор входящих от клиентов на рассыльном номере."""

import logging
from typing import Literal

from pydantic import BaseModel

from .. import db, wa
from ..agents_registry import AgentSpec, build, register
from ..config import normalize_phone

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "interested": "🔥 интерес",
    "question": "❓ вопрос",
    "declined": "🙅 отказ",
    "spam": "🚫 спам",
    "other": "💬 прочее",
}


class InboxResult(BaseModel):
    category: Literal["interested", "question", "declined", "spam", "other"]
    suggested_reply: str


CLASSIFIER_PROMPT = (
    "Ты разбираешь ответ потенциального клиента на холодное сообщение в WhatsApp.\n"
    "Классифицируй: interested (интерес/готов обсуждать), question (задаёт вопрос), "
    "declined (отказ), spam (мусор/бот), other.\n"
    "И предложи короткий человеческий ответ от имени владельца (по-русски, 1–3 предложения). "
    "Для отказа — вежливое завершение. Для спама suggested_reply оставь пустым."
)

register(
    AgentSpec(
        name="inbox_classifier",
        title="Входящие от клиентов",
        tier="cheap",
        prompt=CLASSIFIER_PROMPT,
        description="Классифицирует ответы лидов и предлагает ответ владельцу.",
        output_type=InboxResult,
    )
)


async def handle_incoming(chat_id: str, text: str) -> None:
    phone = normalize_phone(chat_id.split("@")[0])
    row = await db.fetchrow(
        """
        SELECT l.id AS lead_id, l.status, c.name FROM leads l
        JOIN companies c ON c.id = l.company_id
        WHERE c.phone = $1
        """,
        phone,
    )
    if not row:
        await wa.notify_owner(
            f"💬 Сообщение на рассыльный номер от неизвестного +{phone}:\n«{text[:500]}»"
        )
        return

    await db.execute(
        "INSERT INTO lead_messages (lead_id, direction, text) VALUES ($1, 'in', $2)",
        row["lead_id"],
        text[:2000],
    )

    history_rows = await db.fetch(
        "SELECT direction, text FROM lead_messages WHERE lead_id = $1 ORDER BY id DESC LIMIT 6",
        row["lead_id"],
    )
    history = "\n".join(
        f"{'Мы' if r['direction'] == 'out' else 'Клиент'}: {r['text'][:400]}"
        for r in reversed(history_rows)
    )

    try:
        classifier, enabled = await build("inbox_classifier")
        if not enabled:
            await wa.notify_owner(f"💬 Ответ от {row['name']} (+{phone}):\n«{text[:500]}»")
            return
        result = (await classifier.run(f"Переписка:\n{history}\n\nНовый ответ клиента:\n{text}")).output
    except Exception:
        log.exception("inbox classification failed")
        await wa.notify_owner(f"💬 Ответ от {row['name']} (+{phone}):\n«{text[:500]}»")
        return

    if result.category != "other":
        await db.execute(
            "UPDATE leads SET status = $2, updated_at = now() WHERE id = $1",
            row["lead_id"],
            result.category,
        )

    label = STATUS_LABELS[result.category]
    msg = f"{label} — {row['name']} (лид #{row['lead_id']}):\n«{text[:500]}»"
    if result.suggested_reply:
        msg += (
            f"\n\nПредлагаю ответить:\n«{result.suggested_reply}»"
            f"\n\nЧтобы отправить — скажи: «отправь лиду {row['lead_id']}: <текст>»"
        )
    await wa.notify_owner(msg)
