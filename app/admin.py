"""Админка: API + страница. Вход по паролю из ADMIN_PASSWORD (HTTP Basic, логин admin)."""

import os
import secrets as pysecrets
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from . import agents_registry, db, memory, monitoring, reflection, settings_store, tasks, vault
from .config import settings
from .subagents import outreach

_security = HTTPBasic()

STATIC_DIR = Path(__file__).parent / "static"


def require_admin(credentials: HTTPBasicCredentials = Depends(_security)) -> None:
    if not settings.admin_password:
        raise HTTPException(403, "Админка выключена: задай ADMIN_PASSWORD в .env и перезапусти под.")
    ok_user = pysecrets.compare_digest(credentials.username.encode(), b"admin")
    ok_pass = pysecrets.compare_digest(
        credentials.password.encode(), settings.admin_password.encode()
    )
    if not (ok_user and ok_pass):
        raise HTTPException(401, "Неверный логин или пароль", headers={"WWW-Authenticate": "Basic"})


router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

page_router = APIRouter()


@page_router.get("/admin", include_in_schema=False)
async def admin_page(_: None = Depends(require_admin)):
    return FileResponse(STATIC_DIR / "admin.html")


def _clean(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _rows(rows) -> list[dict]:
    return [{k: _clean(v) for k, v in dict(r).items()} for r in rows]


# ===== Обзор =====

@router.get("/overview")
async def overview():
    task_counts = await db.fetch("SELECT status, count(*) AS n FROM tasks GROUP BY status")
    lead_counts = await db.fetch("SELECT status, count(*) AS n FROM leads GROUP BY status")
    return {
        "tasks": {r["status"]: r["n"] for r in task_counts},
        "leads": {r["status"]: r["n"] for r in lead_counts},
        "companies": await db.fetchval("SELECT count(*) FROM companies"),
        "outreach_pending": await db.fetchval(
            "SELECT count(*) FROM outreach_messages WHERE status = 'pending_approval'"
        ),
        "outreach_sent_today": await db.fetchval(
            "SELECT count(*) FROM outreach_messages WHERE status = 'sent' AND sent_at >= date_trunc('day', now())"
        ),
        "watched": await db.fetchval("SELECT count(*) FROM watched_products WHERE active"),
        "facts": await db.fetchval("SELECT count(*) FROM memory_facts WHERE active"),
        "vault_notes": await db.fetchval("SELECT count(*) FROM vault_notes"),
        "mcp_servers": await db.fetchval("SELECT count(*) FROM mcp_servers WHERE enabled"),
        "dialog_messages": await db.fetchval("SELECT count(*) FROM dialog_messages"),
        "agents": len(agents_registry.REGISTRY),
        "scheduler": {
            "enabled": settings.scheduler_enabled,
            "outreach_minutes": settings.outreach_tick_minutes,
            "monitoring_hours": settings.monitoring_tick_hours,
        },
    }


# ===== Агенты =====

@router.get("/agents")
async def list_agents():
    out = []
    for name, spec in agents_registry.REGISTRY.items():
        cfg = await settings_store.agent_config(name)
        tier_model = await settings_store.tier_model(spec.tier)
        out.append({
            "name": name,
            "title": spec.title,
            "description": spec.description,
            "tier": spec.tier,
            "model_default": tier_model,
            "model_override": cfg["model_override"],
            "model_effective": cfg["model_override"] or tier_model,
            "prompt_default": spec.prompt,
            "prompt_override": cfg["prompt_override"],
            "soul": cfg["soul"],
            "use_mcp": spec.use_mcp,
            "enabled": cfg["enabled"],
            "tools": [
                {
                    "name": t.__name__,
                    "doc": (t.__doc__ or "").strip().split("\n")[0],
                    "disabled": t.__name__ in cfg["disabled_tools"],
                }
                for t in spec.tools
            ],
        })
    return out


class AgentUpdate(BaseModel):
    model_override: str | None = None
    prompt_override: str | None = None
    disabled_tools: list[str] | None = None
    enabled: bool | None = None
    soul: str | None = None


@router.put("/agents/{name}")
async def update_agent(name: str, body: AgentUpdate):
    if name not in agents_registry.REGISTRY:
        raise HTTPException(404, f"Агент {name} не найден")
    await settings_store.set_agent_config(
        name,
        model_override=body.model_override,
        prompt_override=body.prompt_override,
        disabled_tools=body.disabled_tools,
        enabled=body.enabled,
        soul=body.soul,
    )
    return {"ok": True}


# ===== Модели (глобальные ярусы) =====

@router.get("/models")
async def get_models():
    out = {}
    defaults = {
        "cheap": settings.model_cheap,
        "strong": settings.model_strong,
        "orchestrator": settings.orchestrator_model_name,
    }
    for tier, default in defaults.items():
        override = await settings_store.get(f"model_{tier}", "")
        out[tier] = {"default": default, "override": override, "effective": override or default}
    return out


class ModelsUpdate(BaseModel):
    cheap: str | None = None
    strong: str | None = None
    orchestrator: str | None = None


@router.put("/models")
async def set_models(body: ModelsUpdate):
    for tier in ("cheap", "strong", "orchestrator"):
        value = getattr(body, tier)
        if value is not None:
            await settings_store.set(f"model_{tier}", value.strip())
    return {"ok": True}


# ===== Задачи =====

@router.get("/tasks")
async def list_tasks(limit: int = 50, status: str = ""):
    if status:
        rows = await db.fetch(
            "SELECT * FROM tasks WHERE status = $1 ORDER BY id DESC LIMIT $2", status, limit
        )
    else:
        rows = await db.fetch("SELECT * FROM tasks ORDER BY id DESC LIMIT $1", limit)
    return _rows(rows)


# ===== Память =====

@router.get("/memory")
async def get_memory():
    facts = await db.fetch(
        "SELECT id, fact, category, created_at FROM memory_facts WHERE active ORDER BY id DESC LIMIT 100"
    )
    state = await db.fetchrow("SELECT summary, summarized_to, updated_at FROM dialog_state WHERE id = 1")
    dialog = await db.fetch(
        "SELECT id, role, content, created_at FROM dialog_messages ORDER BY id DESC LIMIT 30"
    )
    return {
        "facts": _rows(facts),
        "summary": {k: _clean(v) for k, v in dict(state).items()},
        "dialog": _rows(dialog),
    }


class FactIn(BaseModel):
    fact: str


@router.post("/memory/facts")
async def add_fact(body: FactIn):
    if not body.fact.strip():
        raise HTTPException(400, "Пустой факт")
    await db.execute(
        "INSERT INTO memory_facts (fact, category) VALUES ($1, 'manual')", body.fact.strip()
    )
    return {"ok": True}


@router.delete("/memory/facts/{fact_id}")
async def delete_fact(fact_id: int):
    await db.execute("UPDATE memory_facts SET active = FALSE WHERE id = $1", fact_id)
    return {"ok": True}


@router.get("/memory/archive")
async def search_archive(q: str = ""):
    if q.strip():
        results = await memory.search_archive(q, limit=10)
        return {"results": results}
    rows = await db.fetch(
        "SELECT content, created_at FROM memory_archive ORDER BY created_at DESC LIMIT 10"
    )
    return {"results": [f"[{r['created_at']:%d.%m.%Y}] {r['content'][:600]}" for r in rows]}


# ===== Лиды =====

@router.get("/leads")
async def list_leads(status: str = "", limit: int = 200):
    query = """
        SELECT l.id, l.status, l.updated_at, c.name, c.phone, c.city, c.niche, c.note, c.website
        FROM leads l JOIN companies c ON c.id = l.company_id
    """
    args: list = []
    if status:
        args.append(status)
        query += " WHERE l.status = $1"
    args.append(limit)
    query += f" ORDER BY l.updated_at DESC NULLS LAST, l.id DESC LIMIT ${len(args)}"
    return _rows(await db.fetch(query, *args))


# ===== Рассылка =====

@router.get("/outreach")
async def outreach_view():
    pending = await db.fetch(
        """
        SELECT om.id, om.text, om.created_at, c.name, c.phone
        FROM outreach_messages om
        JOIN leads l ON l.id = om.lead_id JOIN companies c ON c.id = l.company_id
        WHERE om.status = 'pending_approval' ORDER BY om.id
        """
    )
    counts = await db.fetch("SELECT status, count(*) AS n FROM outreach_messages GROUP BY status")
    recent = await db.fetch(
        """
        SELECT om.id, om.status, om.text, om.sent_at, om.error, c.name
        FROM outreach_messages om
        JOIN leads l ON l.id = om.lead_id JOIN companies c ON c.id = l.company_id
        WHERE om.status <> 'pending_approval' ORDER BY om.id DESC LIMIT 30
        """
    )
    return {
        "pending": _rows(pending),
        "counts": {r["status"]: r["n"] for r in counts},
        "sent_today": await db.fetchval(
            "SELECT count(*) FROM outreach_messages WHERE status = 'sent' AND sent_at >= date_trunc('day', now())"
        ),
        "recent": _rows(recent),
    }


@router.post("/outreach/{msg_id}/approve")
async def approve_one(msg_id: int):
    return {"message": await outreach.approve([msg_id])}


@router.post("/outreach/{msg_id}/reject")
async def reject_one(msg_id: int):
    return {"message": await outreach.reject([msg_id])}


@router.post("/outreach/approve-all")
async def approve_all():
    return {"message": await outreach.approve([])}


# ===== Мониторинг =====

@router.get("/watched")
async def list_watched():
    rows = await db.fetch(
        "SELECT id, title, url, source, last_price, available, last_checked FROM watched_products WHERE active ORDER BY id"
    )
    return _rows(rows)


@router.delete("/watched/{product_id}")
async def unwatch(product_id: int):
    return {"message": await monitoring.unwatch(product_id)}


@router.post("/jobs/{job}")
async def run_job(job: str):
    if job == "outreach":
        tasks.spawn(outreach.tick())
        return {"message": "Тик рассылки запущен."}
    if job == "monitoring":
        tasks.spawn(monitoring.tick())
        return {"message": "Проверка цен запущена."}
    if job == "reflection":
        task_id = await tasks.create("reflection", "запуск из админки")
        tasks.start(task_id, reflection.run_reflection)
        return {"message": f"Рефлексия запущена (задача #{task_id})."}
    raise HTTPException(404, "Неизвестная задача")


# ===== Vault (заметки) =====

@router.get("/vault")
async def vault_list():
    return _rows(await vault.list_notes())


@router.get("/vault/note")
async def vault_get(path: str):
    content = await vault.read_note(path)
    if content is None:
        raise HTTPException(404, "Заметка не найдена")
    return {"path": vault.normalize_path(path), "content": content}


class NoteIn(BaseModel):
    path: str
    content: str


@router.put("/vault/note")
async def vault_put(body: NoteIn):
    saved = await vault.write_note(body.path, body.content)
    return {"ok": True, "path": saved}


@router.delete("/vault/note")
async def vault_delete(path: str):
    if not await vault.delete_note(path):
        raise HTTPException(404, "Заметка не найдена")
    return {"ok": True}


@router.get("/vault/search")
async def vault_find(q: str):
    return {"results": await vault.search(q, limit=10)}


@router.get("/vault/export")
async def vault_export():
    data = await vault.export_zip()
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="vault.zip"'},
    )


