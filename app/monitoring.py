"""Мониторинг цен и наличия по отслеживаемому списку. Тикается планировщиком."""

import asyncio
import logging
import random

from pydantic import BaseModel

from . import db, wa
from .agents_registry import AgentSpec, build, register
from .tools import scraper, wildberries

log = logging.getLogger(__name__)


class PriceCheck(BaseModel):
    price: float | None
    available: bool


register(
    AgentSpec(
        name="price_extractor",
        title="Мониторинг: парсер цен",
        tier="cheap",
        prompt=(
            "Из текста страницы товара извлеки текущую цену (число в рублях, если валюта не указана) "
            "и наличие товара. Если цена не видна — price=null. Если явно «нет в наличии» — available=false."
        ),
        description="Достаёт цену и наличие из текста страницы (для не-WB сайтов).",
        output_type=PriceCheck,
    )
)


async def add_watch(url: str, title: str) -> str:
    source = "wb" if wildberries.is_wb_url(url) else "web"
    existing = await db.fetchval("SELECT id FROM watched_products WHERE url = $1", url)
    if existing:
        await db.execute(
            "UPDATE watched_products SET active = TRUE WHERE id = $1", existing
        )
        return f"Этот товар уже отслеживается (#{existing}), включил обратно."
    product_id = await db.fetchval(
        "INSERT INTO watched_products (title, url, source) VALUES ($1, $2, $3) RETURNING id",
        title[:200] or url[:200],
        url,
        source,
    )
    row = await db.fetchrow("SELECT * FROM watched_products WHERE id = $1", product_id)
    await _check_one(row)  # первая проверка сразу, чтобы зафиксировать базовую цену
    fresh = await db.fetchrow("SELECT last_price FROM watched_products WHERE id = $1", product_id)
    price_str = f"{fresh['last_price']:.0f} ₽" if fresh["last_price"] is not None else "пока не определилась"
    return f"Добавил в мониторинг (#{product_id}): {title or url}. Текущая цена: {price_str}."


async def _check_one(row) -> str | None:
    """Проверить один товар. Вернёт строку-алерт, если что-то изменилось."""
    price: float | None = None
    available: bool | None = None
    try:
        if row["source"] == "wb":
            nm = wildberries.nm_from_url(row["url"])
            if nm:
                price, available = await wildberries.price_by_nm(nm)
        else:
            extractor, enabled = await build("price_extractor")
            if not enabled:
                return None
            text = await scraper.fetch_text(row["url"], max_chars=5000)
            result = (await extractor.run(text)).output
            price, available = result.price, result.available
    except Exception as e:
        log.warning("price check failed for %s: %s", row["url"], e)
        return None

    await db.execute(
        "INSERT INTO price_history (product_id, price, available) VALUES ($1, $2, $3)",
        row["id"],
        price,
        available,
    )

    old_price = float(row["last_price"]) if row["last_price"] is not None else None
    old_available = row["available"]
    await db.execute(
        "UPDATE watched_products SET last_price = $2, available = $3, last_checked = now() WHERE id = $1",
        row["id"],
        price,
        available,
    )

    alerts = []
    if old_price is not None and price is not None and old_price > 0:
        change = (price - old_price) / old_price
        if abs(change) >= 0.01:
            arrow = "📉" if change < 0 else "📈"
            alerts.append(f"{arrow} {row['title']}: {old_price:.0f} → {price:.0f} ₽ ({change:+.1%})\n{row['url']}")
    if old_available is not None and available is not None and old_available != available:
        word = "появился в наличии ✅" if available else "пропал из наличия ⛔"
        alerts.append(f"{row['title']}: {word}\n{row['url']}")
    return "\n".join(alerts) if alerts else None


async def tick() -> None:
    rows = await db.fetch("SELECT * FROM watched_products WHERE active ORDER BY id")
    alerts: list[str] = []
    for row in rows:
        alert = await _check_one(row)
        if alert:
            alerts.append(alert)
        await asyncio.sleep(random.uniform(2, 6))  # не долбим источники
    if alerts:
        await wa.notify_owner("Мониторинг цен — есть изменения:\n\n" + "\n\n".join(alerts))


async def list_watched() -> str:
    rows = await db.fetch(
        "SELECT id, title, last_price, available, url FROM watched_products WHERE active ORDER BY id"
    )
    if not rows:
        return "Список мониторинга пуст."
    lines = []
    for r in rows:
        price = f"{float(r['last_price']):.0f} ₽" if r["last_price"] is not None else "—"
        stock = "" if r["available"] is None else (" • в наличии" if r["available"] else " • нет в наличии")
        lines.append(f"#{r['id']} {r['title']} — {price}{stock}\n{r['url']}")
    return "Мониторинг:\n" + "\n".join(lines)


async def unwatch(product_id: int) -> str:
    result = await db.execute(
        "UPDATE watched_products SET active = FALSE WHERE id = $1", product_id
    )
    return f"Убрал #{product_id} из мониторинга." if result.endswith("1") else f"#{product_id} не найден."
