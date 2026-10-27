"""Storage layer: isolation, legacy compatibility, inbox/outbox, thread safety."""

import os
import sqlite3
import threading
from pathlib import Path

from database import VioletDatabase


def test_tests_use_isolated_database(violetdb):
    assert Path(violetdb.db_path) == Path(os.environ["VIOLET_DB_PATH"])
    project_db = Path(__file__).resolve().parent.parent / "violet.db"
    assert Path(violetdb.db_path) != project_db


def test_legacy_database_is_readable_and_upgraded(tmp_path):
    """A DB created by the old schema (no inbox/outbox) opens without data loss."""
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            direction TEXT NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
            message_type TEXT NOT NULL CHECK (message_type IN ('text', 'image', 'file', 'voice', 'video')),
            content TEXT, media_id TEXT, created_at TEXT NOT NULL
        );
        INSERT INTO messages (role, direction, message_type, content, created_at)
        VALUES ('user', 'incoming', 'text', 'legacy hello', '2026-05-01T10:00:00+08:00');
        """
    )
    conn.commit()
    conn.close()

    db = VioletDatabase(db_path=legacy)
    try:
        rows = db.get_recent_messages(limit=5)
        assert [r["content"] for r in rows] == ["legacy hello"]
        tables = {r["name"] for r in db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"inbound_messages", "outbound_messages", "event_memories", "core_memories"} <= tables
    finally:
        db.close()


def test_inbound_dedup(violetdb):
    first = violetdb.add_inbound_message("mid:1:0", "1", 0, 1, "hi")
    second = violetdb.add_inbound_message("mid:1:0", "1", 0, 1, "hi")
    assert first is not None
    assert second is None
    violetdb.update_inbound_messages([first], status="done", turn_id="t1")
    row = violetdb.get_inbound_message(first)
    assert row["status"] == "done" and row["turn_id"] == "t1"


def test_outbound_status_lifecycle(violetdb):
    oid = violetdb.add_outbound_message("cid-1", "reply", "text", "hello", turn_id="t1")
    violetdb.update_outbound_message(oid, status="failed", error="boom", increment_attempts=True)
    row = violetdb.get_outbound_message(oid)
    assert row["status"] == "failed" and row["error"] == "boom" and row["attempts"] == 1
    assert violetdb.count_outbound_by_status() == {"failed": 1}


def test_invalid_status_rejected(violetdb):
    import pytest
    with pytest.raises(ValueError):
        violetdb.add_outbound_message("cid-2", "reply", "text", "x", status="bogus")


def test_concurrent_writes_do_not_corrupt(violetdb):
    errors = []

    def writer(n):
        try:
            for i in range(50):
                violetdb.add_user_message("text", f"w{n}-{i}")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert violetdb.fetch_one("SELECT COUNT(*) AS c FROM messages")["c"] == 300