# ===== Скиллы (знания из GitHub) =====

@router.get("/skills")
async def skills_list():
    return _rows(await vault.list_skills())


class SkillIn(BaseModel):
    repo_url: str


@router.post("/skills")
async def skills_install(body: SkillIn):
    return {"message": await vault.install_skill(body.repo_url)}


# ===== MCP-серверы =====

@router.get("/mcp")
async def mcp_list():
    return await settings_store.mcp_servers(only_enabled=False)


class McpIn(BaseModel):
    name: str
    transport: str = "http"   # http | sse | stdio
    url: str
    headers: dict[str, str] = {}


@router.post("/mcp")
async def mcp_add(body: McpIn):
    if body.transport not in ("http", "sse", "stdio"):
        raise HTTPException(400, "transport: http | sse | stdio")
    if not body.name.strip() or not body.url.strip():
        raise HTTPException(400, "Нужны имя и URL (или команда для stdio)")
    import json as _json

    await db.execute(
        """
        INSERT INTO mcp_servers (name, transport, url, headers)
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (name) DO UPDATE SET transport = $2, url = $3, headers = $4::jsonb
        """,
        body.name.strip(),
        body.transport,
        body.url.strip(),
        _json.dumps(body.headers),
    )
    settings_store.bump_mcp_version()
    return {"ok": True}


