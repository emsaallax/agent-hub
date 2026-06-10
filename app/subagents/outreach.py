"""OutreachAgent: драфт дешёвой моделью -> ревью сильной -> аппрув владельца -> очередь с лимитами."""

import asyncio
import logging
import random

from pydantic_ai import Agent

from .. import db, wa
from ..config import phone_to_chat_id, settings
from ..llm import cheap_model, strong_model

log = logging.getLogger(__name__)

_drafter = Agent(
    cheap_model(),
    output_type=str,
    system_prompt=(
        "Ты пишешь ПЕРВОЕ сообщение компании в WhatsApp от имени владельца малого бизнеса.\n"
        "Требования: 2–4 предложения, по-человечески, без канцелярита и спам-штампов "
        "(никаких «уникальное предложение», «взаимовыгодное сотрудничество»). "
        "Персонализируй под компанию (используй заметку о ней). Одно конкретное предложение "
        "и лёгкий вопрос в конце. Без эмодзи-спама. Верни ТОЛЬКО текст сообщения."
    ),
)

_reviewer = Agent(
    strong_model(),
    output_type=str,
    system_prompt=(
        "Ты — строгий редактор холодных сообщений в WhatsApp. Тебе дают черновик.\n"
        "Сделай его лучше: естественный язык, конкретика, не похоже на спам, 2–4 предложения, "
        "сохрани суть предложения. Верни ТОЛЬКО финальный текст сообщения, без комментариев."
    ),
)


async def prepare(offer: str, niche: str = "", city: str = "", limit: int = 10) -> str:
    """Сгенерировать черновики для новых лидов и прислать владельцу на аппрув."""
    query = """
        SELECT l.id AS lead_id, c.name, c.phone, c.niche, c.city, c.note
        FROM leads l JOIN companies c ON c.id = l.company_id
        WHERE l.status = 'new' AND c.phone IS NOT NULL AND c.phone <> ''
    """
    args: list = []
    if niche:
        args.append(f"%{niche}%")
        query += f" AND c.niche ILIKE ${len(args)}"
    if city:
        args.append(f"%{city}%")
        query += f" AND c.city ILIKE ${len(args)}"
    args.append(limit)
    query += f" ORDER BY l.id LIMIT ${len(args)}"

    rows = await db.fetch(query, *args)
    if not rows:
        return "Подходящих лидов со статусом «новый» и телефоном нет. Сначала запусти поиск клиентов."

    drafts = []
    for r in rows:
        context = (
            f"Компания: {r['name']} ({r['niche'] or 'ниша неизвестна'}, {r['city'] or ''}).\n"
            f"Заметка: {r['note'] or 'нет'}.\n"
            f"Что предлагаем: {offer}"
        )
        draft = (await _drafter.run(context)).output.strip()
        final = (await _reviewer.run(f"{context}\n\nЧерновик:\n{draft}")).output.strip()
        msg_id = await db.fetchval(
            "INSERT INTO outreach_messages (lead_id, text) VALUES ($1, $2) RETURNING id",
            r["lead_id"],
            final[:1500],
        )
        drafts.append(f"#{msg_id} → {r['name']} ({r['phone']}):\n{final}")

    return (
        f"Подготовил {len(drafts)} черновиков (дешёвая писала, сильная вычитала):\n\n"
        + "\n\n".join(drafts)
        + "\n\nОтветь: «одобри все», «одобри 12,13» или «отклони 14». "
        f"После аппрува уйдут пачками по {settings.outreach_batch_per_tick} каждые ~15 мин, "
        f"лимит {settings.outreach_daily_limit}/день."
    )


