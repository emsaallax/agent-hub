"""Фоновые задачи: создание, исполнение, уведомление владельца о результате.

Защита от зависаний и «фантомных» задач:
- жёсткий таймаут на задачу (TASK_TIMEOUT_S) — зависшая помечается error;
- результат пишется и в диалоговую память, чтобы оркестратор ЗНАЛ о завершении;
- find_active() — дедупликация: одинаковую задачу второй раз не запускаем.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from . import db, memory, wa

log = logging.getLogger(__name__)

TASK_TIMEOUT_S = 20 * 60  # максимум 20 минут на фоновую задачу

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


async def find_active(kind: str, request: str):
    """Уже идущая задача с тем же типом и запросом (защита от дублей)."""
    return await db.fetchrow(
        """
        SELECT id FROM tasks
        WHERE kind = $1 AND request = $2 AND status IN ('pending', 'running')
        ORDER BY id DESC LIMIT 1
        """,
        kind,
        request,
    )


async def execute(task_id: int, runner: Callable[[], Awaitable[str]]) -> None:
    """Выполнить задачу с таймаутом, сохранить результат, уведомить владельца, заархивировать."""
    await db.execute(
        "UPDATE tasks SET status = 'running', updated_at = now() WHERE id = $1", task_id
    )
    row = await db.fetchrow("SELECT kind, request FROM tasks WHERE id = $1", task_id)
    try:
        summary = await asyncio.wait_for(runner(), timeout=TASK_TIMEOUT_S)
        await db.execute(
            "UPDATE tasks SET status = 'done', result = $2, updated_at = now() WHERE id = $1",
            task_id,
            summary[:8000],
        )
        await memory.archive(
            "task", f"Задача #{task_id} ({row['kind']}): {row['request']}\nРезультат: {summary}"
        )
        # В диалоговую память — чтобы оркестратор видел завершение и не считал задачу идущей
        await memory.add_message(
            "assistant", f"✅ Задача #{task_id} ({row['kind']}) завершена. Результат:\n{summary[:1200]}"
        )
        await wa.notify_owner(f"✅ Задача #{task_id} готова.\n\n{summary}")
    except asyncio.TimeoutError:
        log.error("task %s timed out after %ss", task_id, TASK_TIMEOUT_S)
        await db.execute(
            "UPDATE tasks SET status = 'error', result = $2, updated_at = now() WHERE id = $1",
            task_id,
            f"Прервана по таймауту ({TASK_TIMEOUT_S // 60} мин)",
        )
        await memory.add_message(
            "assistant", f"❌ Задача #{task_id} ({row['kind']}) прервана по таймауту."
        )
        await wa.notify_owner(
            f"❌ Задача #{task_id} зависла и прервана по таймауту ({TASK_TIMEOUT_S // 60} мин). "
            f"Попробуй запустить её ещё раз или сузить запрос."
        )
    except Exception as e:
        log.exception("task %s failed", task_id)
        await db.execute(
            "UPDATE tasks SET status = 'error', result = $2, updated_at = now() WHERE id = $1",
            task_id,
            str(e)[:2000],
        )
        await memory.add_message(
            "assistant", f"❌ Задача #{task_id} ({row['kind']}) упала с ошибкой: {str(e)[:300]}"
        )
        await wa.notify_owner(f"❌ Задача #{task_id} упала: {e}")


def start(task_id: int, runner: Callable[[], Awaitable[str]]) -> None:
    spawn(execute(task_id, runner))


async def mark_orphaned() -> int:
    """После рестарта процесса все pending/running задачи мертвы — честно помечаем их error."""
    result = await db.execute(
        """
        UPDATE tasks SET status = 'error',
            result = 'Прервана перезапуском сервера (задеплоили новую версию или под перезагрузился)',
            updated_at = now()
        WHERE status IN ('pending', 'running')
        """
    )
    return int(result.split()[-1])
