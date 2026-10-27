"""Who Violet talks to on each channel (phase 10).

Violet has ONE conversation partner (the owner): ``messages``, memories and the profile all belong to that person. A
platform account reaches that conversation only once it is BOUND to the owner. This is the identity mapping that keeps
channels from mixing data: a stranger who finds the Telegram bot or messages the QQ account never gets an answer and
nothing they send is stored as the owner's words (the inbox keeps only a placeholder).

Binding:
  * ``accept = "first_peer"`` (default for WeChat iLink, where the account that scanned the QR code is the only one
    talking to the bot): the first peer seen on the channel is bound automatically, later strangers are pending;
  * ``accept = "owner"`` (default for every other channel): a peer is bound only by the admin -- approving a pending
    peer on the Control Center, or a one-time pairing code (``/bind 123456`` or ``绑定 123456`` sent from the new
    account; the code is shown once, stored as a hash, valid 10 minutes, 5 wrong tries per peer per hour);
  * a peer id of "" (a transport that has only one possible sender) is the owner.

Several bound accounts (WeChat + Telegram + QQ) share the owner's conversation and memory -- explicitly, because the
admin bound each of them.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Any

from ai.channels.normalize import clean_text

STATUSES = ("pending", "bound", "blocked")
ACCEPT_POLICIES = ("owner", "first_peer")
PAIRING_TTL_SECONDS = 600
PAIRING_MAX_TRIES = 5
BIND_RE = re.compile(r"^\s*(?:/bind|/start|绑定)\s*(\d{6})\s*$", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    display_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    bound_at TEXT,
    bound_by TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (channel, peer_id)
);
CREATE TABLE IF NOT EXISTS channel_pairing_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash TEXT NOT NULL UNIQUE,
    channel TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    used_by TEXT
);
"""


def _hash_code(code: str) -> str:
    return hashlib.sha256(("violet-pairing:" + code).encode("utf-8")).hexdigest()


class IdentityStore:
    def __init__(self, db):
        self.db = db
        self._tries: dict[tuple[str, str], list[float]] = {}

    # -- reading ------------------------------------------------------------------------------------------------
    def get(self, channel: str, peer_id: str) -> dict[str, Any] | None:
        return self.db.fetch_one("SELECT * FROM channel_identities WHERE channel = ? AND peer_id = ?", (channel, peer_id))

    def list(self, channel: str | None = None) -> list[dict[str, Any]]:
        if channel:
            return self.db.fetch_all("SELECT * FROM channel_identities WHERE channel = ? ORDER BY last_seen_at DESC",
                                     (channel,))
        return self.db.fetch_all("SELECT * FROM channel_identities ORDER BY channel, last_seen_at DESC")

    def bound_peers(self, channel: str) -> list[str]:
        return [r["peer_id"] for r in self.db.fetch_all(
            "SELECT peer_id FROM channel_identities WHERE channel = ? AND status = 'bound' ORDER BY last_seen_at DESC",
            (channel,))]

    def primary_peer(self, channel: str) -> str | None:
        """The bound peer last heard from on this channel (where proactive messages go)."""
        peers = self.bound_peers(channel)
        return peers[0] if peers else None

    # -- the decision for one incoming message ------------------------------------------------------------------
    def admit(self, channel: str, peer_id: str, accept: str = "owner", display_name: str | None = None) -> str:
        """owner (process it) | pending (unknown, not processed) | blocked. Records the peer either way."""
        if peer_id == "":
            return "owner"
        now = self.db.now()
        name = clean_text(display_name, 60) if display_name else None
        with self.db.transaction():
            row = self.get(channel, peer_id)
            if row is None:
                status = "pending"
                if accept == "first_peer" and not self.bound_peers(channel):
                    status = "bound"
                self.db.execute(
                    "INSERT INTO channel_identities (channel, peer_id, display_name, status, first_seen_at, last_seen_at, "
                    "bound_at, bound_by, message_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (channel, peer_id, name, status, now, now, now if status == "bound" else None,
                     "first_peer" if status == "bound" else None))
            else:
                status = row["status"]
                self.db.execute("UPDATE channel_identities SET last_seen_at = ?, message_count = message_count + 1, "
                                "display_name = COALESCE(?, display_name) WHERE id = ?", (now, name, row["id"]))
        return "owner" if status == "bound" else status

    # -- admin actions -----------------------------------------------------------------------------------------
    def set_status(self, identity_id: int, status: str, actor: str = "admin") -> dict[str, Any]:
        if status not in STATUSES:
            raise ValueError("状态只能是 pending / bound / blocked")
        row = self.db.fetch_one("SELECT * FROM channel_identities WHERE id = ?", (identity_id,))
        if row is None:
            raise LookupError("没有这个联系人")
        self.db.execute("UPDATE channel_identities SET status = ?, bound_at = ?, bound_by = ? WHERE id = ?",
                        (status, self.db.now() if status == "bound" else None, actor if status == "bound" else None,
                         identity_id))
        return self.db.fetch_one("SELECT * FROM channel_identities WHERE id = ?", (identity_id,))

    def create_pairing_code(self, channel: str | None = None) -> dict[str, Any]:
        """A six-digit code, returned once (only its hash is stored)."""
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = datetime.fromisoformat(self.db.now())
        expires = (now + timedelta(seconds=PAIRING_TTL_SECONDS)).isoformat()
        self.db.execute("DELETE FROM channel_pairing_codes WHERE used_at IS NULL AND expires_at < ?", (now.isoformat(),))
        self.db.execute("INSERT OR REPLACE INTO channel_pairing_codes (code_hash, channel, created_at, expires_at) "
                        "VALUES (?, ?, ?, ?)", (_hash_code(code), channel, now.isoformat(), expires))
        return {"code": code, "channel": channel, "expires_at": expires}

    def try_pairing(self, channel: str, peer_id: str, text: str) -> str | None:
        """For a message from a peer that is not bound: None (not a pairing message), "bound", "invalid" or
        "locked" (too many wrong codes)."""
        match = BIND_RE.match(text or "")
        if not match or not peer_id:
            return None
        key = (channel, peer_id)
        now_mono = time.monotonic()
        tries = [t for t in self._tries.get(key, []) if now_mono - t < 3600]
        if len(tries) >= PAIRING_MAX_TRIES:
            self._tries[key] = tries
            return "locked"
        now = self.db.now()
        wanted = _hash_code(match.group(1))
        row = None
        for candidate in self.db.fetch_all("SELECT * FROM channel_pairing_codes WHERE used_at IS NULL AND expires_at >= ?",
                                           (now,)):
            if hmac.compare_digest(candidate["code_hash"], wanted) and candidate["channel"] in (None, channel):
                row = candidate
                break
        if row is None:
            tries.append(now_mono)
            self._tries[key] = tries
            return "invalid"
        with self.db.transaction():
            self.db.execute("UPDATE channel_pairing_codes SET used_at = ?, used_by = ? WHERE id = ?",
                            (now, f"{channel}:{peer_id}"[:120], row["id"]))
            existing = self.get(channel, peer_id)
            if existing is None:
                self.db.execute(
                    "INSERT INTO channel_identities (channel, peer_id, status, first_seen_at, last_seen_at, bound_at, "
                    "bound_by, message_count) VALUES (?, ?, 'bound', ?, ?, ?, 'pairing_code', 1)",
                    (channel, peer_id, now, now, now))
            else:
                self.db.execute("UPDATE channel_identities SET status = 'bound', bound_at = ?, bound_by = 'pairing_code' "
                                "WHERE id = ?", (now, existing["id"]))
        self._tries.pop(key, None)
        return "bound"
