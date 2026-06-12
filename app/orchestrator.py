"""Оркестратор: единая точка входа. Читает сообщение владельца, решает, что делать,
раздаёт задачи под-агентам через инструменты, отвечает в WhatsApp.

После завершения фоновой задачи сам осмысляет результат (after_task) и, в зависимости
от уровня автономии, предлагает или запускает следующий шаг.
"""

import asyncio
import datetime as _dt
import logging
from contextvars import ContextVar
from typing import Literal

from pydantic_ai.usage import UsageLimits

from . import db, errlog, memory, monitoring, reflection, settings_store, tasks, vault, wa
from .agents_registry import AgentSpec, build, register, run_safe
from .subagents import code, lead, outreach, product, researcher

log = logging.getLogger(__name__)

SYSTEM = """Ты — личный ассистент-оркестратор владельца малого бизнеса. Общение в WhatsApp, по-русски.

Твои под-агенты (вызываются инструментами):
- Исследователь (start_research): ЛЮБОЙ анализ — кому предлагать услугу, конкуренты, рынок, разбор темы. Если запрос аналитический и не подходит под другие инструменты — это сюда.
- Поиск товаров: сравнение цен, поставщики, новинки. Результат — таблица + сводка.
- Поиск клиентов: компании с телефонами по нише и городу (2GIS + веб).
- Рассылка: черновики пишет дешёвая модель, вычитывает сильная, отправка ТОЛЬКО после аппрува владельца.
- Входящие от клиентов разбираются автоматически — ты получишь уведомления.
- Кодовые задачи: дешёвая модель пишет, сильная ревьюит.
- Мониторинг цен по списку.
- Реклама: модуль ещё не подключён (честно говори об этом).

Память и знания:
- Vault — твоя база заметок (журнал задач, исследования, скиллы с GitHub, рефлексии). vault_search — ищи там контекст, vault_write — записывай важное, vault_read — читай заметку целиком.
- search_memory — архив прошлых задач; remember — запомнить факт навсегда.
- reflect_now — твоя саморефлексия: разбор своей работы, уроки в память.

Запрещено:
- "Хорошо, сейчас сделаю", "Понял, занимаюсь", "Конечно!" и любые шаблонные подтверждения.
- Пересказывать владельцу то, что он только что написал.
- Извиняться и объяснять что ты не можешь — просто скажи коротко что пока не умеешь.
- Завершать сообщение вопросом "Есть ли ещё что-нибудь?" или подобными.
Стиль: деловой мессенджер, не чат-бот. Короче, конкретнее, никакой воды.

Правила работы:
- Пиши коротко, по делу, как живой помощник в мессенджере. Без канцелярита.
- Долгие работы запускай инструментами start_* — они уходят в фон, владельцу придёт уведомление. Не обещай мгновенный результат.
- Если ты уже вызвал start_* инструмент — НЕ пересказывай владельцу его ответ. Ответь одной короткой строкой, что запустил, и жди. Результат придёт отдельным сообщением.
- Показать ход задачи («покажи мысли/шаги задачи #N») — вызови get_task_trace.
- О статусе задач НИКОГДА не отвечай по памяти — сначала вызови get_tasks и пересказывай только его ответ. Если задача done — дай результат, не говори, что она ещё идёт.
- Завершение задачи появляется в истории сообщением «✅ Задача #N завершена». Нет такого сообщения — проверь get_tasks, прежде чем что-то утверждать.
- Одну и ту же задачу повторно не запускай: инструменты сами скажут, если такая уже выполняется.
- Будь проактивным: видишь логичный следующий шаг — предложи его сам, не жди вопроса.
- Если запрос сложный или неоднозначный — сначала переспроси или предложи план одним коротким сообщением, не жги ресурсы на догадки.
- Рассылку без аппрува не отправляй никогда. Аппрув — через approve_outreach.
- Если просят что-то вне твоих инструментов — скажи прямо, что пока не умеешь, и предложи ближайшую альтернативу.

Формат сообщений (СТРОГО — читают в WhatsApp на телефоне):
- Одно сообщение = один экран телефона. Максимум 800 символов в ответе.
- Никаких таблиц (| col | col |) — WhatsApp их не рендерит, получается мусор.
- Никаких заголовков ## — вместо этого *жирная строка* на отдельной строке.
- Жирный текст: *одна звёздочка*, НЕ двойная (**текст**).
- Для списков — только дефис или цифра с точкой.
- Когда результат исследования большой — дай выжимку: 3-5 главных пунктов. Полный отчёт уже сохранён в vault, не нужно дублировать его в чат."""

_queue: asyncio.Queue[str] = asyncio.Queue()
_worker_task: asyncio.Task | None = None

