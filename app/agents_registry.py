"""Реестр агентов: каждый агент собирается динамически из спеки + конфига в БД.

Из админки можно менять модель, системный промпт, душу (soul), выключать инструменты,
подключать MCP-серверы — без перезапуска и без правки кода.
"""

import logging
import shlex
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic_ai import Agent

from . import settings_store
from .llm import get_model

log = logging.getLogger(__name__)


@dataclass
class AgentSpec:
    name: str
    title: str
    tier: str                      # cheap | strong | orchestrator
    prompt: str
    description: str = ""
    output_type: Any = str
    tools: list[Callable] = field(default_factory=list)
    retries: int = 2
    use_mcp: bool = False          # подключать ли внешние MCP-серверы из админки


REGISTRY: dict[str, AgentSpec] = {}

_cache: dict[tuple, Agent] = {}


def register(spec: AgentSpec) -> AgentSpec:
    REGISTRY[spec.name] = spec
    return spec


async def effective(name: str) -> dict:
    """Эффективная конфигурация агента (для админки и сборки)."""
    spec = REGISTRY[name]
    cfg = await settings_store.agent_config(name)
    model = cfg["model_override"] or await settings_store.tier_model(spec.tier)
    prompt = cfg["prompt_override"] or spec.prompt
    if cfg["soul"]:
        prompt = f"{prompt}\n\n# Твоя личность (soul)\n{cfg['soul']}"
    return {
        "spec": spec,
        "model": model,
        "prompt": prompt,
        "soul": cfg["soul"],
        "disabled_tools": cfg["disabled_tools"],
        "enabled": cfg["enabled"],
    }


def _mcp_toolsets(servers: list[dict]) -> list:
    """Собрать pydantic-ai тулсеты из описаний MCP-серверов. Битые — пропускаем."""
    toolsets = []
    for srv in servers:
        try:
            if srv["transport"] == "stdio":
                from pydantic_ai.mcp import MCPServerStdio

                parts = shlex.split(srv["url"])
                toolsets.append(MCPServerStdio(parts[0], args=parts[1:]))
            elif srv["transport"] == "sse":
                from pydantic_ai.mcp import MCPServerSSE

                toolsets.append(MCPServerSSE(srv["url"], headers=srv["headers"] or None))
            else:  # http (streamable)
                from pydantic_ai.mcp import MCPServerStreamableHTTP

                toolsets.append(MCPServerStreamableHTTP(srv["url"], headers=srv["headers"] or None))
        except Exception:
            log.exception("MCP-сервер %s не подключился — пропускаю", srv.get("name"))
    return toolsets


async def build(name: str) -> tuple[Agent, bool]:
    """Вернёт (агент, включён_ли). Агенты кэшируются по конфигурации."""
    eff = await effective(name)
    spec: AgentSpec = eff["spec"]
    disabled = set(eff["disabled_tools"])
    tools = [t for t in spec.tools if t.__name__ not in disabled]

    mcp_servers: list[dict] = []
    if spec.use_mcp:
        mcp_servers = await settings_store.mcp_servers(only_enabled=True)

    key = (
        name,
        eff["model"],
        hash(eff["prompt"]),
        tuple(sorted(t.__name__ for t in tools)),
        settings_store.mcp_version if spec.use_mcp else -1,
    )
    agent = _cache.get(key)
    if agent is None:
        agent = Agent(
            get_model(eff["model"]),
            output_type=spec.output_type,
            system_prompt=eff["prompt"],
            tools=tools,
            toolsets=_mcp_toolsets(mcp_servers) or None,
            retries=spec.retries,
        )
        if len(_cache) > 64:  # конфиги меняются редко, не даём кэшу расти бесконечно
            _cache.clear()
        _cache[key] = agent
    return agent, eff["enabled"]
