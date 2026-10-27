import os
import sqlite3
import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional


class VioletDatabase:
    """SQLite storage layer for Violet chat messages."""

    VALID_ROLES = {"user", "assistant"}
    VALID_DIRECTIONS = {"incoming", "outgoing"}
    VALID_MESSAGE_TYPES = {"text", "image", "file", "voice", "video"}
    VALID_TASK_STATUSES = {"pending_info", "completed", "failed", "cancelled"}
    ACTIVE_TASK_STATUSES = {"pending_info"}
    # Inbox / outbox statuses are validated here instead of SQL CHECK constraints,
    # so later phases can add states without rebuilding tables.
    INBOUND_STATUSES = {"received", "processing", "done", "failed", "skipped", "expired"}
    # 'unknown': interrupted while sending; never resent automatically (ai/reconcile.py)
    OUTBOUND_STATUSES = {"queued", "sending", "sent", "failed", "cancelled", "skipped", "unknown"}
    COMMITMENT_STATUSES = {"pending", "sending", "sent", "cancelled", "failed", "expired", "unknown"}
    MEMORY_JOB_STATUSES = {"waiting_reply", "pending", "running", "done", "failed"}

    def __init__(self, db_path: str | Path | None = None, initialize: bool = True):
        project_root = Path(__file__).resolve().parent
        env_path = os.environ.get("VIOLET_DB_PATH", "").strip()
        if db_path:
            self.db_path = Path(db_path)
        elif env_path:
            self.db_path = Path(env_path)
        else:
            self.db_path = project_root / "violet.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # One connection is shared by the poll loop, turn worker, scheduler and
        # Flask threads; the lock keeps statements and their commits from interleaving.
        self._lock = threading.RLock()
        self._tx_depth = 0          # > 0 while transaction() is open (only ever in the thread holding _lock)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        # Deleted rows are overwritten on disk (personal data deletion, phase 3).
        self.conn.execute("PRAGMA secure_delete = ON")
        # Phase 9.4.1: the timetable tables declare foreign keys (no older table does, so nothing else changes).
        self.conn.execute("PRAGMA foreign_keys = ON")

        if initialize:
            self.initialize_schema()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            if self._tx_depth:
                # inside transaction(): no per-statement commit; the transaction decides
                return self.conn.execute(sql, tuple(params))
            with self.conn:
                return self.conn.execute(sql, tuple(params))

    @contextmanager
    def transaction(self):
        """All statements inside commit together or not at all (phase 7: personal-data deletion).

        Holds the connection lock for the whole block, so other threads' writes wait
        instead of interleaving. Nested use joins the outer transaction. A rollback
        undoes database changes only -- never files, sent WeChat messages or backups.
        """
        with self._lock:
            if self._tx_depth:
                self._tx_depth += 1
                try:
                    yield self
                finally:
                    self._tx_depth -= 1
                return
            if self.conn.in_transaction:
                self.conn.commit()
            self.conn.execute("BEGIN IMMEDIATE")
            self._tx_depth = 1
            try:
                yield self
            except BaseException:
                self._tx_depth = 0
                self.conn.rollback()
                raise
            self._tx_depth = 0
            self.conn.commit()

    def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def initialize_schema(self) -> None:
        with self._lock:
            self._initialize_schema_locked()

    # Columns added to event_memories in phase 2. Defaults describe pre-existing
    # rows truthfully ("legacy" kind, progress unknown) so no data is rewritten.
    _EVENT_MEMORY_COLUMNS = (
        ("kind", "TEXT NOT NULL DEFAULT 'legacy'"),
        ("progress", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("event_time", "TEXT"),
        ("event_key", "TEXT"),
        ("source_turn_id", "TEXT"),
        ("progress_updated_at", "TEXT"),
        ("last_user_mention_at", "TEXT"),
        ("confidence", "REAL NOT NULL DEFAULT 1.0"),
        # Phase 3: the user can forbid a memory from being used in model context.
        ("use_in_context", "INTEGER NOT NULL DEFAULT 1"),
        # Phase 9.2.1: how settled a future event is -- NULL (older rows / not said), plan, conditional (with the
        # condition, still open), wish. A conditional or wished event is never "planned" until the user settles it.
        ("intent_level", "TEXT"),
        ("intent_condition", "TEXT"),
    )

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        existing = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _initialize_schema_locked(self) -> None:
        self._create_tables_locked()
        for column, ddl in self._EVENT_MEMORY_COLUMNS:
            self._ensure_column("event_memories", column, ddl)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_event_memories_event_key ON event_memories(event_key)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_event_memories_source_turn ON event_memories(source_turn_id)")
        # Phase 9.4 (real run cys-p94-sched-r1): what each attempt of a memory job really wrote and why it stopped
        # (JSON; NULL on older rows = nothing recorded). The status column keeps its meaning.
        self._ensure_column("memory_jobs", "progress", "TEXT")
        # Phase 9.4.1: the timetable (ai/schedule/schema.py; mirrored in migrations/phase94_up.sql)
        from ai.schedule import schema as schedule_schema
        schedule_schema.apply(self.conn)
        # Phase 10: which chat channel a message came from / went to (NULL on older rows = WeChat, the only channel
        # there was), the platform sender, and the owner's bound accounts (migrations/phase10_up.sql)
        for table in ("messages", "inbound_messages", "outbound_messages"):
            self._ensure_column(table, "channel", "TEXT")
        for table in ("inbound_messages", "outbound_messages"):
            self._ensure_column(table, "peer_id", "TEXT")
        from ai.channels.identity import SCHEMA as channel_schema
        self.conn.executescript(channel_schema)
        self.conn.commit()

    def _create_tables_locked(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                direction TEXT NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
                message_type TEXT NOT NULL CHECK (message_type IN ('text', 'image', 'file', 'voice', 'video')),
                content TEXT,
                media_id TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS message_stats (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            INSERT OR IGNORE INTO message_stats (key, value, updated_at)
            VALUES ('received_count', 0, datetime('now'));

            INSERT OR IGNORE INTO message_stats (key, value, updated_at)
            VALUES ('sent_count', 0, datetime('now'));

            CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);
            CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(role);
            CREATE INDEX IF NOT EXISTS idx_messages_type ON messages(message_type);
            CREATE INDEX IF NOT EXISTS idx_messages_media_id ON messages(media_id);

            CREATE TABLE IF NOT EXISTS task_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL CHECK (status IN ('pending_info', 'completed', 'failed', 'cancelled')),
                task_name TEXT NOT NULL,
                task_description TEXT NOT NULL,
                task_info TEXT,
                last_question TEXT,
                result TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_message_at TEXT,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_task_sessions_status ON task_sessions(status);
            CREATE INDEX IF NOT EXISTS idx_task_sessions_updated_at ON task_sessions(updated_at);
            CREATE INDEX IF NOT EXISTS idx_task_sessions_last_message_at ON task_sessions(last_message_at);

            CREATE TABLE IF NOT EXISTS event_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                happened_at TEXT,
                valid_until TEXT,
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'archived', 'forgotten', 'deleted')),
                importance REAL NOT NULL DEFAULT 1.0,
                emotional_weight REAL NOT NULL DEFAULT 1.0,
                recall_count INTEGER NOT NULL DEFAULT 1,
                mention_count INTEGER NOT NULL DEFAULT 1,
                forget_weight REAL NOT NULL DEFAULT 1.0,
                source_message_ids TEXT,
                last_recalled_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_event_memories_status ON event_memories(status);
            CREATE INDEX IF NOT EXISTS idx_event_memories_category ON event_memories(category);
            CREATE INDEX IF NOT EXISTS idx_event_memories_happened_at ON event_memories(happened_at);
            CREATE INDEX IF NOT EXISTS idx_event_memories_valid_until ON event_memories(valid_until);
            CREATE INDEX IF NOT EXISTS idx_event_memories_forget_weight ON event_memories(forget_weight);

            CREATE TABLE IF NOT EXISTS core_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_key TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 3,
                confidence REAL NOT NULL DEFAULT 1.0,
                source TEXT NOT NULL DEFAULT 'memory_agent',
                mention_count INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_core_memories_category ON core_memories(category);
            CREATE INDEX IF NOT EXISTS idx_core_memories_priority ON core_memories(priority);

            CREATE TABLE IF NOT EXISTS proactive_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('planned', 'skipped', 'sent', 'task_started', 'failed')),
                action_type TEXT NOT NULL DEFAULT 'none',
                reason TEXT,
                decision TEXT,
                task_id INTEGER,
                sent_messages TEXT,
                metadata TEXT,
                scheduled_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_proactive_events_created_at ON proactive_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_proactive_events_status ON proactive_events(status);
            CREATE INDEX IF NOT EXISTS idx_proactive_events_event_type ON proactive_events(event_type);

            CREATE TABLE IF NOT EXISTS proactive_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            );

            -- Durable inbox: one row per received WeChat item, written before the
            -- poll cursor is committed. dedup_key makes redelivery idempotent.
            CREATE TABLE IF NOT EXISTS inbound_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedup_key TEXT NOT NULL UNIQUE,
                wx_message_id TEXT,
                item_index INTEGER NOT NULL DEFAULT 0,
                item_type INTEGER,
                content TEXT,
                status TEXT NOT NULL,
                turn_id TEXT,
                stored_message_id INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                received_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_inbound_messages_status ON inbound_messages(status);
            CREATE INDEX IF NOT EXISTS idx_inbound_messages_received_at ON inbound_messages(received_at);

            -- Outbox: every outgoing message attempt with an explicit final status.
            CREATE TABLE IF NOT EXISTS outbound_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL UNIQUE,
                origin TEXT NOT NULL,
                turn_id TEXT,
                proactive_event_id INTEGER,
                message_type TEXT NOT NULL,
                content TEXT,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_outbound_messages_status ON outbound_messages(status);
            CREATE INDEX IF NOT EXISTS idx_outbound_messages_created_at ON outbound_messages(created_at);

            -- Phase 2: audit trail for memory changes (progress, corrections, retractions).
            CREATE TABLE IF NOT EXISTS memory_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_table TEXT NOT NULL,
                memory_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                old_content TEXT,
                new_content TEXT,
                old_progress TEXT,
                new_progress TEXT,
                reason TEXT,
                source TEXT,
                source_turn_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memory_revisions_memory ON memory_revisions(memory_table, memory_id);

            -- Phase 2: explicit reminders / follow-ups the user asked for.
            CREATE TABLE IF NOT EXISTS commitments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                due_at TEXT NOT NULL,
                status TEXT NOT NULL,
                evidence TEXT,
                source_turn_id TEXT,
                created_by TEXT NOT NULL DEFAULT 'user',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_error TEXT,
                proactive_event_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                delivered_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_commitments_status_due ON commitments(status, due_at);

            -- Phase 2: what Violet brought up on her own, and how the user responded.
            CREATE TABLE IF NOT EXISTS topic_mentions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_key TEXT NOT NULL,
                memory_id INTEGER,
                source TEXT NOT NULL,
                proactive_event_id INTEGER,
                user_engaged INTEGER,
                created_at TEXT NOT NULL,
                evaluated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_topic_mentions_topic ON topic_mentions(topic_key, created_at);
            CREATE INDEX IF NOT EXISTS idx_topic_mentions_memory ON topic_mentions(memory_id, created_at);

            -- Phase 2: background memory consolidation, one job per user turn.
            CREATE TABLE IF NOT EXISTS memory_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id TEXT NOT NULL UNIQUE,
                user_message TEXT NOT NULL,
                user_message_id INTEGER,
                assistant_reply TEXT,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_memory_jobs_status ON memory_jobs(status, next_attempt_at);

            -- Phase 3: structured user profile (stable facts about the user).
            CREATE TABLE IF NOT EXISTS user_profile_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_key TEXT NOT NULL,
                category TEXT NOT NULL,
                value TEXT NOT NULL,
                origin TEXT NOT NULL,
                source TEXT NOT NULL,
                source_ref TEXT,
                source_message_id INTEGER,
                source_turn_id TEXT,
                evidence TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                stated_at TEXT,
                confirmed_at TEXT,
                valid_from TEXT,
                valid_until TEXT,
                sensitivity TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL,
                use_in_context INTEGER NOT NULL DEFAULT 1,
                supersedes_id INTEGER,
                superseded_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_profile_facts_key ON user_profile_facts(fact_key, status);
            CREATE INDEX IF NOT EXISTS idx_profile_facts_source_ref ON user_profile_facts(source_ref);

            -- Phase 3: short-lived situations ("today at home"), never mixed with facts.
            CREATE TABLE IF NOT EXISTS user_situations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                source TEXT NOT NULL,
                evidence TEXT,
                source_turn_id TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                observed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                sensitivity TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL,
                use_in_context INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_situations_kind ON user_situations(kind, status);

            -- Phase 3: how the user likes to be talked to (explicit beats implicit).
            CREATE TABLE IF NOT EXISTS interaction_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pref_key TEXT NOT NULL,
                value TEXT NOT NULL,
                origin TEXT NOT NULL,
                evidence TEXT,
                source_turn_id TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                observations INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_interaction_preferences_key ON interaction_preferences(pref_key, status);

            CREATE TABLE IF NOT EXISTS preference_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pref_key TEXT NOT NULL,
                value TEXT NOT NULL,
                source TEXT NOT NULL,
                turn_id TEXT,
                observed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_preference_observations_key ON preference_observations(pref_key, observed_at);

            -- Phase 3: optional get-to-know-you questions and their outcome.
            CREATE TABLE IF NOT EXISTS profile_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_key TEXT NOT NULL,
                outcome TEXT NOT NULL,
                turn_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_profile_questions_key ON profile_questions(fact_key, outcome);

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

            -- Phase 6: every image file Violet received or produced. Files live under the media root
            -- (VIOLET_MEDIA_DIR, git-ignored); storage_path is relative and generated by code.
            CREATE TABLE IF NOT EXISTS media_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_uid TEXT NOT NULL UNIQUE,
                task_id TEXT,
                provider_id TEXT,
                model_id TEXT,
                direction TEXT NOT NULL,
                media_kind TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                width INTEGER,
                height INTEGER,
                file_size INTEGER NOT NULL,
                source_inbound_id INTEGER,
                source_message_id INTEGER,
                source_asset_id INTEGER,
                life_event_id INTEGER,
                scene_kind TEXT,
                generated_prompt TEXT,
                caption TEXT,
                ai_generated INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                send_status TEXT NOT NULL DEFAULT 'not_sent',
                send_error TEXT,
                sent_at TEXT,
                deleted_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_media_assets_task ON media_assets(task_id);
            CREATE INDEX IF NOT EXISTS idx_media_assets_status ON media_assets(status, expires_at);

            -- Phase 6: standard image tasks (idempotent by idempotency_key).
            CREATE TABLE IF NOT EXISTS image_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL UNIQUE,
                idempotency_key TEXT UNIQUE,
                turn_key TEXT,
                kind TEXT NOT NULL,
                origin TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_model TEXT,
                entry_id TEXT,
                prompt TEXT,
                params TEXT,
                scene TEXT,
                input_asset_ids TEXT,
                output_asset_ids TEXT,
                result_text TEXT,
                attempts TEXT,
                error_code TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_image_tasks_turn ON image_tasks(turn_key);

            -- Phase 6: every provider call (budgets, costs, failures, latency).
            CREATE TABLE IF NOT EXISTS image_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                accepted TEXT,
                images INTEGER NOT NULL DEFAULT 0,
                est_cost REAL,
                actual_cost REAL,
                latency_ms INTEGER,
                error_code TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_image_calls_entry ON image_calls(entry_id, created_at);

            -- Phase 6: dashboard overrides of the provider config (enable / disable / priority).
            CREATE TABLE IF NOT EXISTS image_model_settings (
                entry_id TEXT PRIMARY KEY,
                enabled INTEGER,
                priority INTEGER,
                updated_at TEXT NOT NULL
            );

            -- Phase 6: capability checks against the real API (a pass counts only for the same config hash).
            CREATE TABLE IF NOT EXISTS image_capability_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id TEXT NOT NULL,
                capability TEXT NOT NULL,
                passed INTEGER NOT NULL,
                detail TEXT,
                config_hash TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            -- Phase 6: history of Violet's visual profile (persona/visual_profile.json).
            CREATE TABLE IF NOT EXISTS visual_profile_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                content TEXT NOT NULL,
                actor TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL
            );

            -- Phase 3: audit trail of personal-data changes. Never stores the values.
            CREATE TABLE IF NOT EXISTS profile_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity TEXT NOT NULL,
                entity_id INTEGER,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                fact_key TEXT,
                detail TEXT,
                created_at TEXT NOT NULL
            );

            """
        )
        self.conn.commit()

    @staticmethod
    def now() -> str:
        from ai.settings import now_local
        return now_local().isoformat()

    @staticmethod
    def to_json(value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def from_json(value: str | None, default: Any = None) -> Any:
        if value is None:
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default

    def _decode_task_row(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        row["task_info"] = self.from_json(row.get("task_info"), default={})
        row["task_state"] = row["task_info"]  # backward-compatible alias
        row["result"] = self.from_json(row.get("result"), default=None)
        return row

    def _decode_event_memory_row(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        row["source_message_ids"] = self.from_json(row.get("source_message_ids"), default=[])
        return row

    @staticmethod
    def merge_dict(base: dict[str, Any] | None, patch: dict[str, Any] | None) -> dict[str, Any]:
        """Recursively merge patch into base and return a new dict."""
        merged = dict(base or {})
        for key, value in (patch or {}).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = VioletDatabase.merge_dict(merged[key], value)
            else:
                merged[key] = value
        return merged

    @staticmethod
    def normalize_text(text: str) -> str:
        text = (text or "").lower().strip()
        return re.sub(r"\s+", "", text)

    @staticmethod
    def text_similarity(first: str, second: str) -> float:
        first_norm = VioletDatabase.normalize_text(first)
        second_norm = VioletDatabase.normalize_text(second)
        if not first_norm or not second_norm:
            return 0.0
        if first_norm == second_norm:
            return 1.0
        if first_norm in second_norm or second_norm in first_norm:
            # Full containment is at least as strong as full character coverage
            # (0.8 below); the old pure length ratio made exact short queries
            # ("服务器" in a long memory) score lower than partial overlaps.
            ratio = min(len(first_norm), len(second_norm)) / max(len(first_norm), len(second_norm))
            return max(ratio, 0.8)

        first_chars = set(first_norm)
        second_chars = set(second_norm)
        if not first_chars or not second_chars:
            return 0.0
        intersection = len(first_chars & second_chars)
        jaccard = intersection / len(first_chars | second_chars)
        coverage = intersection / min(len(first_chars), len(second_chars))
        return max(jaccard, coverage * 0.8)


    def add_message(
        self,
        role: str,
        direction: str,
        message_type: str,
        content: str | None = None,
        media_id: str | None = None,
        created_at: str | None = None,
        channel: str | None = None,
    ) -> int:
        if role not in self.VALID_ROLES:
            raise ValueError(f"Invalid role: {role}")
        if direction not in self.VALID_DIRECTIONS:
            raise ValueError(f"Invalid direction: {direction}")
        if message_type not in self.VALID_MESSAGE_TYPES:
            raise ValueError(f"Invalid message_type: {message_type}")

        cursor = self.execute(
            """
            INSERT INTO messages
            (role, direction, message_type, content, media_id, created_at, channel)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (role, direction, message_type, content, media_id, created_at or self.now(), channel),
        )
        return int(cursor.lastrowid)

    def add_user_message(
        self,
        message_type: str,
        content: str | None = None,
        media_id: str | None = None,
        created_at: str | None = None,
        channel: str | None = None,
    ) -> int:
        return self.add_message(
            role="user",
            direction="incoming",
            message_type=message_type,
            content=content,
            media_id=media_id,
            created_at=created_at,
            channel=channel,
        )

    def add_assistant_message(
        self,
        message_type: str,
        content: str | None = None,
        media_id: str | None = None,
        created_at: str | None = None,
        channel: str | None = None,
    ) -> int:
        return self.add_message(
            role="assistant",
            direction="outgoing",
            message_type=message_type,
            content=content,
            media_id=media_id,
            created_at=created_at,
            channel=channel,
        )

    def get_message(self, message_id: int) -> Optional[dict[str, Any]]:
        return self.fetch_one("SELECT * FROM messages WHERE id = ?", (message_id,))

    def get_recent_messages(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.fetch_all(
            """
            SELECT id, role, direction, message_type, content, media_id, created_at
            FROM messages
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return list(reversed(rows))

    def get_latest_completed_turn_messages(self) -> list[dict[str, Any]]:
        """Return the latest completed conversation turn, starting with assistant messages.

        A completed turn is the newest continuous block of assistant messages followed by
        user messages. If the latest message is from the user and has not been answered
        yet, that pending user block is skipped first.
        """
        rows = self.fetch_all(
            """
            SELECT id, role, direction, message_type, content, media_id, created_at
            FROM messages
            ORDER BY created_at DESC, id DESC
            """
        )
        if not rows:
            return []

        index = 0

        while index < len(rows) and rows[index]["role"] == "user":
            index += 1

        if index >= len(rows) or rows[index]["role"] != "assistant":
            return []

        turn_rows = []

        while index < len(rows) and rows[index]["role"] == "assistant":
            turn_rows.append(rows[index])
            index += 1

        while index < len(rows) and rows[index]["role"] == "user":
            turn_rows.append(rows[index])
            index += 1

        return list(reversed(turn_rows))

    def get_messages_between(self, start_at: str, end_at: str) -> list[dict[str, Any]]:
        return self.fetch_all(
            """
            SELECT id, role, direction, message_type, content, media_id, created_at
            FROM messages
            WHERE created_at >= ? AND created_at < ?
            ORDER BY created_at ASC, id ASC
            """,
            (start_at, end_at),
        )

    def delete_message(self, message_id: int) -> None:
        self.execute("DELETE FROM messages WHERE id = ?", (message_id,))

    def increment_stat(self, key: str, amount: int = 1) -> int:
        self.execute(
            """
            INSERT INTO message_stats (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = value + excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, amount, self.now()),
        )
        return self.get_stat(key)

    def increment_received_count(self, amount: int = 1) -> int:
        return self.increment_stat("received_count", amount)

    def increment_sent_count(self, amount: int = 1) -> int:
        return self.increment_stat("sent_count", amount)

    def get_stat(self, key: str, default: int = 0) -> int:
        row = self.fetch_one("SELECT value FROM message_stats WHERE key = ?", (key,))
        return int(row["value"]) if row else default

    def get_message_stats(self) -> dict[str, int]:
        rows = self.fetch_all("SELECT key, value FROM message_stats")
        stats = {row["key"]: int(row["value"]) for row in rows}
        stats.setdefault("received_count", 0)
        stats.setdefault("sent_count", 0)
        return stats

    def reset_message_stats(self) -> None:
        now = self.now()
        self.execute(
            """
            INSERT INTO message_stats (key, value, updated_at)
            VALUES ('received_count', 0, ?), ('sent_count', 0, ?)
            ON CONFLICT(key) DO UPDATE SET value = 0, updated_at = excluded.updated_at
            """,
            (now, now),
        )

    def create_task_session(
        self,
        task_name: str,
        task_description: str,
        task_info: dict[str, Any] | None = None,
        status: str = "pending_info",
        last_question: str | None = None,
        last_message_at: str | None = None,
    ) -> int:
        if status not in self.VALID_TASK_STATUSES:
            raise ValueError(f"Invalid task status: {status}")
        now = self.now()
        cursor = self.execute(
            """
            INSERT INTO task_sessions
            (status, task_name, task_description, task_info, last_question, created_at, updated_at, last_message_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                status,
                task_name,
                task_description,
                self.to_json(task_info or {}),
                last_question,
                now,
                now,
                last_message_at or now,
            ),
        )
        return int(cursor.lastrowid)

    def get_task_session(self, task_id: int) -> dict[str, Any] | None:
        row = self.fetch_one("SELECT * FROM task_sessions WHERE id = ?", (task_id,))
        return self._decode_task_row(row)

    def get_active_task_session(self) -> dict[str, Any] | None:
        placeholders = ", ".join("?" for _ in self.ACTIVE_TASK_STATUSES)
        row = self.fetch_one(
            f"""
            SELECT * FROM task_sessions
            WHERE status IN ({placeholders})
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            tuple(self.ACTIVE_TASK_STATUSES),
        )
        return self._decode_task_row(row)

    def list_active_task_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in self.ACTIVE_TASK_STATUSES)
        rows = self.fetch_all(
            f"""
            SELECT * FROM task_sessions
            WHERE status IN ({placeholders})
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (*tuple(self.ACTIVE_TASK_STATUSES), limit),
        )
        return [self._decode_task_row(row) for row in rows]

    def list_task_sessions(
        self,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if status:
            rows = self.fetch_all(
                """
                SELECT * FROM task_sessions
                WHERE status = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (status, limit),
            )
        else:
            rows = self.fetch_all(
                """
                SELECT * FROM task_sessions
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [self._decode_task_row(row) for row in rows]

    def update_task_session(
        self,
        task_id: int,
        status: str | None = None,
        task_info: dict[str, Any] | None = None,
        last_question: str | None = None,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
        touch_last_message: bool = True,
    ) -> None:
        current = self.get_task_session(task_id)
        if not current:
            raise ValueError(f"Task session not found: {task_id}")

        next_status = status or current["status"]
        if next_status not in self.VALID_TASK_STATUSES:
            raise ValueError(f"Invalid task status: {next_status}")
        now = self.now()
        completed_at = now if next_status in {"completed", "failed", "cancelled"} else current.get("completed_at")

        self.execute(
            """
            UPDATE task_sessions
            SET status = ?,
                task_info = ?,
                last_question = ?,
                result = ?,
                error_message = ?,
                updated_at = ?,
                last_message_at = ?,
                completed_at = ?
            WHERE id = ?
            """,
            (
                next_status,
                self.to_json(task_info if task_info is not None else current.get("task_info", {})),
                last_question if last_question is not None else current.get("last_question"),
                self.to_json(result) if result is not None else self.to_json(current.get("result")),
                error_message if error_message is not None else current.get("error_message"),
                now,
                now if touch_last_message else current.get("last_message_at"),
                completed_at,
                task_id,
            ),
        )

    def update_task_info(
        self,
        task_id: int,
        task_info: dict[str, Any],
        merge: bool = True,
    ) -> dict[str, Any]:
        current = self.get_task_session(task_id)
        if not current:
            raise ValueError(f"Task session not found: {task_id}")

        next_state = (
            self.merge_dict(current.get("task_info", {}), task_info)
            if merge
            else task_info
        )
        self.update_task_session(task_id=task_id, task_info=next_state)
        return next_state

    def replace_task_info(self, task_id: int, task_info: dict[str, Any]) -> dict[str, Any]:
        return self.update_task_info(task_id=task_id, task_info=task_info, merge=False)

    def update_task_state(self, task_id: int, task_state: dict[str, Any], merge: bool = True) -> dict[str, Any]:
        """Backward-compatible alias for update_task_info."""
        return self.update_task_info(task_id=task_id, task_info=task_state, merge=merge)

    def replace_task_state(self, task_id: int, task_state: dict[str, Any]) -> dict[str, Any]:
        """Backward-compatible alias for replace_task_info."""
        return self.replace_task_info(task_id=task_id, task_info=task_state)

    def change_task_status(self, task_id: int, status: str) -> None:
        self.update_task_session(task_id=task_id, status=status)

    def complete_task_session(self, task_id: int, result: dict[str, Any] | None = None) -> None:
        self.update_task_session(task_id=task_id, status="completed", result=result)

    def fail_task_session(self, task_id: int, error_message: str | None = None, result: dict[str, Any] | None = None) -> None:
        self.update_task_session(task_id=task_id, status="failed", error_message=error_message, result=result)

    def cancel_task_session(self, task_id: int, reason: str | None = None) -> None:
        self.update_task_session(task_id=task_id, status="cancelled", error_message=reason)

    def delete_task_session(self, task_id: int) -> None:
        self.execute("DELETE FROM task_sessions WHERE id = ?", (task_id,))


    def add_core_memory(
        self,
        memory_key: str,
        category: str,
        content: str,
        priority: int = 3,
        confidence: float = 1.0,
        source: str = "memory_agent",
    ) -> int:
        now = self.now()
        priority = max(1, min(5, int(priority)))
        confidence = max(0.0, min(1.0, float(confidence)))
        self.execute(
            """
            INSERT INTO core_memories
            (memory_key, category, content, priority, confidence, source, mention_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (memory_key, category, content, priority, confidence, source, now, now),
        )
        row = self.fetch_one("SELECT id FROM core_memories WHERE memory_key = ?", (memory_key,))
        return int(row["id"])

    def upsert_core_memory(
        self,
        memory_key: str,
        category: str,
        content: str,
        priority: int = 3,
        confidence: float = 1.0,
        source: str = "memory_agent",
        reinforce: bool = True,
    ) -> dict[str, Any]:
        now = self.now()
        priority = max(1, min(5, int(priority)))
        confidence = max(0.0, min(1.0, float(confidence)))
        existing = self.get_core_memory_by_key(memory_key)
        if existing:
            self.execute(
                """
                UPDATE core_memories
                SET category = ?, content = ?, priority = ?, confidence = ?, source = ?,
                    mention_count = mention_count + ?, updated_at = ?
                WHERE memory_key = ?
                """,
                (category, content, priority, confidence, source, 1 if reinforce else 0, now, memory_key),
            )
            return {"action": "updated", "memory": self.get_core_memory_by_key(memory_key)}

        memory_id = self.add_core_memory(memory_key, category, content, priority, confidence, source)
        return {"action": "created", "memory": self.get_core_memory(memory_id)}

    def get_core_memory(self, memory_id: int) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM core_memories WHERE id = ?", (memory_id,))

    def get_core_memory_by_key(self, memory_key: str) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM core_memories WHERE memory_key = ?", (memory_key,))

    def list_core_memories(
        self,
        category: str | None = None,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        if category:
            return self.fetch_all(
                """
                SELECT * FROM core_memories
                WHERE category = ?
                ORDER BY priority DESC, confidence DESC, updated_at DESC
                LIMIT ?
                """,
                (category, limit),
            )
        return self.fetch_all(
            """
            SELECT * FROM core_memories
            ORDER BY priority DESC, confidence DESC, updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    def search_core_memories(
        self,
        query: str,
        category: str | None = None,
        limit: int = 8,
        min_similarity: float = 0.35,
    ) -> list[dict[str, Any]]:
        candidates = self.list_core_memories(category=category, limit=200)
        scored = []
        for memory in candidates:
            score = max(
                self.text_similarity(query, memory.get("content", "")),
                self.text_similarity(query, memory.get("memory_key", "")),
                self.text_similarity(query, memory.get("category", "")),
            )
            if score >= min_similarity:
                memory["similarity"] = score
                scored.append(memory)
        scored.sort(key=lambda item: (item["similarity"], item.get("priority", 0), item.get("confidence", 0)), reverse=True)
        return scored[:limit]

    def update_core_memory(
        self,
        memory_id: int,
        content: str | None = None,
        category: str | None = None,
        memory_key: str | None = None,
        priority: int | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        current = self.get_core_memory(memory_id)
        if not current:
            raise ValueError(f"Core memory not found: {memory_id}")
        next_priority = current["priority"] if priority is None else max(1, min(5, int(priority)))
        next_confidence = current["confidence"] if confidence is None else max(0.0, min(1.0, float(confidence)))
        self.execute(
            """
            UPDATE core_memories
            SET memory_key = ?, category = ?, content = ?, priority = ?, confidence = ?,
                mention_count = mention_count + 1, updated_at = ?
            WHERE id = ?
            """,
            (
                memory_key or current["memory_key"],
                category or current["category"],
                content or current["content"],
                next_priority,
                next_confidence,
                self.now(),
                memory_id,
            ),
        )
        return self.get_core_memory(memory_id)

    def reinforce_core_memory(self, memory_id: int) -> dict[str, Any]:
        self.execute(
            """
            UPDATE core_memories
            SET mention_count = mention_count + 1, updated_at = ?
            WHERE id = ?
            """,
            (self.now(), memory_id),
        )
        return self.get_core_memory(memory_id)

    def delete_core_memory(self, memory_id: int) -> None:
        self.execute("DELETE FROM core_memories WHERE id = ?", (memory_id,))

    def add_event_memory(
        self,
        content: str,
        category: str = "general",
        happened_at: str | None = None,
        valid_until: str | None = None,
        importance: float = 1.0,
        emotional_weight: float = 1.0,
        forget_weight: float | None = None,
        source_message_ids: list[int] | None = None,
        status: str = "active",
        kind: str = "event",
        progress: str | None = None,
        event_time: str | None = None,
        event_key: str | None = None,
        source_turn_id: str | None = None,
        confidence: float = 1.0,
    ) -> int:
        from ai.memory_model import EVENT_PROGRESS, MEMORY_KINDS

        now = self.now()
        kind = kind if kind in MEMORY_KINDS else "event"
        if progress is None:
            progress = "na" if kind in ("fact", "background") else "unknown"
        if progress not in EVENT_PROGRESS:
            raise ValueError(f"Invalid event progress: {progress}")
        if forget_weight is None:
            forget_weight = max(0.1, float(importance) + float(emotional_weight) * 0.5)
        cursor = self.execute(
            """
            INSERT INTO event_memories
            (content, category, happened_at, valid_until, status, importance, emotional_weight,
             recall_count, mention_count, forget_weight, source_message_ids, created_at, updated_at,
             kind, progress, event_time, event_key, source_turn_id, progress_updated_at, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content,
                category,
                happened_at,
                valid_until,
                status,
                float(importance),
                float(emotional_weight),
                float(forget_weight),
                self.to_json(source_message_ids or []),
                now,
                now,
                kind,
                progress,
                event_time,
                event_key,
                source_turn_id,
                now,
                max(0.0, min(1.0, float(confidence))),
            ),
        )
        return int(cursor.lastrowid)

    def set_event_intent(self, memory_id: int, level: str | None, condition: str | None) -> None:
        """Phase 9.2.1: plan / conditional (+ the open condition) / wish of an event memory."""
        self.execute("UPDATE event_memories SET intent_level = ?, intent_condition = ?, updated_at = ? WHERE id = ?",
                     (level, condition, self.now(), int(memory_id)))

    def get_event_memory(self, memory_id: int) -> dict[str, Any] | None:
        row = self.fetch_one("SELECT * FROM event_memories WHERE id = ?", (memory_id,))
        return self._decode_event_memory_row(row)

    def list_event_memories(
        self,
        status: str | None = None,
        category: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if category:
            clauses.append("category = ?")
            params.append(category)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.fetch_all(
            f"""
            SELECT * FROM event_memories
            {where}
            ORDER BY forget_weight DESC, updated_at DESC, id DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        return [self._decode_event_memory_row(row) for row in rows]

    def search_event_memories(
        self,
        query: str,
        category: str | None = None,
        status: str | None = None,
        limit: int = 10,
        min_similarity: float = 0.35,
        include_disabled: bool = False,
        recall: bool = False,
    ) -> list[dict[str, Any]]:
        candidates = self.list_event_memories(status=status, category=category, limit=self.search_window())
        topics, focus = [], None
        if recall:
            from ai.recall_search import query_topics, recall_focus, recall_score
            topics = query_topics(query)
            focus = recall_focus(topics, [m.get("content", "") for m in candidates
                                         if self._event_searchable(m, status, include_disabled)])
        scored = []
        for memory in candidates:
            if not self._event_searchable(memory, status, include_disabled):
                continue
            score = self.text_similarity(query, memory.get("content", ""))
            if recall:
                evidence = recall_score(query, memory.get("content", ""), topics, focus, score, self.text_similarity)
                memory["retrieval"] = evidence
                score = evidence["similarity"]
                if score <= 0:
                    continue  # Even a zero/negative requested threshold cannot invent lexical evidence.
            if score >= min_similarity:
                memory["similarity"] = score
                scored.append(memory)
        scored.sort(key=lambda item: (item["similarity"], item.get("retrieval", {}).get("original_similarity", item["similarity"]),
                                      item.get("forget_weight", 0)), reverse=True)
        return scored[:limit]

    @staticmethod
    def search_window() -> int:
        """How many rows a search looks at (highest forget_weight first). Phase 9.1 (A4): it was 300, so once
        there were more rows, old memories whose weight had decayed were never candidates at all."""
        from ai.settings import env_int
        return max(300, env_int("VIOLET_MEMORY_SEARCH_WINDOW", 5000))

    @staticmethod
    def _event_searchable(memory: dict[str, Any], status: str | None, include_disabled: bool) -> bool:
        """The row filter of search_event_memories (before text similarity)."""
        if status is None and memory.get("status") == "deleted":
            return False
        if not include_disabled and not memory.get("use_in_context", 1):
            return False            # the user forbade using this memory
        return True

    def event_link_candidates(self, content: str, min_similarity: float | None = None,
                              exclude_id: int | None = None) -> list[dict[str, Any]]:
        """Existing memories a new statement may belong to (phase 2 + phase 7 B.6.1). Candidates only:
        the caller has to decide (related_to / update_event_progress, or new_event)."""
        from ai.memory_model import shared_topic_terms
        from ai.settings import env_float
        threshold = min_similarity if min_similarity is not None else env_float("VIOLET_MEMORY_LINK_CANDIDATE_SIMILARITY", 0.4)
        found: dict[int, dict[str, Any]] = {}
        for m in self.search_event_memories(content, status=None, limit=3, min_similarity=threshold):
            if m["id"] != exclude_id:
                found[m["id"]] = {**m, "basis": {"rule": "text_similarity", "shared_terms": shared_topic_terms(content, m["content"])}}
        # an event still open on the same topic ("答辩结束了" / "将于10-21参加天文观测课程答辩"): the wording of
        # a reported outcome often differs too much for text_similarity; one distinctive shared word is enough
        # to ask. Independent of the progress the caller claims, so a mislabelled outcome is asked as well.
        for row in self.fetch_all(
                "SELECT * FROM event_memories WHERE status != 'deleted' AND use_in_context = 1 "
                "AND progress NOT IN ('done', 'cancelled', 'na') ORDER BY id"):
            if row["id"] in found or row["id"] == exclude_id:
                continue
            terms = shared_topic_terms(content, row["content"])
            if terms:
                m = self._decode_event_memory_row(row)
                found[m["id"]] = {**m, "similarity": self.text_similarity(content, m["content"]),
                                  "basis": {"rule": "open_event_same_topic", "shared_terms": terms}}
        return [
            {"id": m["id"], "content": m["content"], "kind": m.get("kind"), "progress": m.get("progress"),
             "event_time": m.get("event_time"), "status": m.get("status"),
             "similarity": round(float(m.get("similarity") or 0), 2), "basis": m["basis"]}
            for m in sorted(found.values(), key=lambda m: m["id"])
        ][:5]

    @staticmethod
    def event_candidate_fingerprint(candidate: dict[str, Any]) -> tuple:
        """What a decision about a candidate was based on; any change means it has to be decided again."""
        return (candidate.get("content"), candidate.get("progress"), candidate.get("status"), candidate.get("event_time"))

    def ended_event_contradictions(self) -> list[dict[str, Any]]:
        """Active events reported as ended next to an active, still open event on the same topic
        (e.g. 答辩已结束 [done] and 将于10-21参加答辩 [unknown]): the memory says two things at once."""
        from ai.memory_model import ENDED_PROGRESS
        out = []
        for row in self.fetch_all("SELECT * FROM event_memories WHERE status = 'active' AND progress IN ('done', 'cancelled') "
                                  "ORDER BY id"):
            for c in self.event_link_candidates(row["content"], exclude_id=row["id"]):
                if c["status"] == "active" and c["progress"] not in ENDED_PROGRESS and c["progress"] != "na":
                    out.append({"ended_id": row["id"], "ended": row["content"], "open_id": c["id"], "open": c["content"],
                                "open_progress": c["progress"], "basis": c["basis"]})
        return out

    def has_searchable_event_memories(self, include_disabled: bool = False) -> bool:
        """Phase 7 B.2: could search_event_memories (no status / category filter) return anything at all?
        Same candidate window and the same row filter as the search itself."""
        return any(self._event_searchable(m, None, include_disabled)
                   for m in self.list_event_memories(limit=self.search_window()))

    def update_event_memory(
        self,
        memory_id: int,
        content: str | None = None,
        category: str | None = None,
        happened_at: str | None = None,
        valid_until: str | None = None,
        status: str | None = None,
        importance: float | None = None,
        emotional_weight: float | None = None,
        forget_weight: float | None = None,
        source_message_ids: list[int] | None = None,
    ) -> None:
        current = self.get_event_memory(memory_id)
        if not current:
            raise ValueError(f"Event memory not found: {memory_id}")
        self.execute(
            """
            UPDATE event_memories
            SET content = ?, category = ?, happened_at = ?, valid_until = ?, status = ?,
                importance = ?, emotional_weight = ?, forget_weight = ?, source_message_ids = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                content if content is not None else current["content"],
                category if category is not None else current["category"],
                happened_at if happened_at is not None else current.get("happened_at"),
                valid_until if valid_until is not None else current.get("valid_until"),
                status if status is not None else current["status"],
                float(importance) if importance is not None else current["importance"],
                float(emotional_weight) if emotional_weight is not None else current["emotional_weight"],
                float(forget_weight) if forget_weight is not None else current["forget_weight"],
                self.to_json(source_message_ids) if source_message_ids is not None else self.to_json(current.get("source_message_ids", [])),
                self.now(),
                memory_id,
            ),
        )

    def reinforce_event_memory(
        self,
        memory_id: int,
        importance_delta: float = 0.3,
        emotional_delta: float = 0.1,
        forget_delta: float = 0.8,
        content: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_event_memory(memory_id)
        if not current:
            raise ValueError(f"Event memory not found: {memory_id}")
        self.execute(
            """
            UPDATE event_memories
            SET content = ?,
                importance = importance + ?,
                emotional_weight = emotional_weight + ?,
                forget_weight = forget_weight + ?,
                mention_count = mention_count + 1,
                recall_count = recall_count + 1,
                last_recalled_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                content or current["content"],
                float(importance_delta),
                float(emotional_delta),
                float(forget_delta),
                self.now(),
                self.now(),
                memory_id,
            ),
        )
        return self.get_event_memory(memory_id)

    # ------------------------------------------------------------------
    # Saving / updating event memories (phase 2)
    #
    # Character overlap NEVER merges or overwrites memories. It only produces
    # candidates; the caller must then say explicitly whether the new
    # information belongs to one of them (related_to=<id>) or is a new event
    # (new_event=True).
    # ------------------------------------------------------------------

    def save_event_memory(
        self,
        content: str,
        category: str = "general",
        kind: str = "event",
        progress: str | None = None,
        happened_at: str | None = None,
        event_time: str | None = None,
        valid_until: str | None = None,
        importance: float = 1.0,
        emotional_weight: float = 1.0,
        event_key: str | None = None,
        related_to: int | None = None,
        new_event: bool = False,
        source_turn_id: str | None = None,
        confidence: float = 1.0,
        candidate_min_similarity: float | None = None,
        decided: dict[int, tuple] | None = None,
        source_message_id: int | None = None,
        preserve_details: str | None = None,
    ) -> dict[str, Any]:
        """preserve_details (phase 9.1.1): the user's words of this turn, when a model wrote it -- an update of an
        existing memory then keeps the dates / clock times the old text had (see apply_event_update)."""
        from ai.memory_model import ENDED_PROGRESS, default_valid_until, event_deadline, parse_time

        content = (content or "").strip()
        if not content:
            raise ValueError("content is empty")
        norm = self.normalize_text(content)

        # 1) idempotent retries of the same background job
        if source_turn_id:
            for row in self.fetch_all(
                "SELECT * FROM event_memories WHERE source_turn_id = ? AND status != 'deleted'", (source_turn_id,)
            ):
                if self.normalize_text(row["content"]) == norm:
                    return {"action": "already_saved", "memory": self._decode_event_memory_row(row), "candidates": []}

        # 2) explicit link to an existing memory
        if related_to is not None:
            memory = self.apply_event_update(
                int(related_to), content=content, progress=progress, event_time=event_time,
                valid_until=valid_until, importance=importance, source="user", source_turn_id=source_turn_id,
                reason="linked by memory agent", record_user_mention=True, source_message_id=source_message_id,
                preserve_details=preserve_details,
            )
            return {"action": "updated", "memory": memory, "candidates": []}

        # 3) the same event_key means the same (still open) event
        if event_key:
            same = self.fetch_one(
                """
                SELECT * FROM event_memories
                WHERE event_key = ? AND status != 'deleted' AND progress NOT IN ('done', 'cancelled')
                ORDER BY id DESC LIMIT 1
                """,
                (event_key,),
            )
            if same:
                memory = self.apply_event_update(
                    same["id"], content=content, progress=progress, event_time=event_time, valid_until=valid_until,
                    importance=importance, source="user", source_turn_id=source_turn_id,
                    reason=f"same event_key {event_key}", record_user_mention=True, source_message_id=source_message_id,
                    preserve_details=preserve_details,
                )
                return {"action": "updated_by_key", "memory": memory, "candidates": []}

        # 4) exact repetition: the user mentioned the same thing again
        for row in self.fetch_all("SELECT * FROM event_memories WHERE status != 'deleted'"):
            if self.normalize_text(row["content"]) == norm:
                memory = self.record_user_mention(row["id"], importance=importance)
                return {"action": "reinforced", "memory": memory, "candidates": []}

        # 5) similar memories exist: ask for an explicit decision instead of guessing.
        #    decided (phase 7 B.6.1): the candidates the deciding model was shown in its current run, as
        #    {id: event_candidate_fingerprint}. With it, new_event=True only passes candidates in it that have
        #    not changed since; anything new or changed is asked again. decided=None keeps the plain rule for
        #    code that decided by itself (no model involved).
        if not new_event or decided is not None:
            candidates = self.event_link_candidates(content, candidate_min_similarity)
            if new_event:
                undecided = [c["id"] for c in candidates
                             if decided.get(c["id"]) != self.event_candidate_fingerprint(c)]
                if undecided:
                    return {"action": "needs_decision", "memory": None, "candidates": candidates,
                            "undecided_ids": undecided}
            elif candidates:
                return {"action": "needs_decision", "memory": None, "candidates": candidates,
                        "undecided_ids": [c["id"] for c in candidates]}

        # 6) new memory
        if progress is None:
            progress = "na" if kind in ("fact", "background") else "unknown"
        if valid_until is None:
            valid_until = default_valid_until(kind, progress, event_time)
        if progress == "planned" and event_time and event_deadline(event_time) and event_deadline(event_time) < parse_time(self.now()):
            progress = "unknown"
        memory_id = self.add_event_memory(
            content=content, category=category, happened_at=happened_at, valid_until=valid_until,
            importance=importance, emotional_weight=emotional_weight, kind=kind, progress=progress,
            event_time=event_time, event_key=event_key, source_turn_id=source_turn_id, confidence=confidence,
            source_message_ids=[int(source_message_id)] if source_message_id is not None else None,
        )
        if progress in ENDED_PROGRESS:
            self.execute("UPDATE event_memories SET last_user_mention_at = ? WHERE id = ?", (self.now(), memory_id))
        return {"action": "created", "memory": self.get_event_memory(memory_id), "candidates": []}

    def save_event_memory_dedup(
        self,
        content: str,
        category: str = "general",
        happened_at: str | None = None,
        valid_until: str | None = None,
        importance: float = 1.0,
        emotional_weight: float = 1.0,
        source_message_ids: list[int] | None = None,
        duplicate_threshold: float = 0.72,
    ) -> dict[str, Any]:
        """Backward-compatible wrapper. No longer merges by similarity (see save_event_memory)."""
        result = self.save_event_memory(
            content=content, category=category, happened_at=happened_at, valid_until=valid_until,
            importance=importance, emotional_weight=emotional_weight,
        )
        result.setdefault("matched_similarity", None)
        return result

    def apply_event_update(
        self,
        memory_id: int,
        content: str | None = None,
        progress: str | None = None,
        event_time: str | None = None,
        valid_until: str | None = None,
        importance: float | None = None,
        source: str = "user",
        source_turn_id: str | None = None,
        reason: str | None = None,
        record_user_mention: bool = False,
        action: str = "update",
        source_message_id: int | None = None,
        preserve_details: str | None = None,
    ) -> dict[str, Any]:
        """Change content / progress / time of one memory, keeping a revision record.

        source_message_id (phase 9.1): the user message this change is based on, appended to source_message_ids.
        Phase 9.1.1 (D-P91-1): a date-only event_time never replaces a known clock time of the same day. With
        preserve_details (the user's words of a model-written change) a new text that drops a date / clock time of the
        old one gets it back ("（原记录：下午15:00）"), and a text whose date / clock contradicts the stored event_time
        is refused (EventTimeMismatch) -- dashboard edits pass None and are taken as written."""
        from ai.memory_model import (ENDED_PROGRESS, EVENT_PROGRESS, EventTimeMismatch, default_valid_until,
                                     dropped_time_details, precise_time_kept, time_mismatch)

        current = self.get_event_memory(memory_id)
        if not current or current.get("status") == "deleted":
            raise ValueError(f"Event memory not found: {memory_id}")
        if progress is not None and progress not in EVENT_PROGRESS:
            raise ValueError(f"Invalid event progress: {progress}")
        now = self.now()
        new_content = content.strip() if content else current["content"]
        event_time = precise_time_kept(current.get("event_time"), event_time)
        kept_details: list[str] = []
        if preserve_details is not None and content:
            kept_details = dropped_time_details(current["content"], new_content, preserve_details, event_time)
            if kept_details:
                new_content = f"{new_content}（原记录：{'、'.join(kept_details)}）"
                reason = f"{reason or ''}；保留原有细节：{'、'.join(kept_details)}".lstrip("；")
            problem = time_mismatch(new_content, event_time or current.get("event_time"))
            if problem:
                raise EventTimeMismatch(problem)
        new_progress = progress or current.get("progress")
        new_kind = current.get("kind")
        if progress and progress not in ("na",) and new_kind in ("legacy", "background"):
            new_kind = "event"
        new_valid = valid_until or current.get("valid_until")
        if progress in ENDED_PROGRESS and progress != current.get("progress"):
            new_valid = valid_until or default_valid_until("event", progress, event_time or current.get("event_time"))
        new_importance = current["importance"]
        if importance is not None:
            new_importance = max(float(current["importance"]), float(importance))
        sources = [int(i) for i in current.get("source_message_ids") or [] if isinstance(i, int)]
        if source_message_id is not None and int(source_message_id) not in sources:
            sources = (sources + [int(source_message_id)])[-20:]
        self.execute(
            """
            UPDATE event_memories
            SET content = ?, progress = ?, kind = ?, event_time = COALESCE(?, event_time), valid_until = ?,
                source_message_ids = ?,
                importance = ?, status = CASE WHEN status = 'deleted' THEN status ELSE 'active' END,
                progress_updated_at = CASE WHEN ? != progress THEN ? ELSE progress_updated_at END,
                last_user_mention_at = CASE WHEN ? THEN ? ELSE last_user_mention_at END,
                mention_count = mention_count + CASE WHEN ? THEN 1 ELSE 0 END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                new_content, new_progress, new_kind, event_time, new_valid, self.to_json(sources), new_importance,
                new_progress, now,
                1 if record_user_mention else 0, now,
                1 if record_user_mention else 0,
                now, memory_id,
            ),
        )
        if event_time and event_time != current.get("event_time"):
            # phase 9.1.1: the revision table has no time column; the change is kept in the reason, not lost
            reason = f"{reason or ''}；时间 {current.get('event_time') or '无'} → {event_time}".lstrip("；")
        self.add_memory_revision(
            "event_memories", memory_id, action,
            old_content=current["content"], new_content=new_content,
            old_progress=current.get("progress"), new_progress=new_progress,
            reason=reason, source=source, source_turn_id=source_turn_id,
        )
        memory = self.get_event_memory(memory_id)
        if kept_details:
            memory["details_kept"] = kept_details
        return memory

    def update_event_progress(
        self,
        memory_id: int,
        progress: str,
        content: str | None = None,
        reason: str | None = None,
        source: str = "user",
        source_turn_id: str | None = None,
        source_message_id: int | None = None,
        preserve_details: str | None = None,
    ) -> dict[str, Any]:
        return self.apply_event_update(
            memory_id, content=content, progress=progress, reason=reason, source=source,
            source_turn_id=source_turn_id, record_user_mention=(source == "user"), action="progress",
            source_message_id=source_message_id, preserve_details=preserve_details,
        )

    def correct_event_memory(
        self,
        memory_id: int,
        content: str,
        reason: str | None = None,
        progress: str | None = None,
        source_turn_id: str | None = None,
        source: str = "user_correction",
        source_message_id: int | None = None,
    ) -> dict[str, Any]:
        """The user said Violet remembered something wrong: replace it, keep the old text in revisions.

        source="user_confirmed" (phase 9.1): the correction quotes the user's own words, so the memory now counts as
        confirmed by the user (see confirmed_by_user)."""
        memory = self.apply_event_update(
            memory_id, content=content, progress=progress, reason=reason, source=source,
            source_turn_id=source_turn_id, record_user_mention=True, action="correct",
            source_message_id=source_message_id,
        )
        self.execute("UPDATE event_memories SET confidence = 1.0 WHERE id = ?", (memory_id,))
        return self.get_event_memory(memory_id)

    def retract_event_memory(self, memory_id: int, reason: str | None = None, source_turn_id: str | None = None,
                             source: str = "user_correction") -> dict[str, Any]:
        current = self.get_event_memory(memory_id)
        if not current:
            raise ValueError(f"Event memory not found: {memory_id}")
        self.execute("UPDATE event_memories SET status = 'deleted', updated_at = ? WHERE id = ?", (self.now(), memory_id))
        self.add_memory_revision(
            "event_memories", memory_id, "retract", old_content=current["content"], new_content=None,
            old_progress=current.get("progress"), new_progress=current.get("progress"),
            reason=reason, source=source, source_turn_id=source_turn_id,
        )
        return self.get_event_memory(memory_id)

    def record_user_mention(self, memory_id: int, importance: float | None = None) -> dict[str, Any]:
        """The user brought this memory up again. Importance only changes with explicit new information."""
        current = self.get_event_memory(memory_id)
        if not current:
            raise ValueError(f"Event memory not found: {memory_id}")
        new_importance = current["importance"]
        if importance is not None and float(importance) > float(current["importance"]):
            new_importance = float(importance)
        new_forget = max(float(current["forget_weight"]), new_importance + float(current["emotional_weight"]) * 0.5)
        now = self.now()
        self.execute(
            """
            UPDATE event_memories
            SET mention_count = mention_count + 1, last_user_mention_at = ?, importance = ?,
                forget_weight = ?, status = CASE WHEN status = 'deleted' THEN status ELSE 'active' END,
                updated_at = ?
            WHERE id = ?
            """,
            (now, new_importance, new_forget, now, memory_id),
        )
        return self.get_event_memory(memory_id)

    def mark_event_memory_recalled(self, memory_id: int, weight_bonus: float = 0.0) -> None:
        """Bookkeeping only: being retrieved does not make a memory more important.

        weight_bonus is kept for backward compatibility and defaults to 0.
        """
        self.execute(
            """
            UPDATE event_memories
            SET recall_count = recall_count + 1,
                forget_weight = forget_weight + ?,
                last_recalled_at = ?
            WHERE id = ?
            """,
            (float(weight_bonus), self.now(), memory_id),
        )

    def refresh_event_states(self) -> dict[str, int]:
        """Apply time: planned events whose time passed become 'unknown' (never 'done');
        active memories past valid_until become 'expired'."""
        now = self.now()
        rows = self.fetch_all(
            "SELECT id, event_time, valid_until, progress, status FROM event_memories WHERE status != 'deleted'"
        )
        from ai.memory_model import event_deadline, parse_time
        now_dt = parse_time(now)
        unknown_ids, expired_ids = [], []
        for row in rows:
            event_time = event_deadline(row.get("event_time"))     # a date-only time passes at the end of that day
            if row["progress"] == "planned" and event_time and event_time < now_dt:
                unknown_ids.append(row["id"])
            valid_until = parse_time(row.get("valid_until"))
            if row["status"] == "active" and valid_until and valid_until < now_dt:
                expired_ids.append(row["id"])
        for memory_id in unknown_ids:
            self.execute(
                "UPDATE event_memories SET progress = 'unknown', progress_updated_at = ? WHERE id = ? AND progress = 'planned'",
                (now, memory_id),
            )
            self.add_memory_revision("event_memories", memory_id, "progress", old_progress="planned",
                                     new_progress="unknown", reason="event time passed without news", source="system")
        for memory_id in expired_ids:
            self.execute("UPDATE event_memories SET status = 'expired' WHERE id = ? AND status = 'active'", (memory_id,))
        return {"progress_unknown": len(unknown_ids), "expired_by_validity": len(expired_ids)}

    def decay_event_memories(
        self,
        decay: float = 0.05,
        expire_below: float = 0.8,
        archive_below: float = 0.35,
        forget_below: float = 0.12,
        delete_below: float = 0.03,
        event_category_decay: float = 0.12,
    ) -> dict[str, int]:
        now = self.now()
        refreshed = self.refresh_event_states()
        # category='event' 的记忆遗忘更快；长期事实（kind='fact'）不随时间衰减
        self.execute(
            """
            UPDATE event_memories
            SET forget_weight = MAX(0, forget_weight - ?), updated_at = ?
            WHERE status IN ('active', 'expired', 'archived', 'forgotten')
              AND category = 'event' AND kind != 'fact'
            """,
            (float(event_category_decay), now),
        )
        self.execute(
            """
            UPDATE event_memories
            SET forget_weight = MAX(0, forget_weight - ?), updated_at = ?
            WHERE status IN ('active', 'expired', 'archived', 'forgotten')
              AND category != 'event' AND kind != 'fact'
            """,
            (float(decay), now),
        )
        expired = self.execute(
            """
            UPDATE event_memories
            SET status = 'expired', updated_at = ?
            WHERE status = 'active' AND forget_weight < ?
            """,
            (now, float(expire_below)),
        ).rowcount
        archived = self.execute(
            """
            UPDATE event_memories
            SET status = 'archived', updated_at = ?
            WHERE status = 'expired' AND forget_weight < ?
            """,
            (now, float(archive_below)),
        ).rowcount
        # Phase 9.1: an important or emotionally strong memory (importance or emotional_weight >= the threshold) stays
        # 'archived' (很久以前) at worst -- a shared experience is not shown as 印象很模糊 after five weeks.
        from ai.settings import env_float
        keep_clear = env_float("VIOLET_MEMORY_KEEP_CLEAR_WEIGHT", 3.0)
        forgotten = self.execute(
            """
            UPDATE event_memories
            SET status = 'forgotten', updated_at = ?
            WHERE status = 'archived' AND forget_weight < ? AND importance < ? AND emotional_weight < ?
            """,
            (now, float(forget_below), keep_clear, keep_clear),
        ).rowcount
        # Phase 9.1 (A1): time never deletes. 'forgotten' is the floor: still searchable, labelled 印象很模糊.
        # 'deleted' is reserved for a user / dashboard retraction, which always leaves a revision row; it used to
        # be reached here after ~2-4 weeks, indistinguishable from a deletion by the user. delete_below is
        # accepted for compatibility and ignored.
        return {"expired": expired + refreshed["expired_by_validity"], "archived": archived, "forgotten": forgotten,
                "deleted": 0, "progress_unknown": refreshed["progress_unknown"]}

    def add_proactive_event(
        self,
        event_type: str,
        status: str = "planned",
        action_type: str = "none",
        reason: str | None = None,
        decision: dict[str, Any] | None = None,
        task_id: int | None = None,
        sent_messages: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        scheduled_at: str | None = None,
    ) -> int:
        now = self.now()
        cursor = self.execute(
            """
            INSERT INTO proactive_events
            (event_type, status, action_type, reason, decision, task_id, sent_messages, metadata, scheduled_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                status,
                action_type,
                reason,
                self.to_json(decision),
                task_id,
                self.to_json(sent_messages or []),
                self.to_json(metadata or {}),
                scheduled_at,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def update_proactive_event(
        self,
        event_id: int,
        status: str | None = None,
        action_type: str | None = None,
        reason: str | None = None,
        decision: dict[str, Any] | None = None,
        task_id: int | None = None,
        sent_messages: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        current = self.get_proactive_event(event_id)
        if not current:
            raise ValueError(f"Proactive event not found: {event_id}")
        self.execute(
            """
            UPDATE proactive_events
            SET status = ?, action_type = ?, reason = ?, decision = ?, task_id = ?,
                sent_messages = ?, metadata = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status or current["status"],
                action_type or current["action_type"],
                reason if reason is not None else current.get("reason"),
                self.to_json(decision) if decision is not None else self.to_json(current.get("decision")),
                task_id if task_id is not None else current.get("task_id"),
                self.to_json(sent_messages) if sent_messages is not None else self.to_json(current.get("sent_messages", [])),
                self.to_json(metadata) if metadata is not None else self.to_json(current.get("metadata", {})),
                self.now(),
                event_id,
            ),
        )
        return self.get_proactive_event(event_id)

    def _decode_proactive_event_row(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        row["decision"] = self.from_json(row.get("decision"), default=None)
        row["sent_messages"] = self.from_json(row.get("sent_messages"), default=[])
        row["metadata"] = self.from_json(row.get("metadata"), default={})
        return row

    def get_proactive_event(self, event_id: int) -> dict[str, Any] | None:
        row = self.fetch_one("SELECT * FROM proactive_events WHERE id = ?", (event_id,))
        return self._decode_proactive_event_row(row)

    def list_proactive_events(
        self,
        event_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.fetch_all(
            f"""
            SELECT * FROM proactive_events
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        return [self._decode_proactive_event_row(row) for row in rows]

    def set_proactive_setting(self, key: str, value: Any) -> None:
        now = self.now()
        self.execute(
            """
            INSERT INTO proactive_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, self.to_json(value), now),
        )

    def get_proactive_setting(self, key: str, default: Any = None) -> Any:
        row = self.fetch_one("SELECT value FROM proactive_settings WHERE key = ?", (key,))
        if not row:
            return default
        return self.from_json(row.get("value"), default=default)

    def get_latest_message_at(self, role: str | None = None) -> str | None:
        if role:
            row = self.fetch_one(
                "SELECT created_at FROM messages WHERE role = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (role,),
            )
        else:
            row = self.fetch_one("SELECT created_at FROM messages ORDER BY created_at DESC, id DESC LIMIT 1")
        return row.get("created_at") if row else None

    def count_proactive_events_since(
        self,
        start_at: str,
        status: str | None = None,
        action_type: str | None = None,
        task_name: str | None = None,
        exclude_event_types: Iterable[str] | None = None,
    ) -> int:
        clauses = ["created_at >= ?"]
        params: list[Any] = [start_at]
        for event_type in exclude_event_types or ():
            clauses.append("event_type != ?")
            params.append(event_type)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if action_type:
            clauses.append("action_type = ?")
            params.append(action_type)
        if task_name:
            clauses.append("json_extract(decision, '$.task.name') = ?")
            params.append(task_name)
        row = self.fetch_one(
            f"SELECT COUNT(*) AS count FROM proactive_events WHERE {' AND '.join(clauses)}",
            params,
        )
        return int(row.get("count", 0)) if row else 0

    def get_latest_proactive_event_at(self, status: str | None = None) -> str | None:
        if status:
            row = self.fetch_one(
                "SELECT updated_at FROM proactive_events WHERE status = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
                (status,),
            )
        else:
            row = self.fetch_one(
                "SELECT updated_at FROM proactive_events ORDER BY updated_at DESC, id DESC LIMIT 1"
            )
        return row.get("updated_at") if row else None


    # ------------------------------------------------------------------
    # Inbox (inbound_messages)
    # ------------------------------------------------------------------

    def add_inbound_message(
        self,
        dedup_key: str,
        wx_message_id: str | None,
        item_index: int,
        item_type: int | None,
        content: str | None,
        status: str = "received",
        received_at: str | None = None,
        channel: str | None = None,
        peer_id: str | None = None,
    ) -> int | None:
        """Insert an inbox row; returns its id, or None if dedup_key was already seen."""
        if status not in self.INBOUND_STATUSES:
            raise ValueError(f"Invalid inbound status: {status}")
        now = self.now()
        cursor = self.execute(
            """
            INSERT OR IGNORE INTO inbound_messages
            (dedup_key, wx_message_id, item_index, item_type, content, status, received_at, updated_at, channel, peer_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (dedup_key, wx_message_id, item_index, item_type, content, status, received_at or now, now, channel,
             peer_id),
        )
        if cursor.rowcount == 0:
            return None
        return int(cursor.lastrowid)

    def update_inbound_messages(
        self,
        ids: Iterable[int],
        status: str | None = None,
        turn_id: str | None = None,
        error: str | None = None,
        stored_message_id: int | None = None,
        content: str | None = None,
        increment_attempts: bool = False,
    ) -> None:
        ids = [int(i) for i in ids]
        if not ids:
            return
        if status is not None and status not in self.INBOUND_STATUSES:
            raise ValueError(f"Invalid inbound status: {status}")
        sets = ["updated_at = ?"]
        params: list[Any] = [self.now()]
        for column, value in (
            ("status", status),
            ("turn_id", turn_id),
            ("error", error),
            ("stored_message_id", stored_message_id),
            ("content", content),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        if increment_attempts:
            sets.append("attempts = attempts + 1")
        placeholders = ", ".join("?" for _ in ids)
        self.execute(
            f"UPDATE inbound_messages SET {', '.join(sets)} WHERE id IN ({placeholders})",
            (*params, *ids),
        )

    def get_inbound_message(self, inbound_id: int) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM inbound_messages WHERE id = ?", (inbound_id,))

    def list_inbound_messages(self, statuses: Iterable[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        statuses = list(statuses or [])
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            return self.fetch_all(
                f"""
                SELECT * FROM inbound_messages
                WHERE status IN ({placeholders})
                ORDER BY received_at ASC, id ASC
                LIMIT ?
                """,
                (*statuses, limit),
            )
        return self.fetch_all(
            "SELECT * FROM inbound_messages ORDER BY received_at DESC, id DESC LIMIT ?",
            (limit,),
        )

    def count_inbound_by_status(self) -> dict[str, int]:
        rows = self.fetch_all("SELECT status, COUNT(*) AS count FROM inbound_messages GROUP BY status")
        return {row["status"]: int(row["count"]) for row in rows}

    # ------------------------------------------------------------------
    # Outbox (outbound_messages)
    # ------------------------------------------------------------------

    def add_outbound_message(
        self,
        client_id: str,
        origin: str,
        message_type: str,
        content: str | None,
        turn_id: str | None = None,
        proactive_event_id: int | None = None,
        status: str = "queued",
        channel: str | None = None,
        peer_id: str | None = None,
    ) -> int:
        if status not in self.OUTBOUND_STATUSES:
            raise ValueError(f"Invalid outbound status: {status}")
        now = self.now()
        cursor = self.execute(
            """
            INSERT INTO outbound_messages
            (client_id, origin, turn_id, proactive_event_id, message_type, content, status, created_at, updated_at,
             channel, peer_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (client_id, origin, turn_id, proactive_event_id, message_type, content, status, now, now, channel, peer_id),
        )
        return int(cursor.lastrowid)

    def update_outbound_message(
        self,
        outbound_id: int,
        status: str | None = None,
        error: str | None = None,
        message_id: int | None = None,
        increment_attempts: bool = False,
    ) -> None:
        if status is not None and status not in self.OUTBOUND_STATUSES:
            raise ValueError(f"Invalid outbound status: {status}")
        now = self.now()
        sets = ["updated_at = ?"]
        params: list[Any] = [now]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
            if status == "sent":
                sets.append("sent_at = ?")
                params.append(now)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if message_id is not None:
            sets.append("message_id = ?")
            params.append(message_id)
        if increment_attempts:
            sets.append("attempts = attempts + 1")
        self.execute(
            f"UPDATE outbound_messages SET {', '.join(sets)} WHERE id = ?",
            (*params, outbound_id),
        )

    def get_outbound_message(self, outbound_id: int) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM outbound_messages WHERE id = ?", (outbound_id,))

    def list_outbound_messages(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status:
            return self.fetch_all(
                "SELECT * FROM outbound_messages WHERE status = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (status, limit),
            )
        return self.fetch_all(
            "SELECT * FROM outbound_messages ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )

    def count_outbound_by_status(self) -> dict[str, int]:
        rows = self.fetch_all("SELECT status, COUNT(*) AS count FROM outbound_messages GROUP BY status")
        return {row["status"]: int(row["count"]) for row in rows}


    # ------------------------------------------------------------------
    # Phase 2: memory queries used by context building
    # ------------------------------------------------------------------

    def set_event_memory_enabled(self, memory_id: int, enabled: bool) -> dict[str, Any] | None:
        self.execute("UPDATE event_memories SET use_in_context = ?, updated_at = ? WHERE id = ?",
                     (1 if enabled else 0, self.now(), memory_id))
        return self.get_event_memory(memory_id)

    def list_event_memories_by_ids(self, ids: Iterable[int]) -> list[dict[str, Any]]:
        ids = [int(i) for i in ids]
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        rows = self.fetch_all(f"SELECT * FROM event_memories WHERE id IN ({placeholders})", ids)
        return [self._decode_event_memory_row(row) for row in rows]

    def list_recent_event_memories(
        self,
        kinds: Iterable[str] | None = None,
        include_ended: bool = True,
        limit: int = 20,
        include_disabled: bool = False,
    ) -> list[dict[str, Any]]:
        """Most recently created/changed non-deleted memories (independent of decay weight)."""
        clauses = ["status != 'deleted'"]
        if not include_disabled:
            clauses.append("use_in_context = 1")
        params: list[Any] = []
        kinds = list(kinds or [])
        if kinds:
            clauses.append(f"kind IN ({', '.join('?' for _ in kinds)})")
            params.extend(kinds)
        if not include_ended:
            clauses.append("progress NOT IN ('done', 'cancelled')")
        rows = self.fetch_all(
            f"""
            SELECT * FROM event_memories WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(progress_updated_at, created_at) DESC, id DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        return [self._decode_event_memory_row(row) for row in rows]

    def add_memory_revision(
        self,
        memory_table: str,
        memory_id: int,
        action: str,
        old_content: str | None = None,
        new_content: str | None = None,
        old_progress: str | None = None,
        new_progress: str | None = None,
        reason: str | None = None,
        source: str | None = None,
        source_turn_id: str | None = None,
    ) -> int:
        cursor = self.execute(
            """
            INSERT INTO memory_revisions
            (memory_table, memory_id, action, old_content, new_content, old_progress, new_progress,
             reason, source, source_turn_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (memory_table, memory_id, action, old_content, new_content, old_progress, new_progress,
             reason, source, source_turn_id, self.now()),
        )
        return int(cursor.lastrowid)

    # Phase 9.1 (A7): revision sources that mean the user confirmed a memory's content -- a dashboard correction, or
    # a chat correction backed by the user's own words of that turn. No new column: older rows corrected in the
    # dashboard (phase 9.0) count as confirmed too.
    CONFIRMED_REVISION_SOURCES = ("dashboard", "user_confirmed")

    def confirmed_by_user(self, memory_table: str, memory_id: int) -> bool:
        row = self.fetch_one(
            f"SELECT 1 FROM memory_revisions WHERE memory_table = ? AND memory_id = ? AND action != 'retract' "
            f"AND source IN ({', '.join('?' for _ in self.CONFIRMED_REVISION_SOURCES)}) LIMIT 1",
            (memory_table, int(memory_id), *self.CONFIRMED_REVISION_SOURCES))
        return row is not None

    def list_memory_revisions(self, memory_table: str, memory_id: int) -> list[dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM memory_revisions WHERE memory_table = ? AND memory_id = ? ORDER BY id",
            (memory_table, memory_id),
        )

    # ------------------------------------------------------------------
    # Phase 2: commitments (explicit reminders / follow-ups)
    # ------------------------------------------------------------------

    def add_commitment(
        self,
        content: str,
        due_at: str,
        kind: str = "reminder",
        evidence: str | None = None,
        source_turn_id: str | None = None,
        created_by: str = "user",
    ) -> dict[str, Any]:
        """Create a pending commitment; idempotent per (source_turn_id, content)."""
        content = (content or "").strip()
        if source_turn_id:
            for row in self.fetch_all("SELECT * FROM commitments WHERE source_turn_id = ?", (source_turn_id,)):
                if self.normalize_text(row["content"]) == self.normalize_text(content):
                    return {"action": "already_saved", "commitment": row}
        duplicate = self.fetch_one(
            "SELECT * FROM commitments WHERE status IN ('pending', 'sending') AND content = ? AND due_at = ?",
            (content, due_at),
        )
        if duplicate:
            return {"action": "duplicate", "commitment": duplicate}
        now = self.now()
        cursor = self.execute(
            """
            INSERT INTO commitments
            (kind, content, due_at, status, evidence, source_turn_id, created_by, next_attempt_at, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
            """,
            (kind, content, due_at, evidence, source_turn_id, created_by, due_at, now, now),
        )
        return {"action": "created", "commitment": self.get_commitment(int(cursor.lastrowid))}

    def get_commitment(self, commitment_id: int) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM commitments WHERE id = ?", (commitment_id,))

    def list_commitments(self, statuses: Iterable[str] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        statuses = list(statuses or [])
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            return self.fetch_all(
                f"SELECT * FROM commitments WHERE status IN ({placeholders}) ORDER BY due_at ASC, id ASC LIMIT ?",
                (*statuses, limit),
            )
        return self.fetch_all("SELECT * FROM commitments ORDER BY due_at DESC, id DESC LIMIT ?", (limit,))

    def list_due_commitments(self, now: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        from ai.memory_model import parse_time
        now_dt = parse_time(now or self.now())
        due = []
        for row in self.list_commitments(["pending"], limit=200):
            due_at = parse_time(row["due_at"])
            next_at = parse_time(row.get("next_attempt_at")) or due_at
            if due_at and due_at <= now_dt and (next_at is None or next_at <= now_dt):
                due.append(row)
        return due[:limit]

    def claim_commitment(self, commitment_id: int) -> bool:
        """pending -> sending, atomically. Returns False if someone else claimed it."""
        cursor = self.execute(
            "UPDATE commitments SET status = 'sending', attempts = attempts + 1, updated_at = ? WHERE id = ? AND status = 'pending'",
            (self.now(), commitment_id),
        )
        return cursor.rowcount == 1

    def update_commitment(self, commitment_id: int, **fields: Any) -> dict[str, Any] | None:
        allowed = {"status", "next_attempt_at", "last_error", "proactive_event_id", "delivered_at", "due_at", "content"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown commitment fields: {unknown}")
        if "status" in fields and fields["status"] not in self.COMMITMENT_STATUSES:
            raise ValueError(f"Invalid commitment status: {fields['status']}")
        if not fields:
            return self.get_commitment(commitment_id)
        sets = [f"{k} = ?" for k in fields] + ["updated_at = ?"]
        self.execute(
            f"UPDATE commitments SET {', '.join(sets)} WHERE id = ?",
            (*fields.values(), self.now(), commitment_id),
        )
        return self.get_commitment(commitment_id)

    # ------------------------------------------------------------------
    # Phase 2: topic mentions (Violet-initiated topics and user engagement)
    # ------------------------------------------------------------------

    def add_topic_mention(
        self,
        topic_key: str,
        source: str,
        memory_id: int | None = None,
        proactive_event_id: int | None = None,
        user_engaged: int | None = None,
        created_at: str | None = None,
    ) -> int:
        cursor = self.execute(
            """
            INSERT INTO topic_mentions (topic_key, memory_id, source, proactive_event_id, user_engaged, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (topic_key, memory_id, source, proactive_event_id, user_engaged, created_at or self.now()),
        )
        return int(cursor.lastrowid)

    def list_topic_mentions(
        self,
        topic_key: str | None = None,
        memory_id: int | None = None,
        sources: Iterable[str] | None = None,
        since: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if topic_key is not None:
            clauses.append("topic_key = ?")
            params.append(topic_key)
        if memory_id is not None:
            clauses.append("memory_id = ?")
            params.append(memory_id)
        sources = list(sources or [])
        if sources:
            clauses.append(f"source IN ({', '.join('?' for _ in sources)})")
            params.extend(sources)
        if since:
            clauses.append("created_at > ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.fetch_all(
            f"SELECT * FROM topic_mentions {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, limit),
        )

    def set_topic_engagement(self, mention_ids: Iterable[int], engaged: bool) -> None:
        ids = [int(i) for i in mention_ids]
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        self.execute(
            f"UPDATE topic_mentions SET user_engaged = ?, evaluated_at = ? WHERE id IN ({placeholders})",
            (1 if engaged else 0, self.now(), *ids),
        )

    # ------------------------------------------------------------------
    # Phase 2: background memory jobs
    # ------------------------------------------------------------------

    def add_memory_job(self, turn_id: str, user_message: str, user_message_id: int | None,
                       status: str = "waiting_reply") -> int | None:
        if status not in self.MEMORY_JOB_STATUSES:
            raise ValueError(f"Invalid memory job status: {status}")
        now = self.now()
        cursor = self.execute(
            """
            INSERT OR IGNORE INTO memory_jobs (turn_id, user_message, user_message_id, status, next_attempt_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (turn_id, user_message, user_message_id, status, now, now, now),
        )
        return int(cursor.lastrowid) if cursor.rowcount else None

    def get_memory_job(self, job_id: int) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM memory_jobs WHERE id = ?", (job_id,))

    def get_memory_job_by_turn(self, turn_id: str) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM memory_jobs WHERE turn_id = ?", (turn_id,))

    def update_memory_job(self, job_id: int, **fields: Any) -> None:
        allowed = {"status", "assistant_reply", "attempts", "next_attempt_at", "last_error", "finished_at", "progress"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown memory job fields: {unknown}")
        if "status" in fields and fields["status"] not in self.MEMORY_JOB_STATUSES:
            raise ValueError(f"Invalid memory job status: {fields['status']}")
        sets = [f"{k} = ?" for k in fields] + ["updated_at = ?"]
        self.execute(f"UPDATE memory_jobs SET {', '.join(sets)} WHERE id = ?", (*fields.values(), self.now(), job_id))

    def claim_next_memory_job(self) -> dict[str, Any] | None:
        """Atomically move the oldest runnable pending job to 'running'."""
        from ai.memory_model import parse_time
        now = self.now()
        now_dt = parse_time(now)
        with self._lock:
            rows = self.fetch_all("SELECT * FROM memory_jobs WHERE status = 'pending' ORDER BY id ASC LIMIT 50")
            for row in rows:
                next_at = parse_time(row.get("next_attempt_at"))
                if next_at and next_at > now_dt:
                    continue
                cursor = self.execute(
                    "UPDATE memory_jobs SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE id = ? AND status = 'pending'",
                    (now, row["id"]),
                )
                if cursor.rowcount == 1:
                    return self.get_memory_job(row["id"])
        return None

    def list_memory_jobs(self, statuses: Iterable[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        statuses = list(statuses or [])
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            return self.fetch_all(f"SELECT * FROM memory_jobs WHERE status IN ({placeholders}) ORDER BY id LIMIT ?", (*statuses, limit))
        return self.fetch_all("SELECT * FROM memory_jobs ORDER BY id DESC LIMIT ?", (limit,))

    def count_memory_jobs_by_status(self) -> dict[str, int]:
        rows = self.fetch_all("SELECT status, COUNT(*) AS count FROM memory_jobs GROUP BY status")
        return {row["status"]: int(row["count"]) for row in rows}
