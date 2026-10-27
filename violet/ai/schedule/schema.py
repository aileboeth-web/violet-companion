"""Phase 9.4.1: timetable tables (mirrored in migrations/phase94_up.sql).

Applied by VioletDatabase at every start (CREATE ... IF NOT EXISTS): an older database gets the tables once, a
restart changes nothing, and no other table is touched. Foreign keys are enforced (VioletDatabase turns
PRAGMA foreign_keys on; no older table declares one, so nothing else changes).

Rollback never drops data silently: older code simply ignores these tables (migrations/phase94_down.sql drops
nothing). drop_schedule_tables() removes them only when they are empty, or with confirm=True after it has
written a JSON backup of every row.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATE = "GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'"
TIME = "GLOB '[0-2][0-9]:[0-5][0-9]'"

SCHEMA_SQL = f"""
-- one row per import attempt (file fingerprint, preview / applied, summary without personal values)
CREATE TABLE IF NOT EXISTS schedule_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name TEXT NOT NULL CHECK (length(file_name) BETWEEN 1 AND 200),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    format TEXT NOT NULL CHECK (format IN ('csv', 'json')),
    term_id INTEGER REFERENCES schedule_terms(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN ('previewed', 'applied', 'failed', 'discarded')),
    summary TEXT NOT NULL DEFAULT '{{}}',
    error_count INTEGER NOT NULL DEFAULT 0 CHECK (error_count >= 0),
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_schedule_imports_sha ON schedule_imports(sha256, status);

-- a term: its dates, the Monday of teaching week 1 and the time zone its wall-clock times are in
CREATE TABLE IF NOT EXISTS schedule_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) BETWEEN 1 AND 60),
    start_date TEXT NOT NULL CHECK (start_date {DATE}),
    end_date TEXT NOT NULL CHECK (end_date {DATE}),
    week1_monday TEXT NOT NULL CHECK (week1_monday {DATE}),
    timezone TEXT NOT NULL CHECK (length(timezone) BETWEEN 1 AND 64),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    source TEXT NOT NULL CHECK (source IN ('manual', 'import')),
    import_id INTEGER REFERENCES schedule_imports(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (end_date >= start_date),
    CHECK (week1_monday <= start_date)
);
CREATE INDEX IF NOT EXISTS idx_schedule_terms_dates ON schedule_terms(status, start_date, end_date);

-- the term's period table: period n starts / ends at these wall-clock times
CREATE TABLE IF NOT EXISTS schedule_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id INTEGER NOT NULL REFERENCES schedule_terms(id) ON DELETE CASCADE,
    period_no INTEGER NOT NULL CHECK (period_no BETWEEN 1 AND 20),
    start_time TEXT NOT NULL CHECK (start_time {TIME}),
    end_time TEXT NOT NULL CHECK (end_time {TIME}),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (term_id, period_no),
    CHECK (end_time > start_time)
);

