import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request

from . import admin, db, monitoring, orchestrator, reflection, tasks
from .config import settings
from .subagents import code, inbox, lead, outreach, product, researcher  # noqa: F401 — регистрация агентов в реестре

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

def _setup_file_logging() -> None:
    log_dir = Path(settings.data_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_dir / "agent-hub.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)

_setup_file_logging()
log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    orphaned = await tasks.mark_orphaned()
    if orphaned:
        log.warning("Помечено как error %s задач, прерванных перезапуском", orphaned)
    if not settings.owner_phone:
        log.warning("OWNER_PHONE не задан — ассистент будет игнорировать все сообщения!")
    if not settings.openrouter_api_key:
        log.warning("OPENROUTER_API_KEY не задан — LLM-запросы будут падать!")
    if not settings.green_api_id_instance:
        log.warning("GREEN_API_ID_INSTANCE не задан — WhatsApp не подключён!")
    if not settings.admin_password:
        log.warning("ADMIN_PASSWORD не задан — веб-админка /admin выключена.")
    if settings.scheduler_enabled:
        scheduler.add_job(
            outreach.tick, "interval",
            minutes=settings.outreach_tick_minutes,
            id="outreach_tick", coalesce=True, max_instances=1,
        )
        scheduler.add_job(
            monitoring.tick, "interval",
            hours=settings.monitoring_tick_hours,
            id="monitoring_tick", coalesce=True, max_instances=1,
        )
        scheduler.add_job(
            reflection.daily_job, "interval",
            hours=24,
            id="reflection_daily", coalesce=True, max_instances=1,
        )
        scheduler.start()
        log.info(
            "Планировщик: рассылка каждые %s мин, мониторинг каждые %s ч",
            settings.outreach_tick_minutes, settings.monitoring_tick_hours,
        )
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    await db.close_pool()


app = FastAPI(title="agent-hub", lifespan=lifespan)
app.include_router(admin.router)
app.include_router(admin.page_router)

# WebDAV-эндпоинт для живой синхронизации с Obsidian (плагин Remotely Save).
# URL: /webdav/   Логин: admin   Пароль: ADMIN_PASSWORD
if settings.admin_password:
    try:
        from starlette.middleware.wsgi import WSGIMiddleware
        from . import vault as _vault_mod

        app.mount("/webdav", WSGIMiddleware(_vault_mod.create_webdav_app("admin", settings.admin_password)))
        log.info("WebDAV смонтирован на /webdav/ — подключай Obsidian (Remotely Save)")
    except ImportError:
        log.warning("wsgidav не установлен — WebDAV недоступен. Сделай pip install wsgidav.")


@app.get("/")
async def root():
    return {"service": "agent-hub", "ok": True, "admin": "/admin"}


@app.get("/health")
async def health():
    return {"ok": True}


def _webhook_authorized(request: Request) -> bool:
    token = settings.green_api_webhook_token
    if not token:
        return True
    auth = request.headers.get("authorization", "")
    return auth in (token, f"Bearer {token}")


def _extract_text(message_data: dict) -> str:
    text = (message_data.get("textMessageData") or {}).get("textMessage")
    if not text:
        text = (message_data.get("extendedTextMessageData") or {}).get("text")
    return (text or "").strip()


@app.post("/webhooks/greenapi")
async def greenapi_webhook(request: Request):
    """Единый вебхук Green API для обоих инстансов.

    Маршрутизация по отправителю: владелец -> оркестратор, остальные -> разбор входящих.
    """
    if not _webhook_authorized(request):
        log.warning("greenapi webhook: неверный webhookUrlToken")
        return {"ok": True}
    body = await request.json()
    if body.get("typeWebhook") != "incomingMessageReceived":
        return {"ok": True}

    sender = body.get("senderData") or {}
    chat = sender.get("chatId") or ""
    text = _extract_text(body.get("messageData") or {})
    log.info("webhook: chat=%s owner_chat_id=%s text_len=%s", chat, settings.owner_chat_id, len(text))
    if not text or not chat.endswith("@c.us"):  # только личные текстовые сообщения
        return {"ok": True}

    if chat == settings.owner_chat_id:
        tasks.spawn(orchestrator.handle_owner_message(text))
    else:
        tasks.spawn(inbox.handle_incoming(chat, text))
    return {"ok": True}


@app.post("/jobs/outreach/tick")
async def outreach_tick():
    """Ручной запуск тика рассылки (планировщик и так делает это сам)."""
    tasks.spawn(outreach.tick())
    return {"ok": True}


@app.post("/jobs/monitoring/tick")
async def monitoring_tick():
    """Ручной запуск проверки цен."""
    tasks.spawn(monitoring.tick())
    return {"ok": True}
