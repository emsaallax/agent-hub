from functools import lru_cache

from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from .config import settings


@lru_cache(maxsize=32)
def get_model(name: str) -> OpenRouterModel:
    # Заглушка вместо пустого ключа: контейнер стартует, а ошибка авторизации
    # всплывёт понятным текстом при первом запросе, а не крэшем при импорте.
    api_key = settings.openrouter_api_key or "missing-openrouter-key"
    return OpenRouterModel(name, provider=OpenRouterProvider(api_key=api_key))