_in_after_task: ContextVar[bool] = ContextVar("in_after_task", default=False)

_health: dict = {
    "last_ok_at": None,
    "last_error_at": None,
    "last_error_msg": None,
    "model": None,
}


def get_health() -> dict:
    return dict(_health)

PROACTIVE_KINDS = {"product_search", "lead_search", "research", "code", "outreach_prepare"}


# ===== Товары =====

async def start_product_search(
    query: str, mode: Literal["compare", "suppliers", "new"] = "compare"
) -> str:
    """Запустить фоновый поиск товаров. mode: compare — сравнить цены, suppliers — найти поставщиков/опт, new — свежие предложения."""
    request = f"{mode}: {query}"
    dup = await tasks.find_active("product_search", request)
    if dup:
        return f"Такая задача уже выполняется (#{dup['id']}). Дубль не запускаю, результат придёт сообщением."
    task_id = await tasks.create("product_search", request)
    tasks.start(task_id, lambda: product.run(query, mode))
    return f"Задача #{task_id} запущена (поиск товаров: {query}). Результат придёт сообщением."


async def watch_product(url: str, title: str = "") -> str:
    """Добавить товар в мониторинг цен по ссылке (WB или любой сайт)."""
    return await monitoring.add_watch(url, title)


async def list_watched_products() -> str:
    """Показать список товаров в мониторинге цен."""
    return await monitoring.list_watched()


async def unwatch_product(product_id: int) -> str:
    """Убрать товар из мониторинга по id."""
    return await monitoring.unwatch(product_id)


async def run_price_check_now() -> str:
    """Запустить внеплановую проверку всех отслеживаемых цен прямо сейчас."""
    tasks.spawn(monitoring.tick())
    return "Запустил проверку цен. Если будут изменения — пришлю."


# ===== Клиенты =====

async def start_lead_search(niche: str, city: str, count: int = 20) -> str:
    """Запустить фоновый поиск потенциальных клиентов: компании с телефонами по нише и городу."""
    request = f"{niche} / {city} / {count}"
    dup = await tasks.find_active("lead_search", request)
    if dup:
        return f"Такая задача уже выполняется (#{dup['id']}). Дубль не запускаю, результат придёт сообщением."
    task_id = await tasks.create("lead_search", request)
    tasks.start(task_id, lambda: lead.run(niche, city, count))
    return f"Задача #{task_id} запущена (клиенты: {niche}, {city}). Результат придёт сообщением."


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

async def prepare_outreach(offer: str, niche: str = "", city: str = "", limit: int = 10) -> str:
    """Подготовить черновики первых сообщений новым лидам. offer — что предлагаем клиентам (своими словами). Черновики придут владельцу на аппрув."""
    request = f"{offer} ({niche} {city}, {limit})"
    dup = await tasks.find_active("outreach_prepare", request)
    if dup:
        return f"Черновики уже готовятся (задача #{dup['id']}). Дубль не запускаю."
    task_id = await tasks.create("outreach_prepare", request)
    tasks.start(task_id, lambda: outreach.prepare(offer, niche, city, limit))
    return f"Задача #{task_id}: готовлю черновики, пришлю их на аппрув."


async def list_pending_outreach() -> str:
    """Показать черновики рассылки, ожидающие аппрува."""
    return await outreach.list_pending()


async def approve_outreach(ids: list[int] | None = None) -> str:
    """Одобрить черновики рассылки. ids — номера черновиков; пустой список или None = одобрить все ожидающие."""
    return await outreach.approve(ids or [])


async def reject_outreach(ids: list[int]) -> str:
    """Отклонить черновики рассылки по номерам."""
    return await outreach.reject(ids)


async def send_to_lead(lead_id: int, text: str) -> str:
    """Отправить конкретному лиду сообщение с рассыльного номера (например, ответ на его вопрос)."""
    return await outreach.send_to_lead(lead_id, text)


# ===== Исследования =====

async def start_research(brief: str) -> str:
    """Запустить фоновое исследование/анализ на любую тему: кому предлагать услугу, конкуренты, рынок, разбор вопроса. brief — подробное задание своими словами."""
    if _in_after_task.get():
        brief = f"[auto] {brief}"
    dup = await tasks.find_active("research", brief)
    if dup:
        return f"Такое исследование уже идёт (#{dup['id']}). Дубль не запускаю."
    task_id = await tasks.create("research", brief)
    tasks.start(task_id, lambda: researcher.run(brief))
    return f"Задача #{task_id} запущена (исследование). Результат придёт сообщением и ляжет в vault."


# ===== Vault (заметки) =====

async def vault_search(query: str) -> str:
    """Поиск по заметкам vault: журнал задач, исследования, скиллы, рефлексии, заметки владельца."""
    results = await vault.search(query)
    if not results:
        return "В vault ничего не нашлось."
    return "\n\n".join(f"[{r['path']}]\n{r['snippet']}" for r in results)


