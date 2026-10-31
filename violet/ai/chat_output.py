"""Utilities for turning ChatAgent plain text into sendable messages."""

from __future__ import annotations

import re
from typing import Any

# ChatAgent may answer with only this token to say "nothing worth sending now"
# (used by proactive generation; harmless elsewhere).
NO_REPLY_TOKEN = "[NO_REPLY]"
# Phase 6: an image produced by an image tool in this turn, placed on its own line.
IMAGE_MARKER_RE = re.compile(r"\[\[图片[:：]\s*(\d+)\]\]")
# 0.7.9: a sticker of her collection (ai/stickers.py); the outbox looks the number up, nothing else is trusted
STICKER_MARKER_RE = re.compile(r"\[\[表情[:：]\s*(\d+)\]\]")
MEDIA_MARKER_RE = re.compile(r"\[\[(图片|表情)[:：]\s*(\d+)\]\]")
MARKDOWN_EMPHASIS_RE = re.compile(r"(\*\*|__)(\S(?:.*?\S)?)\1")


def image_marker(asset_id: int) -> str:
    return f"[[图片:{int(asset_id)}]]"


def strip_stray_brackets(line: str) -> str:
    """Drop 【 / 】 that do not enclose anything on this line.

    Phase 8.9.2: the model sometimes ends a reply with a bare "】【" (8.9.1 real E5 step 8 "…听听看】【。", 32-round
    report 20261013-073654 round 19 "…烦人精是我】【。") -- the brackets of Violet's own context notes leaking into her
    words. A real pair with something inside ("【通知】") is kept; only unmatched or empty brackets go."""
    if "【" not in line and "】" not in line:
        return line
    keep = [True] * len(line)
    opened: list[int] = []
    for i, ch in enumerate(line):
        if ch == "【":
            opened.append(i)
        elif ch == "】":
            if opened and line[opened[-1] + 1:i].strip():
                opened.pop()
            else:
                keep[i] = False
                if opened:                                   # an empty pair "【】": both go
                    keep[opened.pop()] = False
    for i in opened:                                         # never closed
        keep[i] = False
    return "".join(ch for ch, k in zip(line, keep) if k).strip()


VOICE_TAG_RE = re.compile(r"<voice>(.*?)</voice>", re.S | re.I)
VOICE_LINE = "[[语音]]"                                     # a line to be said (ai/voice.py), not written
STICKER_TAG_RE = re.compile(r"<sticker[:：]\s*([^>\s]{1,12})\s*>", re.I)


def expand_markup(text: str, sticker_for_label=None) -> str:
    """2026-10-31: the markup of prompts written for other bots (the Aurabot persona, persona/templates/dafeiyu.json):
    "|||" = a new message, <voice>…</voice> = a line starting with VOICE_LINE (a voice message), <sticker:开心> = a sticker of
    her collection that fits the label ([[表情:N]]), dropped when none fits."""
    text = VOICE_TAG_RE.sub(lambda m: "\n" + VOICE_LINE + " ".join(m.group(1).split()) + "\n", text or "")

    def sticker(m: re.Match) -> str:
        number = None
        if sticker_for_label is not None:
            try:
                number = sticker_for_label(m.group(1))
            except Exception:                                # noqa: BLE001 -- a sticker is never worth an error
                number = None
        return f"\n[[表情:{number}]]\n" if number else ""
    text = STICKER_TAG_RE.sub(sticker, text)
    return text.replace("|||", "\n")


def _owner_sticker_for_label(label: str):
    from ai import stickers
    from ai.chat import violetdb
    row = stickers.by_label(violetdb, label)
    return row["id"] if row else None


def normalize_chat_output(raw_content: str, sticker_for_label=_owner_sticker_for_label) -> dict[str, Any]:
    """ChatAgent now emits plain text; each non-empty line is one message."""
    text = (raw_content or "").strip()
    if not text:
        return {"should_send": False, "messages": []}
    if "|||" in text or "<voice" in text.lower() or "<sticker" in text.lower():
        text = expand_markup(text, sticker_for_label).strip()

    text = re.sub(r"^```(?:text)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()
    # Phase 8.9: WeChat shows Markdown emphasis as literal stars ("**注意休息，别太累啦**" in a real run)
    text = MARKDOWN_EMPHASIS_RE.sub(r"\2", text)
    lines = [strip_stray_brackets(line.strip()) for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if line and line.upper() != NO_REPLY_TOKEN]
    if not lines:
        return {"should_send": False, "messages": [], "declined": NO_REPLY_TOKEN in text.upper()}

    messages = []
    stickers = 0
    for line in lines:
        pos = 0
        for match in MEDIA_MARKER_RE.finditer(line):
            before = line[pos:match.start()].strip()
            if before:
                messages.append({"type": "text", "content": before, "media_id": None})
            if match.group(1) == "图片":
                messages.append({"type": "image", "asset_id": int(match.group(2)), "content": None, "media_id": None})
            elif stickers < 1:                               # one sticker per reply
                messages.append({"type": "sticker", "sticker_id": int(match.group(2)), "content": None,
                                 "media_id": None})
                stickers += 1
            pos = match.end()
        rest = line[pos:].strip()
        if rest.startswith(VOICE_LINE):                      # 2026-10-31: said, not written (MessageSender)
            spoken = rest[len(VOICE_LINE):].strip()
            if spoken:
                messages.append({"type": "voice", "content": spoken, "media_id": None})
        elif rest:
            messages.append({"type": "text", "content": rest, "media_id": None})
    if not messages:
        return {"should_send": False, "messages": []}
    return {"should_send": True, "messages": messages}


def attach_images(payload: dict[str, Any], allowed_asset_ids: list[int], turn_key: str | None = None) -> dict[str, Any]:
    """Keep only images produced in this turn; images the reply forgot to place go at the end, once.

    Every kept image carries the turn key; the outbox sends it only if the image belongs to a
    succeeded image task of exactly that turn (MessageSender._authorize_image). Without a turn
    key, no image survives.
    """
    allowed = [int(a) for a in allowed_asset_ids or []] if turn_key else []
    messages, placed = [], set()
    for msg in payload.get("messages") or []:
        if msg.get("type") == "image":
            if msg.get("asset_id") not in allowed or msg["asset_id"] in placed:
                continue
            placed.add(msg["asset_id"])
            msg = {**msg, "turn_key": turn_key}
        messages.append(msg)
    for asset_id in allowed:
        if asset_id not in placed:
            messages.append({"type": "image", "asset_id": asset_id, "content": None, "media_id": None,
                             "turn_key": turn_key})
    return {**payload, "messages": messages, "should_send": bool(messages)}


def attach_stickers(payload: dict[str, Any], sticker_ids: list[int] | None) -> dict[str, Any]:
    """2026-10-30: the sticker she picked with send_sticker goes out after the text (one per reply; a reply that
    already carries a [[表情:N]] line keeps that one). The outbox still checks the number against the collection."""
    messages = list(payload.get("messages") or [])
    if not sticker_ids or any(m.get("type") == "sticker" for m in messages):
        return payload
    messages.append({"type": "sticker", "sticker_id": int(sticker_ids[0]), "content": None, "media_id": None})
    return {**payload, "messages": messages, "should_send": True}


def strip_images(payload: dict[str, Any]) -> dict[str, Any]:
    """Proactive messages, fixed-time tasks and reminders never send images."""
    messages = [m for m in payload.get("messages") or [] if m.get("type") != "image"]
    return {**payload, "messages": messages, "should_send": bool(messages)}
