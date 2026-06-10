"""Фоновые задачи: создание, исполнение, уведомление владельца о результате."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from . import db, memory, wa

log = logging.getLogger(__name__)

_running: set[asyncio.Task] = set()


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _running.add(task)
    task.add_done_callback(_running.discard)


async def create(kind: str, request: str) -> int:
    return await db.fetchval(
        "INSERT INTO tasks (kind, request, status) VALUES ($1, $2, 'pending') RETURNING id",
        kind,
        request,
    )


async def execute(task_id: int, runner: Callable[[], Awaitable[str]]) -> None:
    """Выполнить задачу, сохранить результат, уведомить владельца, заархивировать."""
    await db.execute(
        "UPDATE tasks SET status = 'running', updated_at = now() WHERE id = $1", task_id
    )
    row = await db.fetchrow("SELECT kind, request FROM tasks WHERE id = $1", task_id)
    try:
        summary = await runner()
        await db.execute(
            "UPDATE tasks SET status = 'done', result = $2, updated_at = now() WHERE id = $1",
            task_id,
            summary[:8000],
        )
        await memory.archive(
            "task", f"Задача #{task_id} ({row['kind']}): {row['request']}\nРезультат: {summary}"
        )
        await wa.notify_owner(f"✅ Задача #{task_id} готова.\n\n{summary}")
    except Exception as e:
        log.exception("task %s failed", task_id)
        await db.execute(
            "UPDATE tasks SET status = 'error', result = $2, updated_at = now() WHERE id = $1",
            task_id,
            str(e)[:2000],
        )
        await wa.notify_owner(f"❌ Задача #{task_id} упала: {e}")


def start(task_id: int, runner: Callable[[], Awaitable[str]]) -> None:
    spawn(execute(task_id, runner))