async def vault_write(path: str, content: str, append: bool = True) -> str:
    """Записать заметку в vault. path — например 'Идеи/Реклама.md'. append=true — дописать в конец, false — перезаписать."""
    saved = await (vault.append_note(path, content) if append else vault.write_note(path, content))
    return f"Записал в «{saved}»."


async def vault_read(path: str) -> str:
    """Прочитать заметку vault целиком по её пути."""
    content = await vault.read_note(path)
    if content is None:
        return f"Заметки «{path}» нет. Найди точный путь через vault_search."
    return content[:6000]


# ===== Рефлексия =====

async def reflect_now() -> str:
    """Запустить саморефлексию: честный разбор последних задач, уроки в память, заметка в vault."""
    dup = await tasks.find_active("reflection", "ручной запуск")
    if dup:
        return f"Рефлексия уже идёт (#{dup['id']})."
    task_id = await tasks.create("reflection", "ручной запуск")
    tasks.start(task_id, reflection.run_reflection)
    return f"Запустил рефлексию (#{task_id}) — пришлю разбор."


# ===== Код =====

async def start_code_task(description: str) -> str:
    """Запустить кодовую задачу: дешёвая модель пишет код, сильная ревьюит (до 3 итераций). Файлы лягут в data/code/."""
    dup = await tasks.find_active("code", description)
    if dup:
        return f"Такая кодовая задача уже выполняется (#{dup['id']}). Дубль не запускаю."
    task_id = await tasks.create("code", description)
    tasks.start(task_id, lambda: code.run(task_id, description))
    return f"Задача #{task_id} запущена (код). Пришлю результат с вердиктом ревью."


# ===== Реклама (заглушка в реестре) =====

async def start_ads_task(description: str) -> str:
    """Модуль рекламы (VK Ads / Яндекс.Директ). Пока не реализован."""
    return (
        "Модуль рекламы пока не подключён — он в плане расширения. "
        "Когда решишь подключать VK Ads или Яндекс.Директ, добавим его как нового под-агента."
    )


# ===== Задачи и память =====

async def get_tasks(limit: int = 5) -> str:
    """Актуальные статусы последних задач (источник правды — БД). Для done/error показывает итог."""
    rows = await db.fetch(
        """
        SELECT id, kind, status, request, result,
               GREATEST(0, EXTRACT(EPOCH FROM (now() - updated_at)) / 60)::int AS mins
        FROM tasks ORDER BY id DESC LIMIT $1
        """,
        limit,
    )
    if not rows:
        return "Задач ещё не было."
    icons = {"pending": "⏳", "running": "⚙️", "done": "✅", "error": "❌"}
    lines = []
    for r in rows:
        line = f"{icons.get(r['status'], '•')} #{r['id']} {r['kind']}: {r['request'][:80]}"
        if r["status"] == "running":
            line += f" — выполняется уже {r['mins']} мин"
        elif r["status"] == "done":
            line += f" — ГОТОВА ({r['mins']} мин назад). Итог: {(r['result'] or '')[:200]}"
        elif r["status"] == "error":
            line += f" — упала: {(r['result'] or '')[:120]}"
        lines.append(line)
    return "\n".join(lines)


async def get_task_trace(task_id: int) -> str:
    """Показать пошаговый трейс задачи («покажи мысли/шаги задачи #N»): что и в каком порядке делал агент."""
    row = await db.fetchrow("SELECT kind, status, trace FROM tasks WHERE id = $1", task_id)
    if not row:
        return f"Задачи #{task_id} нет."
    trace = (row["trace"] or "").strip()
    if not trace:
        return f"Задача #{task_id} ({row['kind']}, {row['status']}): трейс пуст."
    return f"Шаги задачи #{task_id} ({row['kind']}, {row['status']}):\n{trace}"


async def search_memory(query: str) -> str:
    """Поиск по архиву прошлых задач и договорённостей."""
    results = await memory.search_archive(query)
    return "\n\n".join(results) if results else "В архиве ничего не нашлось."


async def remember(fact: str) -> str:
    """Запомнить важный факт навсегда (владелец явно попросил запомнить)."""
    await db.execute("INSERT INTO memory_facts (fact, category) VALUES ($1, 'manual')", fact)
    return "Запомнил."


