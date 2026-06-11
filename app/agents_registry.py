"""Реестр агентов: каждый агент собирается динамически из спеки + конфига в БД.

Из админки можно менять модель, системный промпт, душу (soul), выключать инструменты,
подключать MCP-серверы — без перезапуска и без правки кода.
"""

import asyncio
import logging
import re
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


MCP_CONNECT_TIMEOUT = 15  # сек на проверку одного сервера


def _tool_prefix(name: str) -> str:
    """Префикс инструментов: без него одинаковые имена тулзов у разных серверов конфликтуют."""
    return re.sub(r"\W+", "_", name).strip("_").lower() or "mcp"


def _make_toolset(srv: dict):
    prefix = _tool_prefix(srv["name"])
    if srv["transport"] == "stdio":
        from pydantic_ai.mcp import MCPServerStdio

        parts = shlex.split(srv["url"])
        return MCPServerStdio(parts[0], args=parts[1:], tool_prefix=prefix, timeout=MCP_CONNECT_TIMEOUT)
    if srv["transport"] == "sse":
        from pydantic_ai.mcp import MCPServerSSE

        return MCPServerSSE(srv["url"], headers=srv["headers"] or None, tool_prefix=prefix, timeout=MCP_CONNECT_TIMEOUT)
    from pydantic_ai.mcp import MCPServerStreamableHTTP

    return MCPServerStreamableHTTP(srv["url"], headers=srv["headers"] or None, tool_prefix=prefix, timeout=MCP_CONNECT_TIMEOUT)


async def probe_mcp_server(srv: dict) -> tuple[bool, str]:
    """Реально подключиться к серверу и получить список инструментов.

    Возвращает (ok, сообщение). Используется и при сборке агента, и кнопкой
    «проверить» в админке.
    """
    async def _connect_and_list():
        toolset = _make_toolset(srv)
        async with toolset:
            return await toolset.list_tools()

    try:
        tools = await asyncio.wait_for(_connect_and_list(), MCP_CONNECT_TIMEOUT)
        names = ", ".join(t.name for t in tools[:15])
        return True, f"OK, инструментов: {len(tools)} ({names})"
    except (TimeoutError, asyncio.TimeoutError):
        return False, f"не ответил за {MCP_CONNECT_TIMEOUT} сек"
    except Exception as e:
        while getattr(e, "exceptions", None):  # разворачиваем ExceptionGroup до настоящей причины
            e = e.exceptions[0]
        return False, f"{type(e).__name__}: {e}"


async def _mcp_toolsets(servers: list[dict]) -> list:
    """Собрать тулсеты, предварительно проверив каждый сервер живым подключением.

    Битые серверы пропускаются с записью в лог — один сломанный MCP больше
    не валит весь оркестратор.
    """
    results = await asyncio.gather(*(probe_mcp_server(s) for s in servers))
    toolsets = []
    for srv, (ok, msg) in zip(servers, results):
        if ok:
            toolsets.append(_make_toolset(srv))
            log.info("MCP %s: %s", srv["name"], msg)
        else:
            log.warning("MCP %s пропущен: %s", srv["name"], msg)
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
        # проверка серверов идёт только при пересборке агента (смена конфига), не на каждое сообщение
        agent = Agent(
            get_model(eff["model"]),
            output_type=spec.output_type,
            system_prompt=eff["prompt"],
            tools=tools,
            toolsets=await _mcp_toolsets(mcp_servers) or None,
            retries=spec.retries,
        )
        if len(_cache) > 64:  # конфиги меняются редко, не даём кэшу расти бесконечно
            _cache.clear()
        _cache[key] = agent
    return agent, eff["enabled"]
