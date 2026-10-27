-- Phase 5B schema (also applied automatically at startup; CREATE ... IF NOT EXISTS, safe to re-run).
BEGIN;

-- Phase 5B: external information from the web. Isolated from user memories and Violet's life;
-- untrusted by default; one row per URL (sources are never merged into one claim).
CREATE TABLE IF NOT EXISTS web_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL,
    title TEXT NOT NULL,
    query TEXT,
    summary TEXT,
    source_kind TEXT NOT NULL,
    provider TEXT,
    origin TEXT NOT NULL,
    interest_key TEXT,
    published_at TEXT,
    published_verified INTEGER NOT NULL DEFAULT 0,
    trust TEXT NOT NULL DEFAULT 'untrusted',
    injection_removed INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    used_in_chat_at TEXT,
    used_count INTEGER NOT NULL DEFAULT 0,
    shared_with_user_at TEXT,
    share_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_findings_domain ON web_findings(domain, status);
CREATE INDEX IF NOT EXISTS idx_web_findings_fetched ON web_findings(fetched_at);

-- Phase 5B: per-domain switch (disabled domains are never searched into findings or fetched).
CREATE TABLE IF NOT EXISTS web_sources (
    domain TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    trust TEXT,
    reason TEXT,
    updated_at TEXT NOT NULL
);

-- Phase 5B: every search / fetch attempt (budgets, failures, dashboard). URLs are stored redacted.
CREATE TABLE IF NOT EXISTS web_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    origin TEXT NOT NULL,
    target TEXT,
    provider TEXT,
    status TEXT NOT NULL,
    error_code TEXT,
    result_count INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_requests_created ON web_requests(kind, created_at);
COMMIT;
