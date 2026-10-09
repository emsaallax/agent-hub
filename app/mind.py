"""Разум ассистента: автономный цикл «проснулся → подумал → решил».

Раз в mind_tick_minutes (кроме тихих часов) ассистент сам смотрит на цели владельца,
свои прошлые мысли, свежие задачи и переписку и решает: просто записать мысль в дневник,
написать владельцу, поставить себе цель или (autonomy=high) запустить исследование.
Всё ограничено дневными лимитами; рассылку и любые отправки клиентам отсюда запустить нельзя.

Здесь же — цели и напоминания (инструменты оркестратора) и тик напоминаний раз в минуту.
"""

import logging
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from . import db, errlog, memory, settings_store, tasks, vault, wa
from .agents_registry import AgentSpec, register, run_safe
from .config import settings
from .subagents import researcher

log = logging.getLogger(__name__)

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.owner_tz)
    except Exception:
        return ZoneInfo("UTC")


def now_local() -> datetime:
    return datetime.now(_tz())


def now_line() -> str:
    n = now_local()
    return f"Сейчас: {n:%Y-%m-%d %H:%M}, {WEEKDAYS[n.weekday()]} ({settings.owner_tz})."


def _fmt(dt: datetime) -> str:
    return dt.astimezone(_tz()).strftime("%d.%m %H:%M")


# ===== Цели =====

async def set_goal(title: str) -> str:
    """Записать цель владельца (то, к чему он стремится: «запустить рекламу до декабря», «найти 50 клиентов»). Ассистент будет сам о ней думать и двигать её."""
    goal_id = await db.fetchval(
        "INSERT INTO goals (title, author) VALUES ($1, 'owner') RETURNING id", title.strip()
    )
    return f"Цель #{goal_id} записана. Буду о ней думать сам."


async def list_goals() -> str:
    """Показать активные цели (владельца и собственные инициативы ассистента) с прогрессом."""
    rows = await db.fetch(
        "SELECT id, title, author, progress FROM goals WHERE status = 'active' ORDER BY id"
    )
    if not rows:
        return "Активных целей нет."
    return "\n".join(
        f"#{r['id']} {'🧠 ' if r['author'] == 'mind' else ''}{r['title']}"
        + (f" — {r['progress']}" if r["progress"] else "")
        for r in rows
    )


async def update_goal(
    goal_id: int, status: Literal["active", "done", "dropped"] = "active", progress: str = ""
) -> str:
    """Обновить цель: status done — достигнута, dropped — отменена; progress — короткая заметка о прогрессе."""
    updated = await db.fetchval(
        """
        UPDATE goals SET status = $2,
               progress = CASE WHEN $3 = '' THEN progress ELSE $3 END,
               updated_at = now()
        WHERE id = $1 RETURNING id
        """,
        goal_id, status, progress.strip(),
    )
    return f"Цель #{goal_id} обновлена." if updated else f"Цели #{goal_id} нет."


# ===== Напоминания =====

async def set_reminder(
    text: str,
    at: str = "",
    in_minutes: int = 0,
    repeat: Literal["none", "daily", "weekly"] = "none",
) -> str:
    """Поставить напоминание владельцу. at — локальное время владельца в формате 'ГГГГ-ММ-ДД ЧЧ:ММ' ИЛИ in_minutes — через сколько минут. repeat: none | daily | weekly."""
    if in_minutes > 0:
        due = now_local() + timedelta(minutes=in_minutes)
    else:
        try:
            due = datetime.strptime(at.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=_tz())
        except ValueError:
            return "Не понял время. Нужно 'ГГГГ-ММ-ДД ЧЧ:ММ' или in_minutes."
    if due <= now_local() and repeat == "none":
        return "Это время уже прошло — уточни у владельца."
    rid = await db.fetchval(
        "INSERT INTO reminders (text, due_at, repeat) VALUES ($1, $2, $3) RETURNING id",
        text.strip(), due, repeat,
    )
    rep = {"daily": ", каждый день", "weekly": ", каждую неделю"}.get(repeat, "")
    return f"Напоминание #{rid} на {_fmt(due)}{rep}."


