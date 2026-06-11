"""Researcher: универсальный субагент для произвольных анализов и исследований.

Любая задача, не попадающая в специализированных агентов: «проанализируй, кому
предлагать мою услугу», «изучи конкурентов», «разберись в теме X». Может читать
vault (включая установленные скиллы) и пишет результат заметкой.
"""

from pydantic_ai.usage import UsageLimits

from .. import vault
from ..agents_registry import AgentSpec, build, register
from ..tools import scraper, web_search

SYSTEM = (
    "Ты — исследователь-аналитик владельца малого бизнеса. По-русски.\n"
    "Тебе дают бриф — проведи исследование и дай практичный, конкретный ответ.\n"
    "Инструменты: search_web (веб-поиск), fetch_page (открыть страницу), "
    "vault_search (заметки и база знаний владельца — проверь её первой: там контекст бизнеса и скиллы).\n"
    "Не выдумывай факты: всё спорное проверяй поиском. Структурируй ответ markdown-заголовками.\n"
    "Заверши блоком «Выводы и следующие шаги» — 3–5 конкретных пунктов."
)


async def search_web(query: str) -> str:
    """Веб-поиск (Google). Возвращает заголовки, ссылки, сниппеты."""
    return web_search.format_results(await web_search.search(query))


async def fetch_page(url: str) -> str:
    """Открыть страницу и получить её текст."""
    try:
        return await scraper.fetch_text(url)
    except Exception as e:
        return f"Не удалось открыть {url}: {e}"


async def vault_search(query: str) -> str:
    """Поиск по заметкам и базе знаний владельца (vault + скиллы)."""
    results = await vault.search(query)
    if not results:
        return "В vault ничего не нашлось."
    return "\n\n".join(f"[{r['path']}]\n{r['snippet']}" for r in results)


register(
    AgentSpec(
        name="researcher",
        title="Исследователь",
        tier="cheap",
        prompt=SYSTEM,
        description="Произвольные исследования и анализ: рынок, конкуренты, ниши, любые вопросы.",
        tools=[search_web, fetch_page, vault_search],
        use_mcp=True,
    )
)


async def run(brief: str) -> str:
    agent, enabled = await build("researcher")
    if not enabled:
        return "Исследователь выключен в админке."
    result = await agent.run(brief, usage_limits=UsageLimits(request_limit=15))
    report = result.output.strip()

    title = brief.replace("[auto]", "").strip()[:60]
    note_path = await vault.write_note(f"Исследования/{title}", f"# {title}\n\nБриф: {brief}\n\n{report}")
    return f"{report}\n\n(сохранил в заметку «{note_path}»)"
