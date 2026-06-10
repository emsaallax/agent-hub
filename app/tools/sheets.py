"""Экспорт таблиц: Google Sheets (если настроен сервисный аккаунт) + CSV-фолбэк всегда."""

import asyncio
import csv
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from ..config import settings

log = logging.getLogger(__name__)


def _slug(title: str) -> str:
    s = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:60] or "table"


def _save_csv(title: str, headers: list[str], rows: list[list]) -> str:
    exports = Path(settings.data_dir) / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    path = exports / f"{_slug(title)}-{datetime.now():%Y%m%d-%H%M%S}.csv"
    # utf-8-sig — чтобы Excel открывал кириллицу без танцев
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(headers)
        writer.writerows(rows)
    return str(path)


def _create_sheet_sync(title: str, headers: list[str], rows: list[list]) -> str:
    import gspread

    gc = gspread.service_account(filename=settings.google_service_account_file)
    sh = gc.create(title)
    ws = sh.sheet1
    ws.append_row(headers)
    for i in range(0, len(rows), 200):
        ws.append_rows(rows[i : i + 200])
    if settings.google_share_email:
        sh.share(settings.google_share_email, perm_type="user", role="writer", notify=False)
    if settings.sheets_public_link:
        sh.share(None, perm_type="anyone", role="reader")
    return sh.url


async def export_table(
    title: str, headers: list[str], rows: list[list]
) -> tuple[str | None, str]:
    """Вернёт (url Google-таблицы | None, путь к CSV)."""
    str_rows = [["" if v is None else str(v) for v in row] for row in rows]
    csv_path = _save_csv(title, headers, str_rows)

    if not os.path.exists(settings.google_service_account_file):
        return None, csv_path
    try:
        url = await asyncio.to_thread(_create_sheet_sync, title, headers, str_rows)
        return url, csv_path
    except Exception:
        log.exception("Google Sheets export failed, CSV сохранён: %s", csv_path)
        return None, csv_path


def table_link_line(sheet_url: str | None, csv_path: str) -> str:
    if sheet_url:
        return f"📊 Таблица: {sheet_url}"
    return f"📁 Google не настроен, сохранил CSV: {csv_path} (см. папку data/exports)"