-- a course of a term (teacher / notes are personal)
CREATE TABLE IF NOT EXISTS schedule_courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id INTEGER NOT NULL REFERENCES schedule_terms(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 60),
    code TEXT CHECK (code IS NULL OR length(code) <= 40),
    teacher TEXT CHECK (teacher IS NULL OR length(teacher) <= 40),
    kind TEXT NOT NULL DEFAULT 'lecture' CHECK (kind IN ('lecture', 'lab', 'seminar', 'pe', 'other')),
    notes TEXT CHECK (notes IS NULL OR length(notes) <= 500),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
    source TEXT NOT NULL CHECK (source IN ('manual', 'import')),
    import_id INTEGER REFERENCES schedule_imports(id) ON DELETE SET NULL,
    import_key TEXT CHECK (import_key IS NULL OR length(import_key) <= 120),
    manual_override INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedule_courses_term ON schedule_courses(term_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule_courses_import_key
    ON schedule_courses(term_id, import_key) WHERE import_key IS NOT NULL;

-- a weekly meeting of a course ("课程周期时间表"): weekday, wall-clock times OR a period range, the teaching
-- weeks it runs in (week_from..week_to, parity all / odd / even, minus skip_weeks), location (personal),
-- optional date validity, status and source. The weeks it happens in are exactly
--     {{ w : week_from <= w <= week_to, parity matches w, w not in skip_weeks }}
CREATE TABLE IF NOT EXISTS schedule_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES schedule_courses(id) ON DELETE CASCADE,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 1 AND 7),
    start_time TEXT CHECK (start_time IS NULL OR start_time {TIME}),
    end_time TEXT CHECK (end_time IS NULL OR end_time {TIME}),
    period_start INTEGER CHECK (period_start IS NULL OR period_start BETWEEN 1 AND 20),
    period_end INTEGER CHECK (period_end IS NULL OR period_end BETWEEN 1 AND 20),
    week_from INTEGER NOT NULL CHECK (week_from BETWEEN 1 AND 30),
    week_to INTEGER NOT NULL CHECK (week_to BETWEEN 1 AND 30),
    parity TEXT NOT NULL DEFAULT 'all' CHECK (parity IN ('all', 'odd', 'even')),
    skip_weeks TEXT NOT NULL DEFAULT '[]',
    weeks_text TEXT CHECK (weeks_text IS NULL OR length(weeks_text) <= 60),
    location TEXT CHECK (location IS NULL OR length(location) <= 60),
    valid_from TEXT CHECK (valid_from IS NULL OR valid_from {DATE}),
    valid_until TEXT CHECK (valid_until IS NULL OR valid_until {DATE}),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
    source TEXT NOT NULL CHECK (source IN ('manual', 'import')),
    import_id INTEGER REFERENCES schedule_imports(id) ON DELETE SET NULL,
    import_key TEXT CHECK (import_key IS NULL OR length(import_key) <= 120),
    manual_override INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (week_to >= week_from),
    CHECK ((start_time IS NOT NULL AND end_time IS NOT NULL AND period_start IS NULL AND period_end IS NULL
            AND end_time > start_time)
        OR (start_time IS NULL AND end_time IS NULL AND period_start IS NOT NULL AND period_end IS NOT NULL
            AND period_end >= period_start)),
    CHECK (valid_from IS NULL OR valid_until IS NULL OR valid_until >= valid_from)
);
CREATE INDEX IF NOT EXISTS idx_schedule_slots_course ON schedule_slots(course_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule_slots_import_key
    ON schedule_slots(course_id, import_key) WHERE import_key IS NOT NULL;

-- a change to ONE occurrence (the meeting of slot_id on its nominal date on_date) or an extra class:
--   cancel  the occurrence does not happen
--   move    it happens at new_date / new times / new_location instead (the original date is freed)
--   time    same date, other times;  room  same date and time, other place
--   extra   an additional class of course_id on on_date (no slot)
-- At most one active exception of each kind per occurrence; precedence cancel > move > time > room.
CREATE TABLE IF NOT EXISTS schedule_exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id INTEGER NOT NULL REFERENCES schedule_terms(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES schedule_courses(id) ON DELETE CASCADE,
    slot_id INTEGER REFERENCES schedule_slots(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('cancel', 'move', 'time', 'room', 'extra')),
    on_date TEXT NOT NULL CHECK (on_date {DATE}),
    new_date TEXT CHECK (new_date IS NULL OR new_date {DATE}),
    new_start_time TEXT CHECK (new_start_time IS NULL OR new_start_time {TIME}),
    new_end_time TEXT CHECK (new_end_time IS NULL OR new_end_time {TIME}),
    new_period_start INTEGER CHECK (new_period_start IS NULL OR new_period_start BETWEEN 1 AND 20),
    new_period_end INTEGER CHECK (new_period_end IS NULL OR new_period_end BETWEEN 1 AND 20),
    new_location TEXT CHECK (new_location IS NULL OR length(new_location) <= 60),
    reason TEXT CHECK (reason IS NULL OR length(reason) <= 200),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    source TEXT NOT NULL CHECK (source IN ('manual', 'import')),
    import_id INTEGER REFERENCES schedule_imports(id) ON DELETE SET NULL,
    import_key TEXT CHECK (import_key IS NULL OR length(import_key) <= 120),
    manual_override INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((kind = 'extra') = (slot_id IS NULL)),
    CHECK ((new_start_time IS NULL) = (new_end_time IS NULL)),
    CHECK ((new_period_start IS NULL) = (new_period_end IS NULL)),
    CHECK (new_start_time IS NULL OR new_period_start IS NULL),
    CHECK (new_start_time IS NULL OR new_end_time > new_start_time),
    CHECK (new_period_start IS NULL OR new_period_end >= new_period_start),
    CHECK (kind <> 'cancel' OR (new_date IS NULL AND new_start_time IS NULL AND new_period_start IS NULL
                                AND new_location IS NULL)),
    CHECK (kind <> 'move' OR new_date IS NOT NULL OR new_start_time IS NOT NULL OR new_period_start IS NOT NULL),
    CHECK (kind <> 'time' OR (new_date IS NULL AND (new_start_time IS NOT NULL OR new_period_start IS NOT NULL))),
    CHECK (kind <> 'room' OR (new_location IS NOT NULL AND new_date IS NULL AND new_start_time IS NULL
                              AND new_period_start IS NULL)),
    CHECK (kind <> 'extra' OR (new_date IS NULL AND (new_start_time IS NOT NULL OR new_period_start IS NOT NULL)))
);
CREATE INDEX IF NOT EXISTS idx_schedule_exceptions_term ON schedule_exceptions(term_id, status, on_date);
CREATE INDEX IF NOT EXISTS idx_schedule_exceptions_new_date ON schedule_exceptions(term_id, status, new_date);
CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule_exceptions_one_per_kind
    ON schedule_exceptions(slot_id, on_date, kind) WHERE status = 'active' AND slot_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule_exceptions_import_key
    ON schedule_exceptions(term_id, import_key) WHERE import_key IS NOT NULL;

-- an exam (location / seat are personal); times may be unknown (both NULL)
CREATE TABLE IF NOT EXISTS schedule_exams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id INTEGER NOT NULL REFERENCES schedule_terms(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES schedule_courses(id) ON DELETE SET NULL,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 60),
    kind TEXT NOT NULL DEFAULT 'other' CHECK (kind IN ('final', 'midterm', 'quiz', 'other')),
    exam_date TEXT NOT NULL CHECK (exam_date {DATE}),
    start_time TEXT CHECK (start_time IS NULL OR start_time {TIME}),
    end_time TEXT CHECK (end_time IS NULL OR end_time {TIME}),
    location TEXT CHECK (location IS NULL OR length(location) <= 60),
    seat TEXT CHECK (seat IS NULL OR length(seat) <= 20),
    notes TEXT CHECK (notes IS NULL OR length(notes) <= 500),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled', 'deleted')),
    source TEXT NOT NULL CHECK (source IN ('manual', 'import')),
    import_id INTEGER REFERENCES schedule_imports(id) ON DELETE SET NULL,
    import_key TEXT CHECK (import_key IS NULL OR length(import_key) <= 120),
    manual_override INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((start_time IS NULL) = (end_time IS NULL)),
    CHECK (start_time IS NULL OR end_time > start_time)
);
CREATE INDEX IF NOT EXISTS idx_schedule_exams_date ON schedule_exams(term_id, status, exam_date);
CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule_exams_import_key
    ON schedule_exams(term_id, import_key) WHERE import_key IS NOT NULL;

