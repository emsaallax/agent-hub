"""Оркестратор: единая точка входа. Читает сообщение владельца, решает, что делать,
раздаёт задачи под-агентам через инструменты, отвечает в WhatsApp."""

import asyncio
import logging
from typing import Literal

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from . import db, memory, monitoring, tasks, wa
from .llm import orchestrator_model
from .subagents import code, lead, outreach, product

log = logging.getLogger(__name__)

SYSTEM = """Ты — личный ассистент-оркестратор владельца малого бизнеса. Общение в WhatsApp, по-русски.

Твои под-агенты (вызываются инструментами):
- Поиск товаров: сравнение цен, поставщики, новинки. Результат — таблица + сводка.
- Поиск клиентов: компании с телефонами по нише и городу (2GIS + веб).
- Рассылка: черновики пишет дешёвая модель, вычитывает сильная, отправка ТОЛЬКО после аппрува владельца.
- Входящие от клиентов разбираются автоматически — ты получишь уведомления.
- Кодовые задачи: дешёвая модель пишет, сильная ревьюит.
- Мониторинг цен по списку.
- Реклама: модуль ещё не подключён (честно говори об этом).

Правила:
- Пиши коротко, по делу, как живой помощник в мессенджере. Без канцелярита.
- Долгие работы запускай инструментами start_* — они уходят в фон, владельцу придёт уведомление. Не обещай мгновенный результат.
- Если запрос сложный или неоднозначный — сначала переспроси или предложи план одним коротким сообщением, не жги ресурсы на догадки.
- Рассылку без аппрува не отправляй никогда. Аппрув — через approve_outreach.
- Для справки о прошлых делах используй search_memory.
- Если просят что-то вне твоих инструментов — скажи прямо, что пока не умеешь, и предложи ближайшую альтернативу."""

orchestrator = Agent(orchestrator_model(), output_type=str, system_prompt=SYSTEM, retries=2)

_lock = asyncio.Lock()


# ===== Товары =====

@orchestrator.tool_plain
async def start_product_search(
    query: str, mode: Literal["compare", "suppliers", "new"] = "compare"
) -> str:
    """Запустить фоновый поиск товаров. mode: compare — сравнить цены, suppliers — найти поставщиков/опт, new — свежие предложения."""
    task_id = await tasks.create("product_search", f"{mode}: {query}")
    tasks.start(task_id, lambda: product.run(query, mode))
    return f"Задача #{task_id} запущена (поиск товаров: {query}). Результат придёт сообщением."


@orchestrator.tool_plain
async def watch_product(url: str, title: str = "") -> str:
    """Добавить товар в мониторинг цен по ссылке (WB или любой сайт)."""
    return await monitoring.add_watch(url, title)


@orchestrator.tool_plain
async def list_watched_products() -> str:
    """Показать список товаров в мониторинге цен."""
    return await monitoring.list_watched()


@orchestrator.tool_plain
async def unwatch_product(product_id: int) -> str:
    """Убрать товар из мониторинга по id."""
    return await monitoring.unwatch(product_id)


@orchestrator.tool_plain
async def run_price_check_now() -> str:
    """Запустить внеплановую проверку всех отслеживаемых цен прямо сейчас."""
    tasks.spawn(monitoring.tick())
    return "Запустил проверку цен. Если будут изменения — пришлю."


# ===== Клиенты =====

@orchestrator.tool_plain
async def start_lead_search(niche: str, city: str, count: int = 20) -> str:
    """Запустить фоновый поиск потенциальных клиентов: компании с телефонами по нише и городу."""
    task_id = await tasks.create("lead_search", f"{niche} / {city} / {count}")
    tasks.start(task_id, lambda: lead.run(niche, city, count))
    return f"Задача #{task_id} запущена (клиенты: {niche}, {city}). Результат придёт сообщением."


@orchestrator.tool_plain
async def lead_overview() -> str:
    """Сводка по лидам: сколько в каждом статусе."""
    rows = await db.fetch("SELECT status, count(*) AS n FROM leads GROUP BY status ORDER BY n DESC")
    if not rows:
        return "Лидов в базе пока нет."
    labels = {
        "new": "новые", "queued": "в очереди", "contacted": "написали",
        "interested": "интерес", "question": "вопросы", "declined": "отказ", "spam": "спам",
    }
    return "Лиды: " + ", ".join(f"{labels.get(r['status'], r['status'])} — {r['n']}" for r in rows)


