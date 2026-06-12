"""Researcher: универсальный субагент для произвольных анализов и исследований.

Любая задача, не попадающая в специализированных агентов: «проанализируй, кому
предлагать мою услугу», «изучи конкурентов», «разберись в теме X». Может читать
vault (включая установленные скиллы) и пишет результат заметкой.
"""

from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from .. import errlog, vault
from ..agents_registry import AgentSpec, build, register, run_safe
from ..tools import scraper, web_search

SYSTEM = (
    "Ты — исследователь-аналитик владельца малого бизнеса. По-русски.\n"
    "Тебе дают бриф — проведи исследование и дай практичный, конкретный ответ.\n"
    "Инструменты: search_web (веб-поиск), fetch_page (открыть страницу), "
    "vault_search (заметки и база знаний владельца — проверь её первой: там контекст бизнеса и скиллы).\n"
    "Не выдумывай факты: всё спорное проверяй поиском.\n\n"
    "Структура ответа:\n"
    "1. Краткая выжимка для владельца — до 800 символов, БЕЗ таблиц и ## заголовков.\n"
    "   Формат WhatsApp: *жирный* (одна звёздочка), списки через дефис, без таблиц.\n"
    "   Заверши 3–5 конкретными следующими шагами.\n"
    "2. После выжимки — разделитель: ===\n"
    "3. Полный разбор в свободном формате (markdown-таблицы, заголовки — допустимо, "
    "он пойдёт только в vault, не в WhatsApp).\n"
    "Весь текст (выжимка + полный разбор) — это твой итоговый output."
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
    try:
        result_tuple = await run_safe("researcher", brief, usage_limits=UsageLimits(request_limit=30))
    except UsageLimitExceeded as e:
        await errlog.record("agent", f"researcher: {brief[:80]}", e)
        return (
            "⚠️ Исследование прервано: слишком много шагов для одного запроса. "
            "Сузи тему или разбей на 2–3 отдельных вопроса."
        )
    result, enabled = result_tuple
    if not enabled:
        return "Исследователь выключен в админке."
    report = result.output.strip()

    title = brief.replace("[auto]", "").strip()[:60]

    # Разделяем: выжимка для WhatsApp | полный разбор для vault
    if "===" in report:
        parts = report.split("===", 1)
        wa_part = parts[0].strip()
        full_part = parts[1].strip()
    else:
        wa_part = report[:900].strip()
        full_part = report

    note_path = await vault.write_note(
        f"Исследования/{title}",
        f"# {title}\n\nБриф: {brief}\n\n{full_part}",
    )
    return f"{wa_part}\n\n📄 Полный разбор сохранён в vault: «{note_path}»"
