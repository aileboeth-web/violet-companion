"""Whose memory (2026-11-02, user: "对我来说人设不同他们人也不同啊，怎么能共享记忆呢").

A channel with its own persona (人设页, "这个渠道用别的人设") is a different person: she has her own chat history, her
own memories of the owner, her own virtual life and mood, her own proactive rhythm. Channels that use the same persona
are the same person and share all of it. The unit is the persona ("scope"):

    "main"          every channel without its own persona, and a channel whose own persona is the main one
    <template key>  e.g. "eve", "dafeiyu", "custom": the channels that chose this persona for themselves

Storage: the main scope is the database Violet always had; any other scope is a database file of its own next to it
(<db folder>/personas/<scope>.db). VioletDatabase routes every statement that touches a table of SCOPED_TABLES to the
file of the scope in effect (database.py); everything else -- settings, channels, stickers, timetable, QQ social life,
images, usage -- stays shared in the main file.

The scope in effect is a context variable: set around every owner turn and proactive run by channel_scope() (from the
channel being talked on), around background work by run_as(), and around Control Center requests by the page's
persona picker. Unset = "main", exactly what Violet did before.
"""

from __future__ import annotations

import contextlib
import contextvars
import re
from typing import Iterator

MAIN = "main"

# owner relationship + her own life: what one persona knows and has lived
SCOPED_TABLES = frozenset({
    "messages", "outbound_messages", "core_memories", "event_memories", "memory_revisions", "memory_jobs",
    "commitments", "topic_mentions", "user_profile_facts", "profile_audit_log", "profile_questions",
    "user_situations", "interaction_preferences", "preference_observations", "persona_dynamic", "life_events",
    "life_plans", "life_threads", "life_revisions", "life_engine_runs", "mood_state", "consistency_flags",
    "task_sessions", "proactive_events", "proactive_settings",
})

_SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar("violet_memory_scope", default=MAIN)
_SAFE = re.compile(r"[^a-z0-9_-]")


def current() -> str:
    return _SCOPE.get()


@contextlib.contextmanager
def use(scope: str | None) -> Iterator[str]:
    scope = clean(scope)
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def clean(scope: str | None) -> str:
    scope = _SAFE.sub("", str(scope or "").lower())[:40]
    return scope or MAIN


def for_channel(channel: str | None) -> str:
    """The scope a channel talks in: its own persona's, or main (no own persona / the same as the main one)."""
    if not channel:
        return MAIN
    from ai import persona_templates as pt
    own = (pt.channel_map().get(channel) or {}).get("template")
    if not own or own == pt.current().get("template"):
        return MAIN
    return clean(own)


def channels() -> list[str]:
    """Every channel id there is (WeChat first)."""
    out = ["wechat"]
    try:
        from ai.channels.manager import ChannelStore
        out += [cid for cid in ChannelStore().channels() if cid != "wechat"]
    except Exception:                                     # noqa: BLE001 -- no channel file yet
        pass
    return out


def channels_of(scope: str) -> list[str]:
    return [c for c in channels() if for_channel(c) == scope]


def all_scopes() -> list[str]:
    """main + the scope of every channel that has its own persona."""
    out = [MAIN]
    for channel in channels():
        scope = for_channel(channel)
        if scope not in out:
            out.append(scope)
    return out


def label(scope: str) -> str:
    """The persona's name for the pages."""
    from ai import persona_templates as pt
    try:
        if scope == MAIN:
            return pt.in_effect(None)["persona"]
        channel = next(iter(channels_of(scope)), None)
        return pt.in_effect(channel)["persona"] if channel else scope
    except Exception:                                     # noqa: BLE001
        return scope


@contextlib.contextmanager
def run_as(scope: str) -> Iterator[str]:
    """Background work for one scope: the scope, and a channel of it as the channel context (so PersonaCore.load and
    the relationship read that persona). Main without a channel of its own reads the main persona ("")."""
    from ai.channels.context import ChannelContext, _CURRENT
    scope = clean(scope)
    channel = next(iter(channels_of(scope)), None)
    if scope == MAIN and channel is None:
        channel = ""
    token = _CURRENT.set(ChannelContext(channel=channel or ""))
    try:
        with use(scope):
            yield scope
    finally:
        _CURRENT.reset(token)


