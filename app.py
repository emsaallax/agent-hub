"""Точка входа для InstaPods: systemd-сервис пода запускает `python app.py`.

Приложение обязано слушать 0.0.0.0:8000 — публичный URL пода проксируется на этот порт.
Пакет `app/` имеет приоритет над этим файлом при импорте, поэтому "app.main:app"
резолвится в app/main.py.
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
