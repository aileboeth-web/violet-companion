"""Read records written before the rename to Violet (2026-10-26).

Reports, seeds, episode files and test samples of the earlier phases are evidence and stay byte-for-byte as they were
written (their SHA-256 values are recorded elsewhere), so they still say "eve" where today's tools say "violet": the
reply key of a round (``{"user": ..., "eve": [...]}``), the speaker role of a history row, identifier-like values such as
``db:eve_picture_sent`` and paths under the old program folder ``Project Eve/``.

``upgrade(data)`` returns a copy in today's names. It changes only those structural names -- never free text: what the
user or the model said (which may well contain the word "Eve") is returned unchanged. Hashes are always taken of the
raw bytes, before upgrading.
"""

from __future__ import annotations

import re
from typing import Any

OLD_FOLDER = "Project Eve"
_IDENT_RE = re.compile(r"[a-z0-9_:.\-]+")
# "eve", "eve_x", "eve-web" / "eve-schedule" -- not the names of the separate MCP project (eve-mcp-relay ...)
_EVE_PART_RE = re.compile(r"(?<![a-z])eve(?=_|$|-(?!mcp|readonly|android|release))")


def _name(text: str) -> str:
    """An identifier-like string: "eve" -> "violet", "eve_motives" -> "violet_motives", "db:eve_x" -> "db:violet_x",
    "tools_has:eve-web__web_search" -> "tools_has:violet-web__web_search"."""
    return _EVE_PART_RE.sub("violet", text)


def upgrade_path(text: str) -> str:
    for sep in ("/", "\\"):
        if text.startswith(OLD_FOLDER + sep):
            return "violet" + sep + text[len(OLD_FOLDER) + 1:]
    return text


def _key(key: Any) -> Any:
    if not isinstance(key, str):
        return key
    if key.startswith(OLD_FOLDER):
        return upgrade_path(key)                     # e.g. {"Project Eve/tools/output/.../report.json": "<sha256>"}
    return _name(key) if _IDENT_RE.fullmatch(key) else key


def upgrade(data: Any) -> Any:
    if isinstance(data, dict):
        return {_key(k): upgrade(v) for k, v in data.items()}
    if isinstance(data, list):
        return [upgrade(v) for v in data]
    if isinstance(data, str):
        if data.startswith(OLD_FOLDER):
            return upgrade_path(data)
        if _IDENT_RE.fullmatch(data):
            return _name(data)
    return data