async def approve(ids: list[int]) -> str:
    if ids:
        result = await db.execute(
            "UPDATE outreach_messages SET status = 'approved' WHERE id = ANY($1) AND status = 'pending_approval'",
            ids,
        )
    else:
        result = await db.execute(
            "UPDATE outreach_messages SET status = 'approved' WHERE status = 'pending_approval'"
        )
    count = int(result.split()[-1])
    await db.execute(
        """
        UPDATE leads SET status = 'queued', updated_at = now()
        WHERE id IN (SELECT lead_id FROM outreach_messages WHERE status = 'approved')
          AND status = 'new'
        """
    )
    return f"Одобрено: {count}. Отправка пойдёт автоматически с лимитами."


async def reject(ids: list[int]) -> str:
    result = await db.execute(
        "UPDATE outreach_messages SET status = 'rejected' WHERE id = ANY($1) AND status = 'pending_approval'",
        ids,
    )
    return f"Отклонено: {int(result.split()[-1])}."


async def list_pending() -> str:
    rows = await db.fetch(
        """
        SELECT om.id, om.text, c.name FROM outreach_messages om
        JOIN leads l ON l.id = om.lead_id JOIN companies c ON c.id = l.company_id
        WHERE om.status = 'pending_approval' ORDER BY om.id
        """
    )
    if not rows:
        return "Черновиков на аппруве нет."
    return "На аппруве:\n\n" + "\n\n".join(
        f"#{r['id']} → {r['name']}:\n{r['text']}" for r in rows
    )


async def _sent_today() -> int:
    return await db.fetchval(
        "SELECT count(*) FROM outreach_messages WHERE status = 'sent' AND sent_at >= date_trunc('day', now())"
    )


async def tick() -> None:
    """Отправить очередную пачку одобренных сообщений (зовётся n8n по расписанию)."""
    sent_today = await _sent_today()
    remaining = settings.outreach_daily_limit - sent_today
    if remaining <= 0:
        return

    batch = await db.fetch(
        """
        SELECT om.id, om.text, om.lead_id, c.phone, c.name
        FROM outreach_messages om
        JOIN leads l ON l.id = om.lead_id JOIN companies c ON c.id = l.company_id
        WHERE om.status = 'approved'
        ORDER BY om.id
        LIMIT $1
        """,
        min(settings.outreach_batch_per_tick, remaining),
    )
    for row in batch:
        await asyncio.sleep(
            random.randint(settings.outreach_min_delay_s, settings.outreach_max_delay_s)
        )
        chat_id = phone_to_chat_id(row["phone"])
        try:
            await wa.outreach.send_text(chat_id, row["text"])
        except Exception as e:
            log.exception("outreach send failed: %s", row["id"])
            await db.execute(
                "UPDATE outreach_messages SET status = 'failed', error = $2 WHERE id = $1",
                row["id"],
                str(e)[:500],
            )
            await wa.notify_owner(f"⚠️ Не ушло сообщение #{row['id']} ({row['name']}): {e}")
            continue
        await db.execute(
            "UPDATE outreach_messages SET status = 'sent', sent_at = now() WHERE id = $1",
            row["id"],
        )
        await db.execute(
            "UPDATE leads SET status = 'contacted', updated_at = now() WHERE id = $1",
            row["lead_id"],
        )
        await db.execute(
            "INSERT INTO lead_messages (lead_id, direction, text) VALUES ($1, 'out', $2)",
            row["lead_id"],
            row["text"],
        )
        log.info("outreach sent #%s -> %s", row["id"], row["name"])


async def send_to_lead(lead_id: int, text: str) -> str:
    """Ручная отправка лиду (по команде владельца) через рассыльный номер."""
    row = await db.fetchrow(
        """
        SELECT c.phone, c.name FROM leads l JOIN companies c ON c.id = l.company_id
        WHERE l.id = $1
        """,
        lead_id,
    )
    if not row or not row["phone"]:
        return f"Лид {lead_id} не найден или у него нет телефона."
    await wa.outreach.send_text(phone_to_chat_id(row["phone"]), text)
    await db.execute(
        "INSERT INTO lead_messages (lead_id, direction, text) VALUES ($1, 'out', $2)",
        lead_id,
        text,
    )
    return f"Отправлено лиду #{lead_id} ({row['name']})."
