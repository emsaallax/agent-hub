import asyncio
import logging
from pathlib import Path

import asyncpg

from .config import settings

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

CONNECT_ATTEMPTS = 30
CONNECT_DELAY_S = 2


async def init_pool() -> None:
    """Подключение с ретраями: на поде Postgres может стартовать позже приложения."""
    global _pool
    last_err: Exception | None = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
            break
        except (OSError, asyncpg.PostgresError) as e:
            last_err = e
            log.warning("Postgres недоступен (попытка %s/%s): %s", attempt, CONNECT_ATTEMPTS, e)
            await asyncio.sleep(CONNECT_DELAY_S)
    else:
        raise RuntimeError(
            f"Не удалось подключиться к Postgres за {CONNECT_ATTEMPTS * CONNECT_DELAY_S} сек. "
            f"Проверь, что PostgreSQL установлен на поде (Services) и DATABASE_URL в .env верный. "
            f"Последняя ошибка: {last_err}"
        )
    await _apply_schema()


async def _apply_schema() -> None:
    """Схема идемпотентна (CREATE IF NOT EXISTS) — накатываем при каждом старте."""
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    async with pool().acquire() as conn:
        await conn.execute(schema)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool is not initialized")
    return _pool


async def fetch(query: str, *args):
    return await pool().fetch(query, *args)


async def fetchrow(query: str, *args):
    return await pool().fetchrow(query, *args)


async def fetchval(query: str, *args):
    return await pool().fetchval(query, *args)


async def execute(query: str, *args):
    return await pool().execute(query, *args)