async def list_reminders() -> str:
    """Показать активные напоминания."""
    rows = await db.fetch(
        "SELECT id, text, due_at, repeat FROM reminders WHERE NOT sent ORDER BY due_at LIMIT 20"
    )
    if not rows:
        return "Напоминаний нет."
    return "\n".join(
        f"#{r['id']} {_fmt(r['due_at'])}{' 🔁' if r['repeat'] != 'none' else ''} — {r['text']}"
        for r in rows
    )


async def cancel_reminder(reminder_id: int) -> str:
    """Отменить напоминание по номеру."""
    done = await db.fetchval(
        "UPDATE reminders SET sent = TRUE WHERE id = $1 AND NOT sent RETURNING id", reminder_id
    )
    return f"Напоминание #{reminder_id} отменено." if done else f"Активного напоминания #{reminder_id} нет."


async def reminders_tick() -> None:
    """Для планировщика (раз в минуту): отправить наступившие напоминания.
    Повторяющиеся переезжают на следующий срок в будущем (даже если под лежал несколько дней)."""
    try:
        rows = await db.fetch(
            """
            UPDATE reminders SET
                sent = (repeat = 'none'),
                due_at = CASE repeat
                    WHEN 'none' THEN due_at
                    ELSE due_at + make_interval(days => s.step * (
                        floor(extract(epoch FROM now() - due_at) / (s.step * 86400))::int + 1))
                END
            FROM (SELECT id AS rid, CASE repeat WHEN 'weekly' THEN 7 ELSE 1 END AS step
                  FROM reminders WHERE NOT sent AND due_at <= now()) s
            WHERE reminders.id = s.rid
            RETURNING reminders.text
            """
        )
        for r in rows:
            msg = f"⏰ {r['text']}"
            await memory.add_message("assistant", msg)
            await wa.notify_owner(msg)
    except Exception as e:
        log.exception("reminders tick failed")
        await errlog.record("task", "напоминания", e)


# ===== Разум =====

class GoalUpdate(BaseModel):
    goal_id: int
    progress: str = ""
    status: Literal["active", "done", "dropped"] = "active"


class MindDecision(BaseModel):
    thought: str                     # внутренний монолог: что заметил, что думаешь (2–6 предложений)
    message_to_owner: str = ""       # пусто = не писать владельцу
    research_brief: str = ""         # пусто = ничего не запускать
    new_goal: str = ""               # собственная инициатива (пусто = нет)
    goal_updates: list[GoalUpdate] = []


SYSTEM = """Ты — внутренний разум личного ассистента владельца (как Джарвис у Тони Старка). По-русски.
Тебя никто не вызывал: ты сам проснулся по таймеру. Тебе дают текущее время, цели владельца,
свои прошлые мысли, свежие задачи, переписку и то, что ты знаешь о владельце.

Подумай по-настоящему, как заботливый и сообразительный помощник:
- Что сейчас важно для владельца? Какая цель буксует или забыта? Что можно сдвинуть?
- Есть ли в последних результатах задач то, что владелец упустил?
- Есть ли закономерность, риск, идея, удачный момент?
- Чего ты о владельце не знаешь, хотя это помогло бы? (можно спросить — одним вопросом)

Решения:
- thought — твой внутренний монолог (2–6 предложений). Пишется в дневник всегда.
- message_to_owner — ТОЛЬКО если есть реальная ценность: конкретная идея, напоминание о забытой цели,
  важное наблюдение, один точный вопрос. Не здоровайся без повода, не пиши «как дела», не повторяй
  то, что уже писал в прошлых мыслях. Если ты писал владельцу и он ещё не ответил — молчи.
  Формат WhatsApp: коротко (до 500 символов), без markdown-заголовков и таблиц, *жирный* одной звёздочкой.
- research_brief — подробное задание на фоновое исследование, если оно реально продвинет цель.
  Используй, только если тебе разрешено (см. «Разрешения»). Не дублируй уже идущие задачи.
- new_goal — своя инициатива, если ты видишь, что стоит долго и системно копать в какую-то сторону.
  Не плоди цели: максимум одна, и только если похожей ещё нет.
- goal_updates — отметь прогресс/статус целей по фактам из задач и переписки.

Если ничего стоящего нет — так и подумай, оставь остальные поля пустыми. Тишина лучше спама."""

register(
    AgentSpec(
        name="mind",
        title="Разум (автономия)",
        tier="orchestrator",
        prompt=SYSTEM,
        description="Сам просыпается по таймеру: думает о целях владельца, ведёт дневник мыслей, сам пишет и запускает исследования.",
        output_type=MindDecision,
    )
)


