"""翻篇 (2026-10-30, user: "我删了记忆之后还是会受到上下文的影响").

Clearing the memory left the recent chat in her context (the last ~24 turns with the owner, the last 30 messages of a
group, what she said in groups lately), so she still talked about what was "forgotten". A cut is a point in time:
from then on the model only sees what was said after it. Nothing is deleted -- the 聊天记录 page shows everything.

  <data>/control/context-cut.json   {"owner": time, "social": time, "conversations": {"<id>": time}}

Test mode (ai/test_mode.py) is one more cut while it is on: the later of the two counts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCOPES = ("all", "owner", "social")


def _path() -> Path:
    return Path(os.environ.get("VIOLET_DATA_DIR") or "./data") / "control" / "context-cut.json"


def _read() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(value: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def cut(db, scope: str = "all", conversation_id: int | None = None) -> dict[str, Any]:
    """Turn the page now. scope: all | owner (the owner's chat on every channel) | social (every group and other
    people's private chats); conversation_id: only that one group / private chat."""
    data = _read()
    now = db.now()
    if conversation_id is not None:
        data.setdefault("conversations", {})[str(int(conversation_id))] = now
    else:
        if scope not in SCOPES:
            raise ValueError("翻篇的范围要是 all / owner / social")
        if scope in ("all", "owner"):
            data["owner"] = now
        if scope in ("all", "social"):
            data["social"] = now
    _write(data)
    return status()


def status() -> dict[str, Any]:
    data = _read()
    return {"owner": data.get("owner"), "social": data.get("social"), "conversations": data.get("conversations") or {}}


def _later(*times: str | None) -> str | None:
    known = [t for t in times if t]
    return max(known) if known else None


def owner_since() -> str | None:
    from ai import test_mode
    return _later(_read().get("owner"), test_mode.since())


def social_since(conversation_id: int | None = None) -> str | None:
    from ai import test_mode
    data = _read()
    one = (data.get("conversations") or {}).get(str(conversation_id)) if conversation_id is not None else None
    return _later(data.get("social"), one, test_mode.since())


def _after(rows: list[dict[str, Any]], since: str | None, key: str) -> list[dict[str, Any]]:
    if not since:
        return rows
    return [r for r in rows if str(r.get(key) or "") >= since]


def owner_rows(rows: list[dict[str, Any]], key: str = "created_at") -> list[dict[str, Any]]:
    """The owner's chat as the model may see it (after the cut and after the start of test mode)."""
    return _after(rows, owner_since(), key)


def social_rows(conversation_id: int | None, rows: list[dict[str, Any]], key: str = "created_at") -> list[dict[str, Any]]:
    return _after(rows, social_since(conversation_id), key)
