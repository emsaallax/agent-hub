"""LeadAgent: анализ рынка — компании с контактами по нише и городу."""

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from .. import db
from ..config import normalize_phone
from ..llm import cheap_model
from ..tools import scraper, sheets, twogis, web_search


class CompanyOut(BaseModel):
    name: str
    phone: str = ""  # с кодом страны, только цифры
    website: str = ""
    note: str = ""  # чем занимаются, зацепка для первого сообщения


class LeadReport(BaseModel):
    companies: list[CompanyOut]
    summary: str


SYSTEM = (
    "Ты — агент поиска потенциальных клиентов (компаний) для владельца бизнеса. По-русски.\n"
    "Инструменты: twogis (организации с телефонами по нише и городу — используй первым), "
    "search_web (общий поиск), fetch_page (контакты с сайтов).\n"
    "Цель: компании с РАБОЧИМИ телефонами. Телефон — только цифры с кодом страны (7...). "
    "Не выдумывай контакты — только из инструментов. В note пиши зацепку: чем компания живёт, "
    "что ей можно предложить."
)

_agent = Agent(cheap_model(), output_type=LeadReport, system_prompt=SYSTEM, retries=2)


@_agent.tool_plain
async def twogis_search(niche: str, city: str) -> str:
    """Организации в 2GIS по нише и городу: название, телефон, сайт, адрес."""
    try:
        return twogis.format_results(await twogis.search_companies(niche, city))
    except Exception as e:
        return f"2GIS недоступен: {e}"


@_agent.tool_plain
async def search_web(query: str) -> str:
    """Веб-поиск (Google)."""
    return web_search.format_results(await web_search.search(query))


@_agent.tool_plain
async def fetch_page(url: str) -> str:
    """Открыть страницу сайта (контакты, описание компании)."""
    try:
        return await scraper.fetch_text(url)
    except Exception as e:
        return f"Не удалось открыть {url}: {e}"


async def _upsert_company(c: CompanyOut, niche: str, city: str) -> bool:
    """True, если компания новая."""
    phone = normalize_phone(c.phone)
    if phone:
        existing = await db.fetchval("SELECT id FROM companies WHERE phone = $1", phone)
        if existing:
            return False
    company_id = await db.fetchval(
        """
        INSERT INTO companies (name, phone, website, city, niche, note, source)
        VALUES ($1, $2, $3, $4, $5, $6, 'agent')
        RETURNING id
        """,
        c.name[:200],
        phone,
        c.website[:300],
        city[:100],
        niche[:100],
        c.note[:500],
    )
    await db.execute(
        "INSERT INTO leads (company_id) VALUES ($1) ON CONFLICT (company_id) DO NOTHING",
        company_id,
    )
    return True


async def run(niche: str, city: str, count: int = 20) -> str:
    prompt = (
        f"Найди до {count} компаний: ниша «{niche}», город «{city}». "
        f"Обязательно телефоны. Начни с 2GIS, добери вебом, если мало."
    )
    result = await _agent.run(prompt, usage_limits=UsageLimits(request_limit=15))
    report = result.output

    new_count = 0
    rows = []
    for c in report.companies:
        if await _upsert_company(c, niche, city):
            new_count += 1
        rows.append([c.name, normalize_phone(c.phone) or "—", c.website, c.note])

    sheet_line = ""
    if rows:
        sheet_url, csv_path = await sheets.export_table(
            f"Лиды — {niche[:30]}, {city[:20]}",
            ["Компания", "Телефон", "Сайт", "Заметка"],
            rows,
        )
        sheet_line = "\n\n" + sheets.table_link_line(sheet_url, csv_path)

    with_phone = sum(1 for c in report.companies if normalize_phone(c.phone))
    return (
        f"{report.summary}\n\n"
        f"Найдено компаний: {len(report.companies)}, с телефонами: {with_phone}, новых в базе: {new_count}."
        f"{sheet_line}\n\n"
        f"Когда будешь готов — скажи «подготовь рассылку», я сделаю черновики на аппрув."
    )
