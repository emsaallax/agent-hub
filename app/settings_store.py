"""Настройки и конфиги агентов в Postgres. Редактируются из админки на лету.

Кэш в памяти процесса: воркер один, так что после set() кэш всегда свежий.
"""

import json
from typing import Any

from . import db
from .config import settings

_settings_cache: dict[str, str] = {}
_settings_loaded = False
_agent_cache: dict[str, dict] = {}

AGENT_DEFAULTS = {
    "model_override": "",
    "prompt_override": "",
    "disabled_tools": [],
    "enabled": True,
    "soul": "",
}

mcp_version = 0  # растёт при изменении списка MCP — сбрасывает кэш собранных агентов


async def _load_settings() -> None:
    global _settings_loaded
    rows = await db.fetch("SELECT key, value FROM app_settings")
    _settings_cache.clear()
    _settings_cache.update({r["key"]: r["value"] for r in rows})
    _settings_loaded = True


async def get(key: str, default: str = "") -> str:
    if not _settings_loaded:
        await _load_settings()
    return _settings_cache.get(key, default)


async def get_int(key: str, default: int) -> int:
    raw = await get(key, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


async def set(key: str, value: str) -> None:
    await db.execute(
        """
        INSERT INTO app_settings (key, value) VALUES ($1, $2)
        ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()
        """,
        key,
        str(value),
    )
    _settings_cache[key] = str(value)


async def tier_model(tier: str) -> str:
    """Модель для яруса: override из БД, иначе дефолт из .env."""
    defaults = {
        "cheap": settings.model_cheap,
        "strong": settings.model_strong,
        "orchestrator": settings.orchestrator_model_name,
    }
    override = await get(f"model_{tier}", "")
    return override or defaults[tier]


async def agent_config(name: str) -> dict[str, Any]:
    if name in _agent_cache:
        return _agent_cache[name]
    row = await db.fetchrow("SELECT * FROM agent_configs WHERE name = $1", name)
    if row is None:
        cfg = dict(AGENT_DEFAULTS)
    else:
        disabled = row["disabled_tools"]
        if isinstance(disabled, str):
            disabled = json.loads(disabled)
        cfg = {
            "model_override": row["model_override"],
            "prompt_override": row["prompt_override"],
            "disabled_tools": disabled or [],
            "enabled": row["enabled"],
            "soul": row["soul"],
        }
    _agent_cache[name] = cfg
    return cfg


async def set_agent_config(
    name: str,
    model_override: str | None = None,
    prompt_override: str | None = None,
    disabled_tools: list[str] | None = None,
    enabled: bool | None = None,
    soul: str | None = None,
) -> dict[str, Any]:
    current = await agent_config(name)
    cfg = {
        "model_override": model_override if model_override is not None else current["model_override"],
        "prompt_override": prompt_override if prompt_override is not None else current["prompt_override"],
        "disabled_tools": disabled_tools if disabled_tools is not None else current["disabled_tools"],
        "enabled": enabled if enabled is not None else current["enabled"],
        "soul": soul if soul is not None else current["soul"],
    }
    await db.execute(
        """
        INSERT INTO agent_configs (name, model_override, prompt_override, disabled_tools, enabled, soul)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6)
        ON CONFLICT (name) DO UPDATE SET
            model_override = $2, prompt_override = $3,
            disabled_tools = $4::jsonb, enabled = $5, soul = $6, updated_at = now()
        """,
        name,
        cfg["model_override"],
        cfg["prompt_override"],
        json.dumps(cfg["disabled_tools"]),
        cfg["enabled"],
        cfg["soul"],
    )
    _agent_cache[name] = cfg
    return cfg


# ===== MCP-серверы =====

async def mcp_servers(only_enabled: bool = True) -> list[dict]:
    query = "SELECT id, name, transport, url, headers, enabled FROM mcp_servers"
    if only_enabled:
        query += " WHERE enabled"
    rows = await db.fetch(query + " ORDER BY id")
    out = []
    for r in rows:
        headers = r["headers"]
        if isinstance(headers, str):
            headers = json.loads(headers)
        out.append(
            {
                "id": r["id"], "name": r["name"], "transport": r["transport"],
                "url": r["url"], "headers": headers or {}, "enabled": r["enabled"],
            }
        )
    return out


def bump_mcp_version() -> None:
    global mcp_version
    mcp_version += 1