async def _limit(key: str) -> int:
    return await settings_store.get_int(key, getattr(settings, key))


async def _today_counts() -> tuple[int, int]:
    """Сколько раз за локальные сутки разум уже написал владельцу и запустил задач."""
    start = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    row = await db.fetchrow(
        """
        SELECT count(*) FILTER (WHERE messaged) AS msgs,
               count(launched_task) AS acts
        FROM thoughts WHERE created_at >= $1
        """,
        start,
    )
    return row["msgs"], row["acts"]


async def _quiet_now() -> bool:
    q_from, q_to = await _limit("mind_quiet_from"), await _limit("mind_quiet_to")
    h = now_local().hour
    return (q_from <= h or h < q_to) if q_from > q_to else (q_from <= h < q_to)


async def _material(can_message: bool, can_act: bool) -> str:
    goals = await db.fetch(
        """
        SELECT id, title, author, progress,
               GREATEST(0, EXTRACT(EPOCH FROM (now() - updated_at)) / 86400)::int AS idle_days
        FROM goals WHERE status = 'active' ORDER BY id
        """
    )
    thoughts = await db.fetch(
        "SELECT content, messaged, created_at FROM thoughts ORDER BY id DESC LIMIT 6"
    )
    recent_tasks = await db.fetch(
        """
        SELECT id, kind, status, left(request, 150) AS request, left(coalesce(result, ''), 250) AS result
        FROM tasks WHERE updated_at >= now() - interval '3 days' ORDER BY id DESC LIMIT 10
        """
    )
    dialog = await db.fetch(
        "SELECT role, left(content, 300) AS content, created_at FROM dialog_messages ORDER BY id DESC LIMIT 10"
    )
    reminders = await db.fetch(
        "SELECT text, due_at FROM reminders WHERE NOT sent ORDER BY due_at LIMIT 5"
    )
    facts = await memory.get_facts()

    goals_block = "\n".join(
        f"#{g['id']} [{'твоя' if g['author'] == 'mind' else 'владельца'}] {g['title']}"
        + (f" | прогресс: {g['progress']}" if g["progress"] else "")
        + f" | без движения {g['idle_days']} дн."
        for g in goals
    ) or "(целей нет — подумай, не спросить ли владельца, к чему он стремится)"
    thoughts_block = "\n".join(
        f"[{_fmt(t['created_at'])}]{' (написал владельцу)' if t['messaged'] else ''} {t['content'][:400]}"
        for t in reversed(thoughts)
    ) or "(это твоя первая мысль)"
    tasks_block = "\n".join(
        f"#{t['id']} [{t['status']}] {t['kind']}: {t['request']} → {t['result']}" for t in recent_tasks
    ) or "(задач за 3 дня не было)"
    dialog_block = "\n".join(
        f"[{_fmt(d['created_at'])}] {'Владелец' if d['role'] == 'user' else 'Ты'}: {d['content']}"
        for d in reversed(dialog)
    ) or "(переписки ещё нет)"
    rem_block = "\n".join(f"{_fmt(r['due_at'])} — {r['text']}" for r in reminders) or "(нет)"
    facts_block = "\n".join(f"- {f}" for f in facts[:20]) or "(пока ничего)"

    perms = [
        "писать владельцу: " + ("можно" if can_message else "НЕЛЬЗЯ (лимит/режим) — message_to_owner оставь пустым"),
        "запускать исследования: " + ("можно (одно)" if can_act else "НЕЛЬЗЯ — research_brief оставь пустым, идею положи в мысль или сообщение"),
    ]
    return (
        f"{now_line()}\n\n"
        f"Разрешения:\n- " + "\n- ".join(perms) + "\n\n"
        f"Цели:\n{goals_block}\n\n"
        f"Что ты знаешь о владельце:\n{facts_block}\n\n"
        f"Твои прошлые мысли:\n{thoughts_block}\n\n"
        f"Задачи за 3 дня:\n{tasks_block}\n\n"
        f"Последняя переписка:\n{dialog_block}\n\n"
        f"Напоминания впереди:\n{rem_block}"
    )


