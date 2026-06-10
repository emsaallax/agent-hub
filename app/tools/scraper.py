import re

import httpx
from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0 Safari/537.36"
)


async def fetch_text(url: str, max_chars: int = 6000) -> str:
    """Скачать страницу и вернуть чистый текст (для LLM)."""
    async with httpx.AsyncClient(
        timeout=25, follow_redirects=True, headers={"User-Agent": UA}
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s{2,}", " ", text)
    return f"Заголовок: {title}\n{text[:max_chars]}"
