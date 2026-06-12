"""ProductAgent: поиск товаров/поставщиков/новинок."""

from pydantic import BaseModel
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from .. import errlog
from ..agents_registry import AgentSpec, build, register, run_safe
from ..tools import scraper, sheets, web_search, wildberries


class ProductItem(BaseModel):
    title: str
    price: str  # строкой: "1 290 ₽", "от 800 ₽/шт при опте"
    source: str  # WB | сайт | поставщик
    url: str
    note: str = ""


class ProductReport(BaseModel):
    items: list[ProductItem]
    summary: str  # короткий вывод для владельца: где выгоднее, на что смотреть


SYSTEM = (
    "Ты — агент поиска товаров для владельца бизнеса. Работаешь по-русски.\n"
    "Используй инструменты: wb_search (Wildberries), search_web (общий поиск), fetch_page (открыть страницу).\n"
    "Собирай конкретику: точное название, цена, ссылка. Не выдумывай цены — только из результатов инструментов.\n"
    "Для поставщиков ищи: 'купить оптом <товар>', 'производитель <товар>', и доставай телефоны/сайты со страниц.\n"
    "В summary дай короткий вывод: лучшие варианты, разброс цен, рекомендация."
)

MODE_PROMPTS = {
    "compare": "Найди 5–10 актуальных предложений по запросу «{query}», сравни цены, отметь самые выгодные.",
    "suppliers": "Найди поставщиков/производителей по запросу «{query}»: компании, цены при опте, минимальные партии, контакты.",
    "new": "Найди свежие/новые предложения по критериям «{query}»: новинки, недавно появившиеся позиции.",
}


async def search_web(query: str) -> str:
    """Веб-поиск (Google). Возвращает заголовки, ссылки, сниппеты."""
    return web_search.format_results(await web_search.search(query))


async def wb_search(query: str) -> str:
    """Поиск товаров на Wildberries: название, бренд, цена, рейтинг, ссылка."""
    try:
        return wildberries.format_results(await wildberries.search(query))
    except Exception as e:
        return f"WB недоступен: {e}"


async def fetch_page(url: str) -> str:
    """Открыть страницу и получить её текст (для цен и контактов с сайтов)."""
    try:
        return await scraper.fetch_text(url)
    except Exception as e:
        return f"Не удалось открыть {url}: {e}"


register(
    AgentSpec(
        name="product",
        title="Поиск товаров",
        tier="cheap",
        prompt=SYSTEM,
        description="Сравнение цен, поиск поставщиков, новинки. Результат — таблица + сводка.",
        output_type=ProductReport,
        tools=[search_web, wb_search, fetch_page],
    )
)


async def run(query: str, mode: str = "compare") -> str:
    prompt = MODE_PROMPTS.get(mode, MODE_PROMPTS["compare"]).format(query=query)
    try:
        result_tuple = await run_safe("product", prompt, usage_limits=UsageLimits(request_limit=25))
    except UsageLimitExceeded as e:
        await errlog.record("agent", f"product: {query[:60]}", e)
        return "⚠️ Поиск товаров прерван: слишком широкий запрос. Уточни категорию или уменьши охват."
    result, enabled = result_tuple
    if not enabled:
        return "Агент поиска товаров выключен в админке."
    report = result.output

    summary = report.summary
    if report.items:
        rows = [[i.title, i.price, i.source, i.url, i.note] for i in report.items]
        sheet_url, csv_path = await sheets.export_table(
            f"Товары — {query[:40]}",
            ["Название", "Цена", "Источник", "Ссылка", "Заметка"],
            rows,
        )
        top = "\n".join(
            f"• {i.title} — {i.price} ({i.source})" for i in report.items[:5]
        )
        summary = f"{report.summary}\n\nТоп находок:\n{top}\n\n{sheets.table_link_line(sheet_url, csv_path)}"
    return summary