register(
    AgentSpec(
        name="orchestrator",
        title="Оркестратор",
        tier="orchestrator",
        prompt=SYSTEM,
        description="Главный агент: принимает сообщения владельца и раздаёт задачи под-агентам.",
        use_mcp=True,
        tools=[
            start_research,
            start_product_search, watch_product, list_watched_products,
            unwatch_product, run_price_check_now,
            start_lead_search, lead_overview,
            prepare_outreach, list_pending_outreach, approve_outreach,
            reject_outreach, send_to_lead,
            start_code_task, start_ads_task,
            get_tasks, get_task_trace, search_memory, remember,
            vault_search, vault_write, vault_read, reflect_now,
        ],
    )
)


# ===== Главный цикл =====

async def handle_owner_message(text: str) -> None:
    """Кладёт сообщение владельца в очередь. Обработкой занимается _worker —
    сообщения не блокируют друг друга на приёме, но обрабатываются по одному, по порядку."""
    await _queue.put(text)


async def _process_owner_message(text: str) -> None:
    context = await memory.build_context_block()  # контекст до записи нового сообщения, чтобы не дублировать его
    await memory.add_message("user", text)
    prompt = f"{context}\n\n---\nНовое сообщение владельца:\n{text}"
    try:
        _health["model"] = await settings_store.tier_model("orchestrator")
        result, _ = await run_safe("orchestrator", prompt, usage_limits=UsageLimits(request_limit=6))
        reply = (result.output.strip() if result else "") or "Принял."
        _health["last_ok_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        _health["last_error_at"] = None
        _health["last_error_msg"] = None
    except Exception as e:
        log.exception("orchestrator failed")
        reply = f"⚠️ Ошибка оркестратора: {e}"
        _health["last_error_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        _health["last_error_msg"] = str(e)[:400]
        await errlog.record("orchestrator", f"сообщение: {text[:80]}", e)
    await memory.add_message("assistant", reply)
    await wa.notify_owner(reply)
    tasks.spawn(memory.maintain())


async def _worker() -> None:
    """Однопоточный воркер: берёт сообщения владельца из очереди и обрабатывает по одному.
    Не теряет сообщения и не блокирует приём — приём только кладёт в очередь."""
    log.info("orchestrator worker started")
    while True:
        text = await _queue.get()
        try:
            # Если накопился бэклог — предупредим владельца, что разбираем по очереди.
            backlog = _queue.qsize()
            if backlog > 3:
                await wa.notify_owner(f"Принял {backlog + 1} сообщений, обрабатываю по очереди.")
            await _process_owner_message(text)
        except Exception:
            log.exception("orchestrator worker iteration failed")
        finally:
            _queue.task_done()


def start_worker() -> None:
    """Запустить фонового воркера оркестратора. Зовётся из lifespan приложения."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker())


# ===== Проактивность после завершения задачи =====

async def after_task(task_id: int, kind: str, request: str, summary: str) -> None:
    """Зовётся из tasks.execute после успешного завершения.

    Отправляет результат владельцу и — в зависимости от уровня автономии — осмысляет
    его: medium предлагает следующий шаг, high может сам запустить фоновое исследование.
    Рассылка и любые отправки клиентам из этого пути невозможны.
    """
    wa_summary = summary[:1200] + ("\n\n📄 Полный отчёт сохранён в vault." if len(summary) > 1200 else "")
    await wa.notify_owner(f"✅ Задача #{task_id} готова.\n\n{wa_summary}")

    level = await settings_store.get("autonomy_level", "medium")
    if level == "low" or kind not in PROACTIVE_KINDS or request.startswith("[auto]"):
        return

    if level == "high":
        action_rule = (
            "Если очевиден полезный аналитический следующий шаг — запусти start_research прямо сейчас "
            "и скажи владельцу, что уже копаешь. Рассылку, отправку сообщений и approve_outreach "
            "запускать из этого режима ЗАПРЕЩЕНО — их только предлагай."
        )
    else:
        action_rule = "Сам ничего не запускай — только предложи следующий шаг."

    prompt = (
        f"Фоновая задача #{task_id} ({kind}) «{request[:200]}» завершилась, результат владельцу уже отправлен:\n"
        f"{summary[:1500]}\n\n"
        f"Подумай как ассистент: что владельцу логично сделать дальше? {action_rule}\n"
        "Ответь ОДНИМ коротким сообщением (1–3 предложения, без воды). "
        "Если предложить нечего — ответь ровно: НЕТ."
    )
    try:
        token = _in_after_task.set(True)
        try:
            result, _ = await run_safe("orchestrator", prompt, usage_limits=UsageLimits(request_limit=4))
        finally:
            _in_after_task.reset(token)
        comment = result.output.strip() if result else ""
        if comment and comment.upper() != "НЕТ":
            await memory.add_message("assistant", comment)
            await wa.notify_owner(f"💡 {comment}")
    except Exception as e:
        log.exception("after_task proactive pass failed (task %s)", task_id)
        await errlog.record("orchestrator", f"after_task #{task_id} ({kind})", e)
