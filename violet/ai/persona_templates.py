"""Persona templates and switching (2026-10-29, user: "要有可以自定义人设的功能，可以准备几套不同的模板，也允许空着人设，
就是没有任何身份，空白的记忆，慢慢养"; on a switch the memory can be kept or cleared).

  persona/templates/<key>.json     shipped templates (same format as persona_core.json; "blank": true = no identity)
  <data>/control/persona_core.json the chosen persona (PersonaCore.load prefers it); absent = the shipped Violet
  <data>/control/persona.json      which template, when, how the memory was handled
  <data>/control/persona-archive-<time>.json   what "clear the memory" removed (kept on the server, never deleted
                                   by Violet; the page says where it is)

Clearing removes what Violet knows about the user and her own past (memories, profile, situations, commitments, her
life events, her persona changes, the social impressions). The chat history stays (it is the record of what was said).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ai.settings import PROJECT_ROOT

TEMPLATE_DIR = PROJECT_ROOT / "persona" / "templates"
# what "clear" empties, in this order (archived first)
MEMORY_TABLES = ("core_memories", "event_memories", "memory_revisions", "commitments", "topic_mentions",
                 "user_profile_facts", "user_situations", "interaction_preferences", "preference_observations",
                 "profile_questions", "persona_dynamic", "life_events", "life_plans", "life_threads", "life_revisions")


def control_dir() -> Path:
    return Path(os.environ.get("VIOLET_DATA_DIR") or "./data") / "control"


def active_path() -> Path | None:
    path = control_dir() / "persona_core.json"
    return path if path.is_file() else None


def templates() -> list[dict[str, Any]]:
    out = []
    for path in sorted(TEMPLATE_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ident = data.get("identity") or {}
        out.append({"key": path.stem, "title": data.get("_title") or ident.get("name") or path.stem,
                    "summary": data.get("_summary") or "", "blank": bool(data.get("blank")),
                    "name": ident.get("name") or "", "personality": data.get("personality") or [],
                    "order": data.get("_order", 50)})
    return sorted(out, key=lambda t: (t["order"], t["key"]))


def current() -> dict[str, Any]:
    try:
        info = json.loads((control_dir() / "persona.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    return {"template": info.get("template") or ("custom" if active_path() else "violet"),
            "switched_at": info.get("switched_at"), "memory": info.get("memory"), "archive": info.get("archive")}


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def clear_memory(db) -> tuple[str, dict[str, int]]:
    """Archive, then empty MEMORY_TABLES and the social impressions. (archive file, rows removed per table)"""
    existing = {r["name"] for r in db.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")}
    archive: dict[str, Any] = {"archived_at": db.now(), "tables": {}}
    counts: dict[str, int] = {}
    for table in MEMORY_TABLES:
        if table in existing:
            rows = db.fetch_all(f"SELECT * FROM {table}")
            archive["tables"][table] = rows
            counts[table] = len(rows)
    archive["social_people"] = db.fetch_all("SELECT channel, user_id, nickname, impression FROM social_people "
                                            "WHERE impression IS NOT NULL")
    archive["social_notes"] = db.fetch_all("SELECT id, title, notes FROM social_conversations WHERE notes IS NOT NULL")
    path = control_dir() / f"persona-archive-{time.strftime('%Y%m%d-%H%M%S')}.json"
    _write(path, archive)
    with db.transaction():
        for table in MEMORY_TABLES:
            if table in existing:
                db.execute(f"DELETE FROM {table}")
        db.execute("UPDATE social_people SET impression = NULL")
        db.execute("UPDATE social_conversations SET notes = NULL")
    return str(path), counts


def switch(db, key: str, memory: str, name: str | None = None) -> dict[str, Any]:
    """Use template `key`; memory "keep" | "clear". name: the blank persona's name (optional)."""
    if memory not in ("keep", "clear"):
        raise ValueError("记忆要选“保留”或“清除”")
    path = TEMPLATE_DIR / f"{key}.json"
    if not key.replace("-", "").replace("_", "").isalnum() or not path.is_file():
        raise ValueError("没有这个人设模板")
    data = json.loads(path.read_text(encoding="utf-8"))
    if name:
        name = name.strip()[:20]
        data.setdefault("identity", {})["name"] = name
    archive, counts = (None, {})
    if memory == "clear":
        archive, counts = clear_memory(db)
    if key == "violet" and not name:
        (control_dir() / "persona_core.json").unlink(missing_ok=True)      # back to the shipped (updated) Violet
    else:
        _write(control_dir() / "persona_core.json", data)
    info = {"template": key, "switched_at": db.now(), "memory": memory, "archive": archive}
    _write(control_dir() / "persona.json", info)
    return {**info, "removed": counts}
