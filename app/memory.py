"""Память: рабочее окно диалога + rolling summary + факты + полнотекстовый архив.

Принцип экономии токенов: в промпт попадает только короткий блок контекста,
а не вся история. Старое сжимается дешёвой моделью в фоне.
"""

import logging

from . import db
from .agents_registry import AgentSpec, build, register

log = logging.getLogger(__name__)

WINDOW = 15           # сколько последних сообщений держим в промпте
SUMMARIZE_AFTER = 40  # при скольких несжатых сообщениях запускать суммаризацию
MAX_FACTS = 100
MSG_CAP = 1500        # обрезка одного сообщения в контексте

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
    except Exception:
        log.exception("fact extraction failed")
    try:
        await _maybe_summarize()
    except Exception:
        log.exception("summarization failed")


async def _extract_facts() -> None:
    n_facts = await db.fetchval("SELECT count(*) FROM memory_facts WHERE active")
    if n_facts >= MAX_FACTS:
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
    extractor, enabled = await build("fact_extractor")
    if not enabled:
        return
    known = await get_facts()
    prompt = (
        "Уже известные факты:\n" + "\n".join(f"- {f}" for f in known[:40]) + "\n\nФрагмент диалога:\n" + dialog
    )
    result = await extractor.run(prompt)
    for fact in result.output[:3]:
        fact = fact.strip()
        if fact:
            await db.execute("INSERT INTO memory_facts (fact) VALUES ($1)", fact)


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