-- days without regular classes (holiday / no_class) and make-up teaching days (makeup: a single day that
-- runs the timetable of follows_date -- weekday, teaching week and parity all taken from follows_date)
CREATE TABLE IF NOT EXISTS schedule_holidays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id INTEGER NOT NULL REFERENCES schedule_terms(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('holiday', 'no_class', 'makeup')),
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 60),
    start_date TEXT NOT NULL CHECK (start_date {DATE}),
    end_date TEXT NOT NULL CHECK (end_date {DATE}),
    follows_date TEXT CHECK (follows_date IS NULL OR follows_date {DATE}),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
    source TEXT NOT NULL CHECK (source IN ('manual', 'import')),
    import_id INTEGER REFERENCES schedule_imports(id) ON DELETE SET NULL,
    import_key TEXT CHECK (import_key IS NULL OR length(import_key) <= 120),
    manual_override INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (end_date >= start_date),
    CHECK ((kind = 'makeup') = (follows_date IS NOT NULL)),
    CHECK (kind <> 'makeup' OR (start_date = end_date AND follows_date <> start_date))
);
CREATE INDEX IF NOT EXISTS idx_schedule_holidays_dates ON schedule_holidays(term_id, status, start_date, end_date);
CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule_holidays_makeup
    ON schedule_holidays(term_id, start_date) WHERE kind = 'makeup' AND status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule_holidays_import_key
    ON schedule_holidays(term_id, import_key) WHERE import_key IS NOT NULL;

-- every change, before / after as JSON (kept when the entity is gone; removed only by purge / rollback)
CREATE TABLE IF NOT EXISTS schedule_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL CHECK (entity IN ('term', 'period', 'course', 'slot', 'exception', 'exam', 'holiday',
                                           'import')),
    entity_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('create', 'update', 'delete', 'restore', 'import_skip')),
    before_json TEXT,
    after_json TEXT,
    actor TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('manual', 'import')),
    import_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedule_revisions_entity ON schedule_revisions(entity, entity_id, id);
"""

# children first: the order a removal must follow with foreign keys on
TABLES = ("schedule_revisions", "schedule_exceptions", "schedule_exams", "schedule_holidays", "schedule_slots",
          "schedule_courses", "schedule_periods", "schedule_imports", "schedule_terms")


def apply(conn) -> None:
    """Create the tables (idempotent). Called by VioletDatabase with its lock held."""
    conn.executescript(SCHEMA_SQL)


def present_tables(db) -> list[str]:
    rows = db.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'schedule_%'")
    return sorted(r["name"] for r in rows)


def row_counts(db) -> dict[str, int]:
    present = set(present_tables(db))
    return {t: int(db.fetch_one(f"SELECT COUNT(*) AS n FROM {t}")["n"]) for t in TABLES if t in present}


class RollbackRefused(RuntimeError):
    pass


def drop_schedule_tables(db, *, confirm: bool = False, backup_dir: str | Path | None = None) -> dict[str, Any]:
    """Remove the timetable tables (a rollback of phase 9.4.1). Empty tables are dropped; when any row exists,
    nothing happens unless confirm=True, and then every row is written to a JSON backup file first. The rest of
    the database is never touched. Returns what was done."""
    counts = row_counts(db)
    rows = sum(counts.values())
    if rows and not confirm:
        raise RollbackRefused(f"timetable tables hold {rows} rows; nothing dropped (confirm=True writes a backup "
                              "first)")
    backup = None
    if rows:
        folder = Path(backup_dir or Path(db.db_path).parent)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = folder / f"schedule-backup-{stamp}.json"
        payload = {t: db.fetch_all(f"SELECT * FROM {t} ORDER BY id") for t in counts}
        backup.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    with db.transaction():
        for table in TABLES:
            db.execute(f"DROP TABLE IF EXISTS {table}")
    return {"dropped": list(counts), "rows": rows, "backup": str(backup) if backup else None}
