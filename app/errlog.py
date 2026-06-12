"""Журнал ошибок: единое место, куда падают ошибки задач, агентов и памяти.

Цель — видеть в админке, ЧТО и ПОЧЕМУ сломалось, без раскопок в текстовых логах.
Каждая запись классифицируется (таймаут, лимит шагов, битый JSON от провайдера...),
чтобы повторяющиеся проблемы были видны с одного взгляда. Журнал также читает
рефлексия — уроки из ошибок попадают в память.
"""

import logging
import traceback

from . import db

log = logging.getLogger(__name__)

KEEP_DAYS = 30  # записи старше — чистим, чтобы журнал не разрастался

# Порядок важен: первое совпадение по подстроке выигрывает
_CLASS_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("timeout",       ("таймаут", "timeout", "timed out")),
    ("request_limit", ("request_limit", "usagelimitexceeded", "usage limit", "слишком много шагов")),
    ("provider_json", ("expecting ',' delimiter", "expecting value", "jsondecode", "invalid json",
                       "validationerror", "validation error", "unexpectedmodelbehavior",
                       "exceeded maximum retries")),
    ("mcp",           ("mcp",)),
    ("http",          ("httpx", "status code", "status_code", "429", "502", "503")),
]


def classify(error: BaseException | str) -> str:
    text = (f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)).lower()
    for cls, needles in _CLASS_RULES:
        if any(n in text for n in needles):
            return cls
    return "other"


async def record(source: str, ref: str, error: BaseException | str, details: str = "") -> None:
    """Записать ошибку в журнал. Сам никогда не кидает — журнал не должен ломать работу."""
    try:
        if isinstance(error, BaseException):
            message = f"{type(error).__name__}: {error}"
            if not details:
                details = "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                )
        else:
            message = str(error)
        await db.execute(
            "INSERT INTO error_log (source, ref, error_class, message, details) VALUES ($1, $2, $3, $4, $5)",
            source,
            ref[:200],
            classify(error),
            message[:2000],
            details[:6000],
        )
        await db.execute(
            "DELETE FROM error_log WHERE created_at < now() - interval '30 days'"
        )
    except Exception:
        log.exception("error_log write failed (%s %s)", source, ref)