class McpToggle(BaseModel):
    enabled: bool


@router.put("/mcp/{server_id}")
async def mcp_toggle(server_id: int, body: McpToggle):
    await db.execute("UPDATE mcp_servers SET enabled = $2 WHERE id = $1", server_id, body.enabled)
    settings_store.bump_mcp_version()
    return {"ok": True}


@router.delete("/mcp/{server_id}")
async def mcp_delete(server_id: int):
    await db.execute("DELETE FROM mcp_servers WHERE id = $1", server_id)
    settings_store.bump_mcp_version()
    return {"ok": True}


# ===== Инструменты =====

@router.get("/tools")
async def tools_status():
    from . import wa

    return [
        {
            "name": "web_search",
            "title": "Веб-поиск",
            "ok": bool(settings.serper_api_key or settings.tavily_api_key),
            "detail": "Serper" if settings.serper_api_key else ("Tavily" if settings.tavily_api_key else "нет ключа (SERPER_API_KEY / TAVILY_API_KEY)"),
        },
        {
            "name": "twogis",
            "title": "2GIS (поиск организаций)",
            "ok": bool(settings.twogis_api_key),
            "detail": "ключ задан" if settings.twogis_api_key else "нет ключа (TWOGIS_API_KEY)",
        },
        {
            "name": "wildberries",
            "title": "Wildberries",
            "ok": True,
            "detail": "публичный API, ключ не нужен",
        },
        {
            "name": "scraper",
            "title": "Чтение страниц",
            "ok": True,
            "detail": "httpx + BeautifulSoup",
        },
        {
            "name": "sheets",
            "title": "Google Sheets",
            "ok": os.path.exists(settings.google_service_account_file),
            "detail": (
                f"ключ найден, доступ: {settings.google_share_email or 'email не задан'}"
                if os.path.exists(settings.google_service_account_file)
                else "нет файла secrets/google-service-account.json — экспорт идёт в CSV"
            ),
        },
        {
            "name": "greenapi_assistant",
            "title": "WhatsApp ассистента",
            "ok": wa.assistant.configured,
            "detail": f"инстанс {settings.green_api_id_instance}" if wa.assistant.configured else "не настроен",
        },
        {
            "name": "greenapi_outreach",
            "title": "WhatsApp рассылки",
            "ok": wa.outreach.configured,
            "detail": (
                "отдельный номер" if settings.green_api_outreach_id_instance
                else "общий с ассистентом" if wa.outreach.configured else "не настроен"
            ),
        },
        {
            "name": "openrouter",
            "title": "OpenRouter (LLM)",
            "ok": bool(settings.openrouter_api_key),
            "detail": "ключ задан" if settings.openrouter_api_key else "нет ключа OPENROUTER_API_KEY",
        },
    ]


