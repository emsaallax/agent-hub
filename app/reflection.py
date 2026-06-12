"""Рефлексия: ассистент анализирует свою работу, копит уроки и видит свой потенциал.

Раз в сутки (и по запросу) дешёвая модель просматривает последние задачи и ошибки,
пишет разбор в vault (Рефлексия/ГГГГ-ММ-ДД.md), а ключевые уроки кладёт в memory_facts
(категория lesson) — они попадают в контекст оркестратора и реально меняют поведение.
"""

import logging
from datetime import datetime

from pydantic import BaseModel

from . import db, errlog, memory, vault
from .agents_registry import REGISTRY, AgentSpec, build, register

log = logging.getLogger(__name__)

MAX_LESSONS_PER_RUN = 3
MAX_LESSON_FACTS = 30


class Reflection(BaseModel):
    analysis: str        # разбор: что получилось, что нет, где потенциал (markdown)
    lessons: list[str]   # короткие уроки-правила на будущее (0–3 шт)


SYSTEM = (
    "Ты — модуль саморефлексии ассистента-оркестратора. По-русски.\n"
    "Тебе дают: список возможностей системы, последние задачи со статусами и ошибки.\n"
    "Сделай честный разбор (analysis): что сработало, что падало и почему, какие запросы "
    "владельца система решала плохо, где недоиспользованный потенциал (какие возможности "
    "владелец не применяет, что стоило бы улучшить или подключить).\n"
    "В lessons — до 3 коротких правил на будущее, КОНКРЕТНЫХ и применимых "
    "(например: «поиск клиентов в маленьких городах давать с count<=15, иначе пусто»). "
    "Не повторяй уже известные уроки. Если новых уроков нет — пустой список."
)

register(
    AgentSpec(
        name="reflector",
        title="Рефлексия",
        tier="cheap",
        prompt=SYSTEM,
        description="Ежесуточный самоанализ: разбор работы в vault + уроки в память.",
        output_type=Reflection,
    )
)


async def _gather_material() -> str:
    tasks_rows = await db.fetch(
        """
        SELECT id, kind, status, request, left(coalesce(result, ''), 300) AS result
        FROM tasks WHERE updated_at >= now() - interval '7 days'
        ORDER BY id DESC LIMIT 30
        """
    )
    lessons = await db.fetch(
        "SELECT fact FROM memory_facts WHERE active AND category = 'lesson' ORDER BY id DESC LIMIT 20"
    )
    errors = await db.fetch(
        """
        SELECT source, ref, error_class, message, created_at
        FROM error_log WHERE created_at >= now() - interval '7 days'
        ORDER BY id DESC LIMIT 15
        """
    )
    capabilities = "\n".join(
        f"- {s.title} ({s.name}): {s.description}" for s in REGISTRY.values()
    )
    tasks_block = "\n".join(
        f"#{r['id']} [{r['status']}] {r['kind']}: {r['request'][:120]} → {r['result'][:200]}"
        for r in tasks_rows
    ) or "Задач за неделю не было."
    lessons_block = "\n".join(f"- {r['fact']}" for r in lessons) or "(пока нет)"
    errors_block = "\n".join(
        f"[{r['created_at']:%d.%m %H:%M}] {r['source']} {r['ref']} ({r['error_class']}): {r['message'][:150]}"
        for r in errors
    ) or "(ошибок не записано)"
    return (
        f"Возможности системы:\n{capabilities}\n\n"
        f"Задачи за 7 дней:\n{tasks_block}\n\n"
        f"Журнал ошибок за 7 дней (классифицированный):\n{errors_block}\n\n"
        f"Уже выученные уроки:\n{lessons_block}"
    )


async def run_reflection() -> str:
    """Прогнать рефлексию: заметка в vault + новые уроки в память. Возвращает краткий итог."""
    reflector, enabled = await build("reflector")
    if not enabled:
        return "Рефлексия выключена в админке."

    material = await _gather_material()
    result = (await reflector.run(material)).output

    today = datetime.now().strftime("%Y-%m-%d")
    await vault.append_note(
        f"Рефлексия/{today}.md",
        f"\n## Разбор {datetime.now():%H:%M}\n{result.analysis}\n",
    )

    saved = 0
    n_lessons = await db.fetchval(
        "SELECT count(*) FROM memory_facts WHERE active AND category = 'lesson'"
    )
    for lesson in result.lessons[:MAX_LESSONS_PER_RUN]:
        lesson = lesson.strip()
        if not lesson or n_lessons + saved >= MAX_LESSON_FACTS:
            continue
        await db.execute(
            "INSERT INTO memory_facts (fact, category) VALUES ($1, 'lesson')", lesson
        )
        saved += 1

    summary = f"Рефлексия готова: разбор в vault (Рефлексия/{today}.md), новых уроков: {saved}."
    if result.lessons:
        summary += "\n" + "\n".join(f"- {l}" for l in result.lessons[:MAX_LESSONS_PER_RUN])
    return summary


async def daily_job() -> None:
    """Для планировщика: тихая ежесуточная рефлексия + актуализация фактов."""
    try:
        await memory.curate_facts()
    except Exception as e:
        log.exception("daily fact curation failed")
        await errlog.record("memory", "fact_curator", e)
    try:
        await run_reflection()
        log.info("daily reflection done")
    except Exception as e:
        log.exception("daily reflection failed")
        await errlog.record("task", "reflection (ежесуточная)", e)
