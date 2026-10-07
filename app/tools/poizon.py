"""Poizon (Dewu / 得物): поиск товаров.

У Poizon нет открытого API: приложение подписывает каждый запрос секретным
ключом, а официальный open.dewu.com дают только продавцам. Поэтому два пути:

1. Apify (если задан POIZON_APIFY_TOKEN) — готовый парсер Poizon в облаке:
   отдаёт название, бренд, артикул, цены по размерам, ссылку. Платный, но
   стабильный: когда Poizon меняет защиту, чинит автор парсера, а не мы.
2. Без ключа — веб-поиск по сайту poizon.com через Serper/Tavily
   (только названия и ссылки; цену агент добирает через fetch_page).
"""

import re

import httpx

from ..config import settings
from . import web_search

APIFY_URL = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _to_price(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"\d[\d\s.,]*", value)
        if m:
            try:
                return float(m.group(0).replace(" ", "").replace(",", ""))
            except ValueError:
                return None
    if isinstance(value, dict):
        return _to_price(_first(value, "amount", "value", "price"))
    return None


def _sizes(item: dict) -> list[tuple[str, float]]:
    """Цены по размерам, если парсер их отдал: [(размер, цена)]."""
    raw = _first(item, "sizes", "skus", "variants", "sizePrices") or []
    out = []
    if isinstance(raw, list):
        for s in raw:
            if not isinstance(s, dict):
                continue
            size = _first(s, "size", "name", "label", "propertyValue")
            price = _to_price(_first(s, "price", "lowestPrice", "minPrice", "amount"))
            if size and price:
                out.append((str(size), price))
    return out


def _normalize(item: dict) -> dict:
    """Приводим ответ парсера к нашему виду. Ключи у разных парсеров разные,
    поэтому берём первое найденное из нескольких вариантов."""
    sizes = _sizes(item)
    price = _to_price(_first(item, "price", "lowestPrice", "minPrice", "salePrice", "priceUsd"))
    if price is None and sizes:
        price = min(p for _, p in sizes)
    return {
        "id": _first(item, "spuId", "id", "productId"),
        "name": _first(item, "title", "name", "productName") or "",
        "brand": _first(item, "brand", "brandName") or "",
        "article": _first(item, "styleCode", "articleNumber", "articleNo", "sku") or "",
        "price": price,
        "currency": _first(item, "currency") or "",
        "sold": _first(item, "soldCount", "unitsSold", "sales", "soldNum"),
        "sizes": sizes,
        "url": _first(item, "url", "productUrl", "link") or "",
    }


async def _apify_search(query: str, limit: int) -> list[dict]:
    payload = {
        # разные акторы называют поле запроса по-разному — шлём основные варианты,
        # лишние поля Apify игнорирует
        "searchQueries": [query],
        "keywords": [query],
        "query": query,
        "maxItems": limit,
        "maxResults": limit,
    }
    url = APIFY_URL.format(actor=settings.poizon_apify_actor.replace("/", "~"))
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(url, params={"token": settings.poizon_apify_token}, json=payload)
        resp.raise_for_status()
        data = resp.json()
    items = data if isinstance(data, list) else data.get("items", [])
    return [_normalize(i) for i in items if isinstance(i, dict)][:limit]


async def _web_search(query: str, limit: int) -> list[dict]:
    results = await web_search.search(f"site:poizon.com {query}", num=limit)
    return [
        {
            "id": None,
            "name": r["title"],
            "brand": "",
            "article": "",
            "price": _to_price(r["snippet"]) if "$" in r["snippet"] or "¥" in r["snippet"] else None,
            "currency": "",
            "sold": None,
            "sizes": [],
            "url": r["url"],
            "snippet": r["snippet"],
        }
        for r in results
        if "poizon" in r["url"] or "dewu" in r["url"]
    ]


def source() -> str:
    return "apify" if settings.poizon_apify_token else "web"


async def search(query: str, limit: int = 10) -> list[dict]:
    """Поиск товаров на Poizon. Название, бренд, артикул, цена, размеры, ссылка."""
    if settings.poizon_apify_token:
        return await _apify_search(query, limit)
    return await _web_search(query, limit)


def format_results(results: list[dict]) -> str:
    if not results:
        if not (settings.poizon_apify_token or settings.serper_api_key or settings.tavily_api_key):
            return "Poizon не настроен: нужен POIZON_APIFY_TOKEN (или хотя бы SERPER_API_KEY для поиска по сайту)."
        return "На Poizon ничего не нашлось. Попробуй по-английски или по артикулу (например, DD1391-100)."
    lines = []
    for i, r in enumerate(results):
        head = r["name"]
        if r["brand"] and r["brand"].lower() not in head.lower():
            head = f"{head} ({r['brand']})"
        parts = [f"{i + 1}. {head}"]
        if r["article"]:
            parts.append(f"артикул {r['article']}")
        if r["price"]:
            parts.append(f"от {r['price']:.0f} {r['currency']}".strip())
        else:
            parts.append("цену смотреть по ссылке")
        if r["sold"]:
            parts.append(f"продано {r['sold']}")
        line = ", ".join(parts)
        if r["sizes"]:
            sizes = "; ".join(f"{s}: {p:.0f}" for s, p in r["sizes"][:12])
            line += f"\nРазмеры: {sizes}"
        if r.get("snippet"):
            line += f"\n{r['snippet']}"
        lines.append(f"{line}\n{r['url']}")
    return "\n".join(lines)
