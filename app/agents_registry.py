"""Реестр агентов: каждый агент собирается динамически из спеки + конфига в БД.

Из админки можно менять модель, системный промпт, выключать инструменты и весь агент —
без перезапуска и без правки кода.
"""

from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic_ai import Agent

from . import settings_store
from .llm import get_model


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
    return {
        "spec": spec,
        "model": model,
        "prompt": prompt,
        "disabled_tools": cfg["disabled_tools"],
        "enabled": cfg["enabled"],
    }


async def build(name: str) -> tuple[Agent, bool]:
    """Вернёт (агент, включён_ли). Агенты кэшируются по конфигурации."""
    eff = await effective(name)
    spec: AgentSpec = eff["spec"]
    disabled = set(eff["disabled_tools"])
    tools = [t for t in spec.tools if t.__name__ not in disabled]
    key = (
        name,
        eff["model"],
        hash(eff["prompt"]),
        tuple(sorted(t.__name__ for t in tools)),
    )
    agent = _cache.get(key)
    if agent is None:
        agent = Agent(
            get_model(eff["model"]),
            output_type=spec.output_type,
            system_prompt=eff["prompt"],
            tools=tools,
            retries=spec.retries,
        )
        if len(_cache) > 64:  # конфиги меняются редко, не даём кэшу расти бесконечно
            _cache.clear()
        _cache[key] = agent
    return agent, eff["enabled"]
