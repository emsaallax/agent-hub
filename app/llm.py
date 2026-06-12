from functools import lru_cache

from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings
from pydantic_ai.providers.openrouter import OpenRouterProvider

from .config import settings

_CACHE_SETTINGS = OpenRouterModelSettings(
    openrouter_cache_instructions="1h",   # системный промпт — статичен, кэш на час
    openrouter_cache_tool_definitions=True,  # схемы инструментов (22 шт) — только для Anthropic fallback
)


@lru_cache(maxsize=32)
def get_model(name: str) -> OpenRouterModel:
    api_key = settings.openrouter_api_key or "missing-openrouter-key"
    return OpenRouterModel(
        name,
        provider=OpenRouterProvider(api_key=api_key),
        settings=_CACHE_SETTINGS,
    )
