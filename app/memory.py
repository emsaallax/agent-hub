"""Память: рабочее окно диалога + rolling summary + факты + полнотекстовый архив.

Принцип экономии токенов: в промпт попадает только короткий блок контекста,
а не вся история. Старое сжимается дешёвой моделью в фоне.
"""

import logging
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from . import db, errlog, settings_store
from .agents_registry import AgentSpec, build, register, run_safe

log = logging.getLogger(__name__)

WINDOW = 15           # сколько последних сообщений держим в промпте
SUMMARIZE_AFTER = 40  # при скольких несжатых сообщениях запускать суммаризацию
MAX_FACTS = 100
MSG_CAP = 1500        # обрезка одного сообщения в контексте
CURATE_COOLDOWN_H = 6 # не гонять актуализацию при полной памяти чаще, чем раз в N часов

register(
    AgentSpec(
        name="memory_summarizer",
        title="Память: выжимка диалога",
        tier="cheap",
        prompt=(
            "Ты сжимаешь историю диалога владельца с его ассистентом в краткую выжимку. "
            "Сохрани: принятые решения, договорённости, незакрытые задачи, важные цифры и имена. "
            "Убери воду и приветствия. Не больше 120 слов. Пиши по-русски."
        ),
        description="Сжимает старую переписку в rolling summary (экономия токенов).",
    )
)

register(
    AgentSpec(
        name="fact_extractor",
        title="Память: извлечение фактов",
        tier="cheap",
        prompt=(
            "Из фрагмента диалога выдели НОВЫЕ долгосрочные факты о владельце и его бизнесе: "
            "чем занимается, предпочтения, постоянные инструкции, важные контакты. "
            "Только то, что пригодится через недели. Не дублируй уже известные факты. "
            "Каждый факт — одна короткая строка по-русски. Если новых фактов нет — верни пустой список."
        ),
        description="Достаёт долгосрочные факты из диалога в memory_facts.",
        output_type=list[str],
    )
)


class FactAudit(BaseModel):
    remove_ids: list[int]  # id устаревших/дублирующихся фактов
    add: list[str]         # объединённые формулировки взамен слитых


register(
    AgentSpec(
        name="fact_curator",
        title="Память: актуализация фактов",
        tier="cheap",
        prompt=(
            "Ты приводишь в порядок список долгосрочных фактов о владельце и его бизнесе. "
            "Тебе дают пронумерованный список фактов с категориями и датами. Верни:\n"
            "- remove_ids: id фактов, которые УСТАРЕЛИ (есть более свежий противоречащий факт), "
            "ДУБЛИРУЮТ другой факт или были разовыми и не пригодятся через месяц;\n"
            "- add: новые формулировки, если несколько фактов стоит слить в один "
            "(иначе пустой список). Каждая — одна короткая строка по-русски.\n"
            "Факты категории manual вводил сам владелец — удаляй их только при явном "
            "дубле или противоречии. Будь консервативен: сомневаешься — оставляй."
        ),
        description="Чистит память: убирает устаревшие и дублирующиеся факты, сливает похожие.",
        output_type=FactAudit,
    )
)


async def add_message(role: str, content: str) -> None:
    await db.execute(
        "INSERT INTO dialog_messages (role, content) VALUES ($1, $2)", role, content
    )


async def get_facts() -> list[str]:
    rows = await db.fetch(
        "SELECT fact FROM memory_facts WHERE active ORDER BY id DESC LIMIT $1", MAX_FACTS
    )
    return [r["fact"] for r in rows]


async def build_context_block() -> str:
    """Компактный контекст для оркестратора: факты + выжимка + последние сообщения."""
    state = await db.fetchrow("SELECT summary, summarized_to FROM dialog_state WHERE id = 1")
    recent = await db.fetch(
        "SELECT role, content FROM dialog_messages ORDER BY id DESC LIMIT $1", WINDOW
    )
    facts = await get_facts()

    parts: list[str] = []
    if facts:
        parts.append("Что ты знаешь о владельце:\n" + "\n".join(f"- {f}" for f in facts[:20]))
    if state and state["summary"]:
        parts.append("Выжимка более ранней переписки:\n" + state["summary"][:2000])
    if recent:
        lines = []
        for r in reversed(recent):
            who = "Владелец" if r["role"] == "user" else "Ты"
            lines.append(f"{who}: {r['content'][:MSG_CAP]}")
        parts.append("Последние сообщения:\n" + "\n".join(lines))
    return "\n\n".join(parts) if parts else "Диалог только начинается."


async def archive(kind: str, content: str) -> None:
    await db.execute(
        "INSERT INTO memory_archive (kind, content) VALUES ($1, $2)", kind, content[:8000]
    )


async def search_archive(query: str, limit: int = 5) -> list[str]:
    rows = await db.fetch(
        """
        SELECT content, created_at FROM memory_archive
        WHERE tsv @@ plainto_tsquery('russian', $1)
        ORDER BY created_at DESC LIMIT $2
        """,
        query,
        limit,
    )
    return [f"[{r['created_at']:%d.%m.%Y}] {r['content'][:600]}" for r in rows]