# ===== Настройки =====

SETTING_KEYS = (
    "outreach_daily_limit",
    "outreach_batch_per_tick",
    "outreach_min_delay_s",
    "outreach_max_delay_s",
)


@router.get("/settings")
async def get_settings():
    out = {}
    for key in SETTING_KEYS:
        default = getattr(settings, key)
        out[key] = {
            "default": default,
            "effective": await settings_store.get_int(key, default),
        }
    out["autonomy_level"] = await settings_store.get("autonomy_level", "medium")
    out["info"] = {
        "owner_phone": settings.owner_phone,
        "scheduler_enabled": settings.scheduler_enabled,
        "outreach_tick_minutes": settings.outreach_tick_minutes,
        "monitoring_tick_hours": settings.monitoring_tick_hours,
        "data_dir": settings.data_dir,
    }
    return out


class SettingsUpdate(BaseModel):
    outreach_daily_limit: int | None = None
    outreach_batch_per_tick: int | None = None
    outreach_min_delay_s: int | None = None
    outreach_max_delay_s: int | None = None
    autonomy_level: str | None = None


@router.put("/settings")
async def update_settings(body: SettingsUpdate):
    for key in SETTING_KEYS:
        value = getattr(body, key)
        if value is not None:
            await settings_store.set(key, str(int(value)))
    if body.autonomy_level is not None:
        if body.autonomy_level not in ("low", "medium", "high"):
            raise HTTPException(400, "autonomy_level: low | medium | high")
        await settings_store.set("autonomy_level", body.autonomy_level)
    return {"ok": True}