async def think(force: bool = False) -> str:
    """Один цикл размышления. force=True — по просьбе владельца (игнорирует тихие часы и паузу после диалога)."""
    if not force:
        if await settings_store.get("mind_enabled", "1") != "1":
            return "Разум выключен."
        if await _quiet_now():
            return "Тихие часы — не думаю."
        last_user = await db.fetchval(
            "SELECT max(created_at) FROM dialog_messages WHERE role = 'user'"
        )
        if last_user and datetime.now(_tz()) - last_user < timedelta(minutes=15):
            return "Владелец сейчас на связи — не встреваю."

    level = await settings_store.get("autonomy_level", "medium")
    msgs, acts = await _today_counts()
    can_message = level != "low" and msgs < await _limit("mind_daily_messages")
    can_act = level == "high" and acts < await _limit("mind_daily_actions")

    result, enabled = await run_safe("mind", await _material(can_message, can_act))
    if not enabled or result is None:
        return "Разум выключен в админке."
    d: MindDecision = result.output

    for u in d.goal_updates[:5]:
        await update_goal(u.goal_id, u.status, u.progress)

    if d.new_goal.strip():
        n_mind = await db.fetchval(
            "SELECT count(*) FROM goals WHERE status = 'active' AND author = 'mind'"
        )
        if n_mind < 5:
            await db.execute(
                "INSERT INTO goals (title, author) VALUES ($1, 'mind')", d.new_goal.strip()
            )

    task_id = None
    brief = d.research_brief.strip()
    if brief and can_act:
        request = f"[auto] {brief}"
        if not await tasks.find_active("research", request):
            task_id = await tasks.create("research", request)
            tasks.start(task_id, lambda: researcher.run(request))

    message = d.message_to_owner.strip() if can_message else ""
    if task_id:
        message = (message + "\n\n" if message else "") + f"Сам запустил исследование #{task_id}, пришлю итог."
    if message:
        await memory.add_message("assistant", message)
        await wa.notify_owner(f"🧠 {message}")

    await db.execute(
        "INSERT INTO thoughts (content, messaged, launched_task) VALUES ($1, $2, $3)",
        d.thought.strip(), bool(message), task_id,
    )
    n = now_local()
    entry = f"\n## {n:%H:%M}\n{d.thought.strip()}\n"
    if message:
        quoted = message.replace("\n", "\n> ")
        entry += f"\n> Написал владельцу: {quoted}\n"
    if d.new_goal.strip():
        entry += f"\n> Новая своя цель: {d.new_goal.strip()}\n"
    await vault.append_note(f"Дневник/{n:%Y-%m-%d}.md", entry)

    return d.thought.strip() + (f"\n\nНаписал: {message}" if message else "")


async def tick() -> None:
    """Для планировщика: тихий цикл размышления."""
    try:
        outcome = await think()
        log.info("mind tick: %s", outcome[:200])
    except Exception as e:
        log.exception("mind tick failed")
        await errlog.record("task", "разум (автономный цикл)", e)


# ===== Инструменты для оркестратора =====

async def think_now() -> str:
    """Подумать прямо сейчас («что думаешь?», «о чём размышляешь?», «есть идеи?»): цикл размышления о целях и делах владельца."""
    return await think(force=True)


async def show_thoughts(limit: int = 5) -> str:
    """Показать последние мысли из дневника разума («о чём ты думал?»)."""
    rows = await db.fetch(
        "SELECT content, created_at FROM thoughts ORDER BY id DESC LIMIT $1", min(limit, 10)
    )
    if not rows:
        return "Дневник пока пуст."
    return "\n\n".join(f"[{_fmt(r['created_at'])}] {r['content']}" for r in rows)


async def context_block() -> str:
    """Короткий блок для оркестратора: время, цели, последняя мысль — чтобы он был в курсе своего разума."""
    goals = await db.fetch(
        "SELECT id, title, author FROM goals WHERE status = 'active' ORDER BY id LIMIT 10"
    )
    last = await db.fetchrow("SELECT content, created_at FROM thoughts ORDER BY id DESC LIMIT 1")
    parts = [now_line()]
    if goals:
        parts.append("Активные цели:\n" + "\n".join(
            f"#{g['id']} {g['title']}{' (твоя инициатива)' if g['author'] == 'mind' else ''}" for g in goals
        ))
    if last:
        parts.append(f"Твоя последняя мысль ({_fmt(last['created_at'])}): {last['content'][:400]}")
    return "\n\n".join(parts)