async def maintain() -> None:
    """Фоновое обслуживание после обмена сообщениями: факты + суммаризация."""
    try:
        await _extract_facts()
    except Exception as e:
        log.exception("fact extraction failed")
        await errlog.record("memory", "fact_extractor", e)
    try:
        await _maybe_summarize()
    except Exception as e:
        log.exception("summarization failed")
        await errlog.record("memory", "memory_summarizer", e)


async def _fact_exists(fact: str) -> bool:
    return bool(
        await db.fetchval(
            "SELECT 1 FROM memory_facts WHERE active AND lower(fact) = lower($1)", fact
        )
    )


async def _extract_facts() -> None:
    n_facts = await db.fetchval("SELECT count(*) FROM memory_facts WHERE active")
    if n_facts >= MAX_FACTS:
        # Память заполнена: вместо тихой остановки чистим её (не чаще раза в CURATE_COOLDOWN_H)
        last = await settings_store.get("facts_last_curated", "")
        try:
            last_dt = datetime.fromisoformat(last) if last else None
        except ValueError:
            last_dt = None
        if last_dt is None or datetime.now(timezone.utc) - last_dt > timedelta(hours=CURATE_COOLDOWN_H):
            log.info("memory_facts заполнена (%s) — запускаю актуализацию", n_facts)
            await curate_facts()
        return
    recent = await db.fetch(
        "SELECT role, content FROM dialog_messages ORDER BY id DESC LIMIT 4"
    )
    if not recent:
        return
    dialog = "\n".join(
        f"{'Владелец' if r['role'] == 'user' else 'Ассистент'}: {r['content'][:800]}"
        for r in reversed(recent)
    )
    known = await get_facts()
    prompt = (
        "Уже известные факты:\n" + "\n".join(f"- {f}" for f in known[:40]) + "\n\nФрагмент диалога:\n" + dialog
    )
    result, enabled = await run_safe("fact_extractor", prompt)
    if not enabled:
        return
    for fact in result.output[:3]:
        fact = fact.strip()
        if fact and not await _fact_exists(fact):
            await db.execute("INSERT INTO memory_facts (fact) VALUES ($1)", fact)


async def curate_facts() -> str:
    """Актуализация памяти: убрать устаревшее и дубли, слить похожие факты.

    Запускается ежесуточно (вместе с рефлексией), кнопкой из админки и
    автоматически, когда память заполнена. Возвращает краткий итог.
    """
    await settings_store.set(
        "facts_last_curated", datetime.now(timezone.utc).isoformat()
    )
    rows = await db.fetch(
        "SELECT id, fact, category, created_at FROM memory_facts WHERE active ORDER BY id"
    )
    if len(rows) < 5:
        return f"Фактов всего {len(rows)} — актуализация не нужна."
    listing = "\n".join(
        f"#{r['id']} [{r['category']}, {r['created_at']:%d.%m.%Y}] {r['fact']}" for r in rows
    )
    run_result, enabled = await run_safe("fact_curator", "Список фактов:\n" + listing)
    if not enabled:
        return "Куратор фактов выключен в админке."
    result = run_result.output

    valid_ids = {r["id"] for r in rows}
    remove = [i for i in result.remove_ids if i in valid_ids]
    # Защита от слишком агрессивной модели: за один проход не выкидываем больше половины
    if len(remove) > len(rows) // 2:
        log.warning("fact_curator предложил удалить %s из %s — режу до половины", len(remove), len(rows))
        remove = remove[: len(rows) // 2]
    for fact_id in remove:
        await db.execute("UPDATE memory_facts SET active = FALSE WHERE id = $1", fact_id)

    added = 0
    for fact in result.add[:10]:
        fact = fact.strip()
        if fact and not await _fact_exists(fact):
            await db.execute(
                "INSERT INTO memory_facts (fact, category) VALUES ($1, 'merged')", fact
            )
            added += 1

    left = len(rows) - len(remove) + added
    summary = f"Актуализация памяти: убрано {len(remove)}, объединено в новые {added}, осталось {left}."
    log.info(summary)
    return summary


async def _maybe_summarize() -> None:
    state = await db.fetchrow("SELECT summary, summarized_to FROM dialog_state WHERE id = 1")
    unsummarized = await db.fetch(
        "SELECT id, role, content FROM dialog_messages WHERE id > $1 ORDER BY id",
        state["summarized_to"],
    )
    if len(unsummarized) <= SUMMARIZE_AFTER:
        return
    # Сжимаем всё, кроме последних WINDOW сообщений
    to_compress = unsummarized[:-WINDOW]
    dialog = "\n".join(
        f"{'Владелец' if r['role'] == 'user' else 'Ассистент'}: {r['content'][:800]}"
        for r in to_compress
    )
    summarizer, enabled = await build("memory_summarizer")
    if not enabled:
        return
    prompt = f"Текущая выжимка:\n{state['summary'] or '(пусто)'}\n\nНовая часть диалога:\n{dialog}\n\nОбнови выжимку."
    result = await summarizer.run(prompt)
    await db.execute(
        "UPDATE dialog_state SET summary = $1, summarized_to = $2, updated_at = now() WHERE id = 1",
        result.output.strip()[:3000],
        to_compress[-1]["id"],
    )
