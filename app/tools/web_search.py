import httpx

from ..config import settings


async def search(query: str, num: int = 8) -> list[dict]:
    """Веб-поиск через Serper (приоритет) или Tavily. Возвращает [{title, url, snippet}]."""
    if settings.serper_api_key:
        return await _serper(query, num)
    if settings.tavily_api_key:
        return await _tavily(query, num)
    return []


async def _serper(query: str, num: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": settings.serper_api_key},
            json={"q": query, "gl": "ru", "hl": "ru", "num": num},
        )
        resp.raise_for_status()
        data = resp.json()
    return [
        {"title": r.get("title", ""), "url": r.get("link", ""), "snippet": r.get("snippet", "")}
        for r in data.get("organic", [])[:num]
    ]


async def _tavily(query: str, num: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={"api_key": settings.tavily_api_key, "query": query, "max_results": num},
        )
        resp.raise_for_status()
        data = resp.json()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")[:300]}
        for r in data.get("results", [])[:num]
    ]


def format_results(results: list[dict]) -> str:
    if not results:
        return "Поиск не настроен или ничего не нашёл (проверь SERPER_API_KEY/TAVILY_API_KEY)."
    return "\n\n".join(
        f"{i + 1}. {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(results)
    )
