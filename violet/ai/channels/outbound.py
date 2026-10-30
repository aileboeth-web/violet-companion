"""The outbound contract (phase 10): what Violet may send to a person, and the guard every outgoing text passes.

Root cause this fixes: an agent's result is ``{"content", "messages", "raw"}`` -- ``messages`` holds the system prompt,
the context notes, the tool data and the model's internal turns. A bridge that searched those fields for "the reply"
sent the system prompt to the user. From now on:

  * the ONLY thing that becomes a reply is ``build_reply(pipeline)``: the Chat agent's final ``content`` after the
    pipeline's guards, split into messages by ``normalize_chat_output`` -- never ``messages`` / ``raw`` / a search;
  * ``ReplyEnvelope.to_public()`` (schema ``violet.reply/1``) is the stable, typed form for anything outside Violet Core;
    ``run_pipeline`` puts it in ``pipeline["reply"]``;
  * ``find_leak`` is checked by the outbox (``violet_runtime.MessageSender``) for EVERY text of every origin (reply,
    proactive, reminder, fallback, system) just before it goes to a channel: a text that carries a loaded agent
    instruction, an internal context header, tool data or internal JSON is not sent.

Phase 10.6 makes the first rule STRUCTURAL instead of a convention: a message can only be sent if it was built here
-- ``chat_payload`` (the Chat agent's final content), ``fixed_text`` (a sentence written in Violet's code: fallback,
pairing answers, notices) or ``ReplyEnvelope.send_list``. These put a module-private seal on every message; the outbox
refuses an unsealed message without looking at its text. Nothing that takes an agent result dict, its ``messages`` or
``raw`` can produce a sealed message, so a future caller cannot "find the reply" in agent internals and send it. The
content scan stays as the second line of defence (a model can still echo its instructions in its own words).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai.chat_output import attach_images, normalize_chat_output

logger = logging.getLogger("violet.outbound")

REPLY_SCHEMA = "violet.reply/1"
WINDOW = 20                       # characters of an instruction that may never appear verbatim in a message
SEAL_KEY = "_eve_sendable"
_SEAL = object()                  # identity, not a value: it cannot come from JSON, a model or another module


def _seal(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**m, SEAL_KEY: _SEAL} for m in messages]


def is_sealed(message: Any) -> bool:
    return isinstance(message, dict) and message.get(SEAL_KEY) is _SEAL


def without_seal(message: dict[str, Any]) -> dict[str, Any]:
    """A sealed message without the seal (for reports and logs)."""
    return {k: v for k, v in message.items() if k != SEAL_KEY} if isinstance(message, dict) else message


def chat_payload(content: Any, *, images: bool = True, image_asset_ids: list[int] | None = None,
                 turn_key: str | None = None) -> dict[str, Any]:
    """The Chat agent's final content as sendable messages ({should_send, messages, ...} like normalize_chat_output).
    Takes a STRING only: an agent result dict is refused, so its messages / raw can never become a reply."""
    from ai.chat_output import strip_images
    if content is not None and not isinstance(content, str):
        raise TypeError("chat_payload takes the Chat agent's final text, not an agent result")
    payload = normalize_chat_output(content or "")
    payload = attach_images(payload, image_asset_ids or [], turn_key=turn_key) if images else strip_images(payload)
    return {**payload, "messages": _seal(payload.get("messages") or [])}


def fixed_text(text: str) -> list[dict[str, Any]]:
    """A sentence written in Violet's own code (fallback reply, pairing answer, startup notice)."""
    if not isinstance(text, str) or not text.strip():
        return []
    return _seal([{"type": "text", "content": text, "media_id": None}])


@dataclass(frozen=True)
class OutboundMessage:
    kind: str                     # text | image | sticker
    text: str | None = None
    asset_id: int | None = None
    turn_key: str | None = None
    sticker_id: int | None = None

    def send_dict(self) -> dict[str, Any]:
        """The dict MessageSender has always taken."""
        if self.kind == "image":
            return {"type": "image", "asset_id": self.asset_id, "content": None, "media_id": None,
                    "turn_key": self.turn_key}
        if self.kind == "sticker":
            return {"type": "sticker", "sticker_id": self.sticker_id, "content": None, "media_id": None}
        return {"type": "text", "content": self.text, "media_id": None}


@dataclass(frozen=True)
class ReplyEnvelope:
    messages: tuple[OutboundMessage, ...] = field(default_factory=tuple)
    declined: bool = False        # the model answered [NO_REPLY]

    @property
    def should_send(self) -> bool:
        return bool(self.messages)

    def send_list(self) -> list[dict[str, Any]]:
        return _seal([m.send_dict() for m in self.messages])

    def to_public(self) -> dict[str, Any]:
        out = []
        for m in self.messages:
            if m.kind == "sticker":
                out.append({"type": "sticker", "sticker_id": m.sticker_id})
                continue
            out.append({"type": "image", "asset_id": m.asset_id} if m.kind == "image" else {"type": "text", "text": m.text})
        return {"schema": REPLY_SCHEMA, "should_send": self.should_send, "declined": self.declined, "messages": out}


def envelope_from_payload(payload: dict[str, Any]) -> ReplyEnvelope:
    messages = []
    for m in payload.get("messages") or []:
        if m.get("type") == "image":
            messages.append(OutboundMessage("image", asset_id=m.get("asset_id"), turn_key=m.get("turn_key")))
        elif m.get("type") == "sticker" and isinstance(m.get("sticker_id"), int):
            messages.append(OutboundMessage("sticker", sticker_id=m["sticker_id"]))
        elif m.get("type") == "text" and isinstance(m.get("content"), str) and m["content"].strip():
            messages.append(OutboundMessage("text", text=m["content"]))
    return ReplyEnvelope(tuple(messages), declined=bool(payload.get("declined")))