# ===== Рассылка =====

@orchestrator.tool_plain
async def prepare_outreach(offer: str, niche: str = "", city: str = "", limit: int = 10) -> str:
    """Подготовить черновики первых сообщений новым лидам. offer — что предлагаем клиентам (своими словами). Черновики придут владельцу на аппрув."""
    task_id = await tasks.create("outreach_prepare", f"{offer} ({niche} {city}, {limit})")
    tasks.start(task_id, lambda: outreach.prepare(offer, niche, city, limit))
    return f"Задача #{task_id}: готовлю черновики, пришлю их на аппрув."


@orchestrator.tool_plain
async def list_pending_outreach() -> str:
    """Показать черновики рассылки, ожидающие аппрува."""
    return await outreach.list_pending()


@orchestrator.tool_plain
async def approve_outreach(ids: list[int] | None = None) -> str:
    """Одобрить черновики рассылки. ids — номера черновиков; пустой список или None = одобрить все ожидающие."""
    return await outreach.approve(ids or [])


@orchestrator.tool_plain
async def reject_outreach(ids: list[int]) -> str:
    """Отклонить черновики рассылки по номерам."""
    return await outreach.reject(ids)


@orchestrator.tool_plain
async def send_to_lead(lead_id: int, text: str) -> str:
    """Отправить конкретному лиду сообщение с рассыльного номера (например, ответ на его вопрос)."""
    return await outreach.send_to_lead(lead_id, text)


# ===== Код =====

@orchestrator.tool_plain
async def start_code_task(description: str) -> str:
    """Запустить кодовую задачу: дешёвая модель пишет код, сильная ревьюит (до 3 итераций). Файлы лягут в data/code/."""
    task_id = await tasks.create("code", description)
    tasks.start(task_id, lambda: code.run(task_id, description))
    return f"Задача #{task_id} запущена (код). Пришлю результат с вердиктом ревью."


# ===== Реклама (заглушка в реестре) =====

@orchestrator.tool_plain
async def start_ads_task(description: str) -> str:
    """Модуль рекламы (VK Ads / Яндекс.Директ). Пока не реализован."""
    return (
        "Модуль рекламы пока не подключён — он в плане расширения. "
        "Когда решишь подключать VK Ads или Яндекс.Директ, добавим его как нового под-агента."
    )


# ===== Задачи и память =====

@orchestrator.tool_plain
async def get_tasks(limit: int = 5) -> str:
    """Статусы последних задач."""
    rows = await db.fetch(
        "SELECT id, kind, status, request FROM tasks ORDER BY id DESC LIMIT $1", limit
    )
    if not rows:
        return "Задач ещё не было."
    icons = {"pending": "⏳", "running": "⚙️", "done": "✅", "error": "❌"}
    return "\n".join(
        f"{icons.get(r['status'], '•')} #{r['id']} {r['kind']}: {r['request'][:80]}" for r in rows
    )


@orchestrator.tool_plain
async def search_memory(query: str) -> str:
    """Поиск по архиву прошлых задач и договорённостей."""
    results = await memory.search_archive(query)
    return "\n\n".join(results) if results else "В архиве ничего не нашлось."


@orchestrator.tool_plain
async def remember(fact: str) -> str:
    """Запомнить важный факт навсегда (владелец явно попросил запомнить)."""
    await db.execute("INSERT INTO memory_facts (fact, category) VALUES ($1, 'manual')", fact)
    return "Запомнил."


# ===== Главный цикл =====

async def handle_owner_message(text: str) -> None:
    async with _lock:  # сообщения владельца обрабатываем по одному
        context = await memory.build_context_block()  # контекст до записи нового сообщения, чтобы не дублировать его
        await memory.add_message("user", text)
        prompt = f"{context}\n\n---\nНовое сообщение владельца:\n{text}"
        try:
            result = await orchestrator.run(prompt, usage_limits=UsageLimits(request_limit=12))
            reply = result.output.strip() or "Принял."
        except Exception as e:
            log.exception("orchestrator failed")
            reply = f"⚠️ Ошибка оркестратора: {e}"
        await memory.add_message("assistant", reply)
        await wa.notify_owner(reply)
    tasks.spawn(memory.maintain())
