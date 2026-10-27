-- Phase 4 schema (also applied automatically at startup by VioletDatabase.initialize_schema;
-- every statement is CREATE ... IF NOT EXISTS, so running it twice is safe).
BEGIN;

-- Phase 4: loaded versions of the hand-edited persona core (audit only).
CREATE TABLE IF NOT EXISTS persona_core_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    loaded_at TEXT NOT NULL
);

-- Phase 4: slowly changing preferences / temporary interests / opinions of Violet.
CREATE TABLE IF NOT EXISTS persona_dynamic (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aspect_key TEXT NOT NULL,
    category TEXT NOT NULL,
    value TEXT NOT NULL,
    previous_value TEXT,
    reason TEXT NOT NULL,
    related_event_ids TEXT,
    evidence_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    requires_review INTEGER NOT NULL DEFAULT 0,
    valid_until TEXT,
    decided_by TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_persona_dynamic_aspect ON persona_dynamic(aspect_key, status);

-- Phase 4: Violet's virtual life. Thread / plan / event statuses are separate vocabularies.
CREATE TABLE IF NOT EXISTS life_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_key TEXT NOT NULL UNIQUE,
    interest_key TEXT NOT NULL,
    title TEXT NOT NULL,
    item TEXT,
    status TEXT NOT NULL,
    progress_steps INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    last_progress_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_life_threads_status ON life_threads(status);

CREATE TABLE IF NOT EXISTS life_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    planned_for TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    resolution_event_id INTEGER,
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_life_plans_status ON life_plans(status, planned_for);

CREATE TABLE IF NOT EXISTS life_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL UNIQUE,
    thread_id INTEGER,
    plan_id INTEGER,
    event_type TEXT NOT NULL,
    content TEXT NOT NULL,
    planned_at TEXT,
    occurred_at TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL DEFAULT 60,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    source_detail TEXT,
    related_event_id INTEGER,
    is_virtual INTEGER NOT NULL DEFAULT 1,
    shared_with_user_at TEXT,
    share_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_life_events_occurred ON life_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_life_events_thread ON life_events(thread_id, occurred_at);

CREATE TABLE IF NOT EXISTS life_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_life_revisions_entity ON life_revisions(entity, entity_id);

-- One row per local day: the life engine never runs twice for the same day (restart-safe).
CREATE TABLE IF NOT EXISTS life_engine_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key TEXT NOT NULL UNIQUE,
    outcome TEXT NOT NULL,
    detail TEXT,
    ran_at TEXT NOT NULL
);

-- Consistency problems that need a human (never silently "fixed" by inventing facts).
CREATE TABLE IF NOT EXISTS consistency_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL,
    entity_ref TEXT,
    check_name TEXT NOT NULL,
    detail TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_consistency_flags_status ON consistency_flags(status);
COMMIT;
