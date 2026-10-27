-- Phase 10 (productisation): chat channels. VioletDatabase applies the same changes itself at start (additive only);
-- this file is the reviewed SQL for running them by hand on a copy. ADD COLUMN fails if the column already exists:
-- run it once, on a database that does not have the columns yet.
ALTER TABLE messages ADD COLUMN channel TEXT;            -- NULL on older rows = WeChat (the only channel before)
ALTER TABLE inbound_messages ADD COLUMN channel TEXT;
ALTER TABLE inbound_messages ADD COLUMN peer_id TEXT;    -- the sender on that platform ('' / NULL = single-user)
ALTER TABLE outbound_messages ADD COLUMN channel TEXT;
ALTER TABLE outbound_messages ADD COLUMN peer_id TEXT;

CREATE TABLE IF NOT EXISTS channel_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    display_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',              -- pending | bound | blocked
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    bound_at TEXT,
    bound_by TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (channel, peer_id)
);
CREATE TABLE IF NOT EXISTS channel_pairing_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash TEXT NOT NULL UNIQUE,                      -- SHA-256 of the code; the code itself is never stored
    channel TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    used_by TEXT
);
