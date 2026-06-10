import re

import httpx

SEARCH_URL = "https://search.wb.ru/exactmatch/ru/common/v4/search"
CARD_URLS = [
    "https://card.wb.ru/cards/v2/detail",
    "https://card.wb.ru/cards/detail",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.wildberries.ru/",
}
DEST_MOSCOW = "-1257786"


def _extract_price(p: dict) -> float | None:
    for key in ("salePriceU", "priceU"):
        value = p.get(key)
        if value:
            return round(value / 100, 2)
    sizes = p.get("sizes") or []
    if sizes:
        price = (sizes[0].get("price") or {})
        value = price.get("product") or price.get("basic")
        if value:
            return round(value / 100, 2)
    return None


def _parse_products(data: dict) -> list[dict]:
    products = (data.get("data") or {}).get("products") or data.get("products") or []
    out = []
    for p in products:
        pid = p.get("id")
        out.append(
            {
                "id": pid,
                "name": p.get("name", ""),
                "brand": p.get("brand", ""),
                "price": _extract_price(p),
                "rating": p.get("reviewRating") or p.get("rating"),
                "feedbacks": p.get("feedbacks"),
                "url": f"https://www.wildberries.ru/catalog/{pid}/detail.aspx",
            }
        )
    return out


async def search(query: str, limit: int = 15) -> list[dict]:
    """Поиск по публичному каталогу WB (тот же JSON, что видит браузер)."""
    params = {
        "appType": "1",
        "curr": "rub",
        "dest": DEST_MOSCOW,
        "lang": "ru",
        "page": "1",
        "query": query,
        "resultset": "catalog",
        "sort": "popular",
        "spp": "30",
        "suppressSpellcheck": "false",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(SEARCH_URL, params=params, headers=HEADERS)
        resp.raise_for_status()
        return _parse_products(resp.json())[:limit]


async def price_by_nm(nm_id: int) -> tuple[float | None, bool]:
    """(цена, в наличии) по артикулу WB."""
    params = {"appType": "1", "curr": "rub", "dest": DEST_MOSCOW, "nm": str(nm_id)}
    async with httpx.AsyncClient(timeout=20) as client:
        for url in CARD_URLS:
            try:
                resp = await client.get(url, params=params, headers=HEADERS)
                resp.raise_for_status()
                products = _parse_products(resp.json())
                if products:
                    p = products[0]
                    raw = (resp.json().get("data") or {}).get("products") or []
                    qty = raw[0].get("totalQuantity", 0) if raw else 0
                    available = bool(qty) or p["price"] is not None
                    return p["price"], available
            except httpx.HTTPError:
                continue
    return None, False


def nm_from_url(url: str) -> int | None:
    m = re.search(r"/catalog/(\d+)/", url)
    return int(m.group(1)) if m else None


def is_wb_url(url: str) -> bool:
    return "wildberries.ru" in url and nm_from_url(url) is not None


def format_results(results: list[dict]) -> str:
    if not results:
        return "WB ничего не нашёл (или эндпоинт сменил версию — это у них бывает)."
    lines = []
    for i, r in enumerate(results):
        price = f"{r['price']:.0f} ₽" if r["price"] else "цена недоступна"
        rating = f", рейтинг {r['rating']} ({r['feedbacks']} отзывов)" if r.get("rating") else ""
        lines.append(f"{i + 1}. {r['name']} ({r['brand']}) — {price}{rating}\n{r['url']}")
    return "\n".join(lines)
