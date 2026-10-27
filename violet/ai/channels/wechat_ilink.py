"""WeChat iLink (phase 10): the existing channel, behind the common interface.

The receive loop for WeChat stays in ``VioletRuntime`` (``poll_once`` / ``_poll_loop``: cursor committed only after every
item is stored, session timeout -> new QR login, supervisor restart) -- it is proven and its tests stay. What changes:
  * ``parse_message`` turns one iLink message into an ``InboundMessage`` with ONE part per item and the same
    de-duplication keys as before (an upgrade never answers an old message twice);
  * the item types the runtime used to drop silently (voice, file, video, unknown) now reach Violet as safe text
    (a voice message with WeChat's own transcript is read as that transcript);
  * ``WeChatChannel`` lets the channel manager start / stop / report the loop like any other channel.

iLink delivers only the messages of the account that scanned the QR code and sends only to it: the peer is "" (the
channel's one possible sender, the owner).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from ai.channels.base import ChannelAdapter, ChannelStatus, InboundMessage, Part

ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
CHANNEL_ID = "wechat"
CHANNEL_TYPE = "wechat_ilink"


def dedup_key(raw: dict[str, Any], index: int, item: dict[str, Any]) -> str:
    """Unchanged from violet_runtime._dedup_key (rows written before phase 10 keep matching)."""
    message_id = raw.get("message_id")
    if message_id not in (None, ""):
        return f"mid:{message_id}:{index}"
    seq, created = raw.get("seq"), raw.get("create_time_ms")
    if seq is not None:
        return f"seq:{seq}:{created}:{index}"
    if created is not None:
        digest = hashlib.sha256(
            json.dumps({"from": raw.get("from_user_id"), "ct": created, "item": item}, sort_keys=True,
                       ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()[:32]
        return f"hash:{digest}:{index}"
    return f"nokey:{uuid.uuid4().hex}"           # no identity at all: never drop a message on a guess


def item_part(item: dict[str, Any], key: str) -> Part:
    kind = item.get("type")
    data: dict[str, Any] = {"dedup_key": key, "item_type": kind, "item": item}
    if kind == ITEM_TEXT:
        text = (item.get("text_item") or {}).get("text", "") or ""
        title = ((item.get("ref_msg") or {}).get("title") or "").strip()
        if title:
            data["quote"] = title
        return Part("text", text, data)
    if kind == ITEM_IMAGE:
        return Part("image", "", data)
    if kind == ITEM_VOICE:
        voice = item.get("voice_item") or {}
        return Part("voice", str(voice.get("text") or ""), data)
    if kind == ITEM_FILE:
        return Part("file", str((item.get("file_item") or {}).get("file_name") or ""), data)
    if kind == ITEM_VIDEO:
        return Part("video", "", data)
    return Part("unsupported", "", data)


def unexpected_sender(message: dict[str, Any], owner_id: str | None) -> str | None:
    """The sender of a message that did NOT come from the account that scanned the QR code, else None.

    iLink is expected to deliver only the owner's messages (that is why the peer is ""), but nothing checked it. Both
    ids must be known to decide: a message without a sender id, or a client without the owner's id, is not judged."""
    raw = message.get("raw") or message
    sender = str(raw.get("from_user_id") or message.get("from_user_id") or "").strip()
    owner = str(owner_id or "").strip()
    return sender if sender and owner and sender != owner else None


def parse_message(message: dict[str, Any], channel_id: str = CHANNEL_ID) -> InboundMessage:
    raw = message.get("raw") or message
    wx_id = raw.get("message_id", message.get("message_id"))
    parts = [item_part(item, dedup_key(raw, index, item)) for index, item in enumerate(raw.get("item_list") or [])]
    first = parts[0].data["dedup_key"] if parts else f"nokey:{uuid.uuid4().hex}"
    return InboundMessage(channel=channel_id, channel_type=CHANNEL_TYPE, peer_id="", dedup_key=first, parts=parts,
                          message_id=None if wx_id is None else str(wx_id))


class WeChatChannel(ChannelAdapter):
    """Start / stop / status of the runtime's WeChat loop for the channel manager. Sending stays on the runtime's
    WeChat MessageSender (the ILinkClient), so nothing about WeChat delivery changes."""

    channel_type = CHANNEL_TYPE
    label = "微信"
    capabilities = frozenset({"text", "image_in", "image_out", "typing", "voice_text"})

    def __init__(self, runtime, channel_id: str = CHANNEL_ID, config: dict[str, Any] | None = None):
        super().__init__(channel_id, config)
        self.runtime = runtime

    @property
    def client(self):
        return self.runtime.client

    def start(self, sink=None) -> None:
        self.runtime.start_polling()

    def stop(self, timeout: float = 5.0) -> None:
        self.runtime.stop_polling(timeout=timeout)

    def is_running(self) -> bool:
        return bool(self.runtime._poll_thread and self.runtime._poll_thread.is_alive())

    def receive_once(self) -> int:
        return self.runtime.poll_once()

    def send_text(self, peer_id: str, text: str, client_id: str | None = None) -> dict[str, Any]:
        return self.client.send_text(text, client_id=client_id)

    def send_image(self, peer_id: str, path: str) -> dict[str, Any]:
        return self.client.send_image(path)

    def send_typing(self, peer_id: str, on: bool) -> None:
        self.client.send_typing(status=1 if on else 2)

    def download(self, part: Part):
        return self.client.download_media(part.data.get("item") or {})

    def status(self) -> ChannelStatus:
        rt, info = self.runtime, self.status_info
        logged_in = True
        try:
            logged_in = bool(self.client.get_status().get("logged_in"))
        except Exception:
            logged_in = bool(getattr(getattr(self.client, "state", None), "logged_in", True))
        info.last_ok_at = rt.last_poll_ok_at
        info.last_error = rt.last_poll_error
        info.consecutive_errors = rt.consecutive_poll_errors
        info.restarts = rt.poll_restarts
        info.received = rt.stats.get("items_received", 0)
        if not logged_in:
            info.state, info.detail = "needs_login", "需要在后台扫码登录微信"
        elif not self.is_running():
            info.state, info.detail = "stopped", "没有在接收消息"
        elif rt.consecutive_poll_errors:
            info.state, info.detail = "reconnecting", "接收出错，正在重试"
        else:
            info.state, info.detail = "connected", "已连接"
        return info
