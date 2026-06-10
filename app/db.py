from pathlib import Path

import asyncpg

from .config import settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
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