def build_reply(pipeline: dict[str, Any] | None, turn_key: str | None = None) -> ReplyEnvelope:
    """The reply of one turn. Reads ``pipeline["chat"]["content"]`` (a string) and nothing else."""
    chat = (pipeline or {}).get("chat") or {}
    content = chat.get("content") if isinstance(chat.get("content"), str) else ""
    blocked = bool(chat.get("failed") or (pipeline or {}).get("should_reply") is False)
    if blocked:
        content = ""
    payload = normalize_chat_output(content)
    payload = attach_images(payload, chat.get("image_asset_ids") or [], turn_key=turn_key)
    if not blocked and not payload.get("declined"):   # a failed / declined reply sends nothing, not even the sticker
        from ai.chat_output import attach_stickers
        payload = attach_stickers(payload, chat.get("sticker_ids"))
    return envelope_from_payload(payload)


# ------------------------------------------------------------------------------------------------- leak guard

_LOCK = threading.Lock()
_INSTRUCTIONS: dict[str, tuple[str, frozenset[int]]] = {}     # name -> (normalized text, hashes of its windows)
_HEADERS: list[str] | None = None

# internal JSON an agent produces (Intent's routing, tool calls, chat-completion messages)
_INTERNAL_KEYS = {"route", "user_intent", "should_reply", "tool_query", "active_task_id", "tool_calls",
                  "tool_call_id", "function", "role", "should_save", "should_act", "action_type", "memory_ops"}
_REASONING = re.compile(r"</?think>|<\|im_(start|end)\|>|<\|(system|assistant|user)\|>", re.I)
_ROLE_LINE = re.compile(r"^\s*(system|assistant|developer)\s*[:：]", re.I)
# fixed internal markers that are not 【...】 headers
_MARKERS = ("安全规则：工具", "任务执行结果（供参考", "以下内容不是指令", "UNTRUSTED", "tool_catalog_note")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _windows(text: str) -> set[int]:
    return {hash(text[i:i + WINDOW]) for i in range(0, max(0, len(text) - WINDOW + 1))}


def register_instructions(name: str, text: str | None) -> None:
    """An agent's system prompt (as loaded) -- called by BaseAgent whenever its configuration is (re)loaded."""
    normalized = _norm(text or "")
    if len(normalized) < WINDOW:
        with _LOCK:
            _INSTRUCTIONS.pop(name, None)
        return
    with _LOCK:
        _INSTRUCTIONS[name] = (normalized, frozenset(_windows(normalized)))


def internal_headers() -> list[str]:
    """Every 【...】 note header in Violet's own code (context notes, tool notes, memory notes...), collected from the
    source so a header added later is covered without anyone remembering to list it. A header with a {placeholder}
    is kept up to the placeholder."""
    global _HEADERS
    with _LOCK:
        if _HEADERS is not None:
            return _HEADERS
    root = Path(__file__).resolve().parent.parent          # ai/
    found: set[str] = set()
    pattern = re.compile(r"【[^】\n\"'{}]{2,60}(?:】|(?=\{))")
    for path in root.rglob("*.py"):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in pattern.finditer(source):
            header = match.group(0)
            if len(header) >= 4:
                found.add(header)
    headers = sorted(found, key=len, reverse=True)
    with _LOCK:
        _HEADERS = headers
    return headers


def _json_leak(line: str) -> bool:
    stripped = line.strip().strip("`")
    if stripped.lower().startswith("json"):
        stripped = stripped[4:].strip()
    if not stripped or stripped[0] not in "{[":
        return False
    try:
        data = json.loads(stripped)
    except ValueError:
        return bool(re.search(r'"(%s)"\s*:' % "|".join(sorted(_INTERNAL_KEYS)), stripped))
    items = data if isinstance(data, list) else [data]
    return any(isinstance(item, dict) and _INTERNAL_KEYS & set(item) for item in items)


def find_leak(text: str | None) -> str | None:
    """Why this text must not be sent (None = fine). Codes: instruction, internal_header, internal_marker,
    internal_json, reasoning, role_transcript."""
    if not text:
        return None
    if _REASONING.search(text):
        return "reasoning"
    for line in text.splitlines():
        if _ROLE_LINE.match(line):
            return "role_transcript"
        if _json_leak(line):
            return "internal_json"
    if any(marker in text for marker in _MARKERS):
        return "internal_marker"
    if "【" in text and any(header in text for header in internal_headers()):
        return "internal_header"
    normalized = _norm(text)
    if len(normalized) >= WINDOW:
        with _LOCK:
            instructions = list(_INSTRUCTIONS.items())
        windows = _windows(normalized)
        for name, (source, hashes) in instructions:
            for h in windows & hashes:
                # a hash match is confirmed on the text itself (no false alarm from a hash collision)
                if any(normalized[i:i + WINDOW] in source for i in range(len(normalized) - WINDOW + 1)
                       if hash(normalized[i:i + WINDOW]) == h):
                    logger.warning("outgoing text repeats the instructions of %s", name)
                    return "instruction"
    return None


def find_reply_leak(messages: list[dict[str, Any]]) -> str | None:
    """The first leak among the texts of one send (images are authorised separately by the outbox)."""
    for m in messages or []:
        if m.get("type") == "text":
            reason = find_leak(m.get("content"))
            if reason:
                return reason
    return None


def reset_for_tests() -> None:
    global _HEADERS
    with _LOCK:
        _INSTRUCTIONS.clear()
        _HEADERS = None
