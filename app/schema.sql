-- Схема agent-hub. Идемпотентна, применяется при старте приложения.

-- ===== Диалог с владельцем и память =====

CREATE TABLE IF NOT EXISTS dialog_messages (
    id BIGSERIAL PRIMARY KEY,
    role TEXT NOT NULL,                          -- user | assistant
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Единственная строка: rolling summary старой части диалога
CREATE TABLE IF NOT EXISTS dialog_state (
    id INT PRIMARY KEY DEFAULT 1,
    summary TEXT NOT NULL DEFAULT '',
    summarized_to BIGINT NOT NULL DEFAULT 0,     -- id последнего сообщения, вошедшего в выжимку
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO dialog_state (id) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS memory_facts (
    id BIGSERIAL PRIMARY KEY,
    fact TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Архив завершённых задач/диалогов: полнотекстовый поиск (память v2)
CREATE TABLE IF NOT EXISTS memory_archive (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'task',
    content TEXT NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('russian', content)) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS memory_archive_tsv_idx ON memory_archive USING GIN (tsv);

-- ===== Журнал ошибок (задачи, память, оркестратор) =====

CREATE TABLE IF NOT EXISTS error_log (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,                        -- task | agent | memory | orchestrator
    ref TEXT NOT NULL DEFAULT '',                -- например 'task #42 (research)'
    error_class TEXT NOT NULL DEFAULT 'other',   -- timeout | request_limit | provider_json | mcp | http | other
    message TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',            -- traceback
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS error_log_created_idx ON error_log (created_at DESC);

-- ===== Настройки и конфиги агентов (редактируются из админки) =====

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_configs (
    name TEXT PRIMARY KEY,
    model_override TEXT NOT NULL DEFAULT '',
    prompt_override TEXT NOT NULL DEFAULT '',
    disabled_tools JSONB NOT NULL DEFAULT '[]'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE agent_configs ADD COLUMN IF NOT EXISTS soul TEXT NOT NULL DEFAULT '';

-- ===== Vault: markdown-заметки (Obsidian-совместимая память) =====

CREATE TABLE IF NOT EXISTS vault_notes (
    path TEXT PRIMARY KEY,                       -- например 'Журнал/2026-06-11.md'
    content TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('russian', left(content, 200000))) STORED
);
CREATE INDEX IF NOT EXISTS vault_notes_tsv_idx ON vault_notes USING GIN (tsv);

-- ===== MCP-серверы (внешние наборы инструментов) =====

CREATE TABLE IF NOT EXISTS mcp_servers (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    transport TEXT NOT NULL DEFAULT 'http',      -- http | sse | stdio
    url TEXT NOT NULL DEFAULT '',                -- url для http/sse, команда для stdio
    headers JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===== Задачи оркестратора =====

CREATE TABLE IF NOT EXISTS tasks (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL,                          -- product_search | lead_search | outreach_prepare | code | ...
    status TEXT NOT NULL DEFAULT 'pending',      -- pending | running | done | error
    request TEXT NOT NULL,
    result TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '';

-- ===== Клиенты =====

CREATE TABLE IF NOT EXISTS companies (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT,                                  -- только цифры, 7XXXXXXXXXX
    website TEXT,
    city TEXT,
    niche TEXT,
    note TEXT,
    source TEXT,                                 -- 2gis | web | manual | agent
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS companies_phone_uq
    ON companies (phone) WHERE phone IS NOT NULL AND phone <> '';

CREATE TABLE IF NOT EXISTS leads (
    id BIGSERIAL PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'new',          -- new | queued | contacted | interested | question | declined | spam
    notes TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id)
);

CREATE TABLE IF NOT EXISTS outreach_messages (
    id BIGSERIAL PRIMARY KEY,
    lead_id BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_approval', -- pending_approval | approved | sent | rejected | failed
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ,
    error TEXT
);

-- Переписка с лидами (вход/исход через рассыльный номер)
CREATE TABLE IF NOT EXISTS lead_messages (
    id BIGSERIAL PRIMARY KEY,
    lead_id BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    direction TEXT NOT NULL,                     -- in | out
    text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===== Товары и мониторинг цен =====

CREATE TABLE IF NOT EXISTS watched_products (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT 'web',          -- wb | web
    last_price NUMERIC,
    available BOOLEAN,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    last_checked TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS price_history (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES watched_products(id) ON DELETE CASCADE,
    price NUMERIC,
    available BOOLEAN,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===== Лог токенов =====

CREATE TABLE IF NOT EXISTS token_log (
    id BIGSERIAL PRIMARY KEY,
    agent_name TEXT NOT NULL,
    task_kind TEXT,
    input_tokens INT,
    output_tokens INT,
    total_tokens INT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_token_log_created ON token_log (created_at DESC);
