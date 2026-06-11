"""Vault: markdown-заметки (Obsidian-совместимая память агента).

Источник правды — Postgres (vault_notes, полнотекстовый поиск). Каждая заметка
зеркалится файлом в data/vault/ — папку можно скачать целиком и открыть в Obsidian.
Агент пополняет vault сам: журнал задач, рефлексии, заметки по командам владельца.
"""

import asyncio
import io
import logging
import re
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

from . import db
from .config import settings

log = logging.getLogger(__name__)

MAX_NOTE_CHARS = 200_000
SKILL_FILE_EXT = {".md", ".txt", ".rst", ".mdx"}
SKILL_MAX_FILES = 120
SKILL_MAX_FILE_CHARS = 50_000


def _vault_dir() -> Path:
    d = Path(settings.data_dir) / "vault"
    d.mkdir(parents=True, exist_ok=True)
    return d


def normalize_path(path: str) -> str:
    """Безопасный относительный путь заметки: только внутри vault, всегда .md."""
    path = path.replace("\\", "/").strip().lstrip("/")
    path = re.sub(r"\.\.+", ".", path)  # никаких ..
    path = re.sub(r"[^\w\-./ ()«»а-яА-ЯёЁ]", "", path, flags=re.UNICODE)
    path = path.strip("/. ") or f"Заметка-{datetime.now():%Y%m%d-%H%M%S}"
    if not path.endswith(".md"):
        path += ".md"
    return path


def _mirror_to_file(path: str, content: str) -> None:
    try:
        file = _vault_dir() / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
    except OSError:
        log.exception("vault: не смог записать файл %s", path)


async def write_note(path: str, content: str) -> str:
    """Создать/перезаписать заметку. Возвращает нормализованный путь."""
    path = normalize_path(path)
    content = content[:MAX_NOTE_CHARS]
    await db.execute(
        """
        INSERT INTO vault_notes (path, content) VALUES ($1, $2)
        ON CONFLICT (path) DO UPDATE SET content = $2, updated_at = now()
        """,
        path,
        content,
    )
    _mirror_to_file(path, content)
    return path


async def append_note(path: str, chunk: str) -> str:
    """Дописать в конец заметки (создаст, если нет)."""
    path = normalize_path(path)
    current = await db.fetchval("SELECT content FROM vault_notes WHERE path = $1", path) or ""
    content = (current + ("\n" if current and not current.endswith("\n") else "") + chunk)[-MAX_NOTE_CHARS:]
    return await write_note(path, content)


async def read_note(path: str) -> str | None:
    return await db.fetchval("SELECT content FROM vault_notes WHERE path = $1", normalize_path(path))


async def delete_note(path: str) -> bool:
    result = await db.execute("DELETE FROM vault_notes WHERE path = $1", normalize_path(path))
    try:
        (_vault_dir() / normalize_path(path)).unlink(missing_ok=True)
    except OSError:
        pass
    return result.endswith("1")


async def list_notes(limit: int = 500) -> list[dict]:
    rows = await db.fetch(
        "SELECT path, length(content) AS size, updated_at FROM vault_notes ORDER BY updated_at DESC LIMIT $1",
        limit,
    )
    return [dict(r) for r in rows]


async def search(query: str, limit: int = 6) -> list[dict]:
    """Полнотекстовый поиск по заметкам: [{path, snippet}]."""
    rows = await db.fetch(
        """
        SELECT path,
               ts_headline('russian', left(content, 8000), plainto_tsquery('russian', $1),
                           'MaxWords=60, MinWords=20') AS snippet
        FROM vault_notes
        WHERE tsv @@ plainto_tsquery('russian', $1)
        ORDER BY ts_rank(tsv, plainto_tsquery('russian', $1)) DESC
        LIMIT $2
        """,
        query,
        limit,
    )
    return [dict(r) for r in rows]


async def journal(entry: str) -> None:
    """Запись в дневной журнал (Журнал/ГГГГ-ММ-ДД.md)."""
    today = datetime.now().strftime("%Y-%m-%d")
    stamp = datetime.now().strftime("%H:%M")
    await append_note(f"Журнал/{today}.md", f"\n## {stamp}\n{entry}\n")


async def export_zip() -> bytes:
    """Все заметки одним zip — открывается в Obsidian как vault."""
    rows = await db.fetch("SELECT path, content FROM vault_notes")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in rows:
            z.writestr(r["path"], r["content"])
        if not rows:
            z.writestr("README.md", "Vault пока пуст.")
    return buf.getvalue()


# ===== Скиллы: знания из GitHub-репозиториев =====

def _skill_name(repo_url: str) -> str:
    name = repo_url.rstrip("/").split("/")[-1]
    return re.sub(r"[^\w\-]", "", name.removesuffix(".git")) or "skill"


def _clone_sync(repo_url: str, dest: Path) -> None:
    if dest.exists():
        subprocess.run(
            ["git", "-C", str(dest), "pull", "--depth", "1"],
            check=True, capture_output=True, timeout=120,
        )
    else:
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(dest)],
            check=True, capture_output=True, timeout=180,
        )


async def install_skill(repo_url: str) -> str:
    """Клонировать репозиторий и проиндексировать его доки в vault (Скиллы/<имя>/...).

    Загружаются только текстовые файлы (.md/.txt/.rst) — код не исполняется.
    """
    repo_url = repo_url.strip()
    if not re.match(r"^https://(github\.com|gitlab\.com|bitbucket\.org)/[\w\-./]+$", repo_url.removesuffix(".git")):
        return "Дай ссылку вида https://github.com/owner/repo"
    name = _skill_name(repo_url)
    dest = Path(settings.data_dir) / "skills" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.to_thread(_clone_sync, repo_url, dest)
    except subprocess.CalledProcessError as e:
        return f"git не смог склонировать: {(e.stderr or b'').decode(errors='ignore')[:300]}"
    except FileNotFoundError:
        return "git не найден на сервере — скиллы недоступны."

    imported = 0
    for file in sorted(dest.rglob("*")):
        if imported >= SKILL_MAX_FILES:
            break
        if not file.is_file() or file.suffix.lower() not in SKILL_FILE_EXT or ".git" in file.parts:
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")[:SKILL_MAX_FILE_CHARS]
        except OSError:
            continue
        if not text.strip():
            continue
        rel = file.relative_to(dest).as_posix()
        await write_note(f"Скиллы/{name}/{rel}", text)
        imported += 1

    await db.execute(
        """
        INSERT INTO app_settings (key, value) VALUES ($1, $2)
        ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()
        """,
        f"skill_{name}",
        repo_url,
    )
    return f"Скилл «{name}» установлен: проиндексировано {imported} файлов. Агент найдёт их через vault_search."


def create_webdav_app(username: str, password: str):
    """WSGI-приложение wsgidav — монтируется в FastAPI для живой синхронизации с Obsidian."""
    from wsgidav.wsgidav_app import WsgiDAVApp  # noqa: import здесь — опциональная зависимость

    vault_dir = str(_vault_dir())
    config = {
        "provider_mapping": {"/": vault_dir},
        "simple_dc": {
            "user_mapping": {
                "*": {username: {"password": password}},
            }
        },
        "verbose": 0,
        "logging": {"enable_loggers": []},
        "lock_storage": True,
    }
    return WsgiDAVApp(config)


async def list_skills() -> list[dict]:
    rows = await db.fetch("SELECT key, value, updated_at FROM app_settings WHERE key LIKE 'skill_%' ORDER BY key")
    return [
        {"name": r["key"].removeprefix("skill_"), "repo": r["value"], "updated_at": r["updated_at"]}
        for r in rows
    ]
