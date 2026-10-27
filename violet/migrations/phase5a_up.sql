-- Phase 5A schema (also applied automatically at startup; CREATE ... IF NOT EXISTS, safe to re-run).
BEGIN;

-- Phase 5A: background LLM helper calls (life proposer, semantic guard). No prompt text stored.
CREATE TABLE IF NOT EXISTS aux_llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 1,
    latency_ms INTEGER,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aux_llm_calls_purpose ON aux_llm_calls(purpose, created_at);

-- Phase 5A: life engine switches set from the dashboard (e.g. paused).
CREATE TABLE IF NOT EXISTS life_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
COMMIT;