SPLIT_FILE = "persona-split.json"


def _split_done() -> dict:
    from ai import persona_templates as pt
    import json
    try:
        data = json.loads((pt.control_dir() / SPLIT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def split_channels(db) -> dict[str, dict[str, int]]:
    """2026-11-02 (user: 已有的聊天按渠道拆): the first time a channel has a persona of its own, the chat it had in the
    main memory goes with it -- its messages and outbox rows, and the event memories that came only from those
    messages (with their revisions). Memories without a known source stay with the main persona. Once per channel
    (<data>/control/persona-split.json); later persona changes move nothing (a new persona is a new person)."""
    from ai import persona_templates as pt
    import json
    done = _split_done()
    moved: dict[str, dict[str, int]] = {}
    for channel in channels():
        scope = for_channel(channel)
        if scope == MAIN or channel in done:
            continue
        moved[channel] = _move_channel(db, channel, scope)
        done[channel] = {"scope": scope, "at": db.now(), **moved[channel]}
        pt._write(pt.control_dir() / SPLIT_FILE, done)
    return moved


def _move_channel(db, channel: str, scope: str) -> dict[str, int]:
    import json
    target = db.scope_db(scope)
    with use(MAIN):
        messages = db.fetch_all("SELECT * FROM messages WHERE COALESCE(channel, 'wechat') = ? ORDER BY id", (channel,))
        if not messages:
            return {"messages": 0, "events": 0}
        outbound = db.fetch_all("SELECT * FROM outbound_messages WHERE COALESCE(channel, 'wechat') = ?", (channel,))
        ids = {int(m["id"]) for m in messages}
        events = []
        for e in db.fetch_all("SELECT * FROM event_memories WHERE source_message_ids IS NOT NULL"):
            try:
                src = [int(i) for i in json.loads(e["source_message_ids"] or "[]")]
            except (TypeError, ValueError):
                continue
            if src and all(i in ids for i in src):
                events.append(e)
        revisions = [r for e in events for r in db.fetch_all(
            "SELECT * FROM memory_revisions WHERE memory_table = 'event_memories' AND memory_id = ?", (e["id"],))]

    def copy(table: str, rows: list[dict]) -> list[int]:
        """Rows copied with their ids; a row whose id is taken there stays where it is (never lost)."""
        cols = {r[1] for r in target.conn.execute(f"PRAGMA table_info({table})")}
        copied = []
        for row in rows:
            keys = [k for k in row if k in cols]
            cur = target.conn.execute(f"INSERT OR IGNORE INTO {table} ({', '.join(keys)}) "
                                      f"VALUES ({', '.join('?' * len(keys))})", [row[k] for k in keys])
            if cur.rowcount:
                copied.append(row["id"])
        return copied

    with db.transaction():                    # holds the shared lock; the target file is written in one commit
        with target.conn:
            done = {t: copy(t, rows) for t, rows in (("messages", messages), ("outbound_messages", outbound),
                                                     ("event_memories", events), ("memory_revisions", revisions))}
        with use(MAIN):
            for table in ("memory_revisions", "event_memories", "outbound_messages", "messages"):
                for row_id in done[table]:
                    db.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    return {"messages": len(done["messages"]), "events": len(done["event_memories"])}


def tables_in(sql: str) -> set[str]:
    return {m.lower() for m in _TABLES.findall(sql)}


_TABLES = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN|TABLE(?:\s+IF\s+(?:NOT\s+)?EXISTS)?|table_info\s*\()\s*\(?\s*"
                     r"([A-Za-z_][A-Za-z0-9_]*)", re.I)


def is_scoped(sql: str) -> bool:
    return bool(tables_in(sql) & SCOPED_TABLES)
