# AGENTS.md — карта проекта для ИИ-агента

Читай ЭТОТ файл и `ARCHITECTURE.md` первыми. Не сканируй весь проект — используй карту ниже и точечный поиск.

## Где что лежит

| Что | Файл |
|---|---|
| Вход InstaPods (uvicorn) | `app.py` |
| FastAPI: lifespan, вебхук Green API, планировщик | `app/main.py` |
| Оркестратор + его инструменты | `app/orchestrator.py` |
| Реестр агентов + динамическая сборка (модель/промпт/soul/MCP) | `app/agents_registry.py` |
| Под-агенты | `app/subagents/` (product, lead, outreach, inbox, code, researcher) |
| Настройки и конфиги агентов из БД (кэш в памяти) | `app/settings_store.py` |
| Память: окно/выжимка/факты/архив | `app/memory.py` |
| Vault (Obsidian-заметки) + скиллы + WebDAV | `app/vault.py` |
| Рефлексия (самоанализ, уроки) | `app/reflection.py` |
| Фоновые задачи + after_task + дедуп + таймаут | `app/tasks.py` |
| Журнал ошибок (error_log, классификация причин) | `app/errlog.py` |
| Мониторинг цен | `app/monitoring.py` |
| LLM через OpenRouter | `app/llm.py` |
| Конфиг из .env | `app/config.py` |
| Пул Postgres + ретраи + применение схемы | `app/db.py` |
| Клиент Green API (WhatsApp) | `app/wa.py` |
| Схема БД (идемпотентная) | `app/schema.sql` |
| Админка API + auth | `app/admin.py` |
| Админка UI (весь интерфейс) | `app/static/admin.html` |
| Внешние инструменты | `app/tools/` (web_search, twogis, wildberries, scraper, sheets) |

## Правила работы (экономия контекста)

- Сверяйся с этой картой и `ARCHITECTURE.md`, не обходи весь проект.
- Ищи точечно (по символу/строке), читай нужные диапазоны строк, а не файлы целиком.
- Не перечитывай то, что уже в контексте.
- Не лезь в `data/`, `secrets/`, экспорты и сгенерированное (см. `.cursorignore`).
- Длинные находки — кратким резюме, не вставляй простыни кода.
- Перед широким исследованием спроси, нужно ли оно.

## Важные факты

- Секреты живут в `.env` на поде InstaPods и в `secrets/` — НЕ в git.
- Конфиги агентов (модель/промпт/soul/инструменты), MCP-серверы, лимиты — в Postgres, меняются в админке `/admin` без рестарта.
- Деплой: push в GitHub → redeploy на InstaPods. Схема БД применяется при старте сама.
- Формат для WhatsApp: без markdown-разметки (`#`, `**`, таблицы, `[](...)`) — это настраивается в промптах агентов.
