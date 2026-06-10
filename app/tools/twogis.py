import httpx

from ..config import normalize_phone, settings

API_URL = "https://catalog.api.2gis.com/3.0/items"


async def search_companies(query: str, city: str, limit: int = 25) -> list[dict]:
    """Поиск организаций в 2GIS: [{name, address, phone, website}]. Пустой список, если ключа нет."""
    if not settings.twogis_api_key:
        return []
    params = {
        "q": f"{query} {city}".strip(),
        "fields": "items.contact_groups,items.address_name",
        "page_size": min(limit, 50),
        "key": settings.twogis_api_key,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(API_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    items = (data.get("result") or {}).get("items") or []
    out: list[dict] = []
    for item in items[:limit]:
        phone, website = "", ""
        for group in item.get("contact_groups") or []:
            for contact in group.get("contacts") or []:
                if contact.get("type") == "phone" and not phone:
                    phone = normalize_phone(contact.get("value", ""))
                elif contact.get("type") == "website" and not website:
                    website = contact.get("value", "")
        out.append(
            {
                "name": item.get("name", ""),
                "address": item.get("address_name", ""),
                "phone": phone,
                "website": website,
            }
        )
    return out


def format_results(results: list[dict]) -> str:
    if not results:
        return "2GIS не настроен (TWOGIS_API_KEY) или ничего не нашёл."
    return "\n".join(
        f"{i + 1}. {r['name']} | тел: {r['phone'] or '—'} | сайт: {r['website'] or '—'} | {r['address']}"
        for i, r in enumerate(results)
    )
