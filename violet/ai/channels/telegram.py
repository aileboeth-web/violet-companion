"""Telegram Bot API channel (phase 10): long polling, private chats only.

Configuration (Control Center "聊天渠道"): ``api_base`` (default https://api.telegram.org; a self-hosted Bot API
server or a test server may be used -- https, or http only on this machine), ``poll_timeout`` (seconds of long
polling), ``proxy`` (optional http(s) proxy URL). Secret: ``bot_token`` (from @BotFather).

Receiving: ``getUpdates`` with an offset that is advanced only after every message of the batch was stored (the
offset is kept in <data_dir>/channels/<id>.json, so a restart neither loses nor repeats messages). Group, channel and
inline updates are ignored -- Violet talks with one person. The token appears in Telegram's URLs; every error text that
leaves this module is scrubbed (normalize.safe_error).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import requests

from ai.channels.base import ChannelAdapter, InboundMessage, NeedsLogin, Part
from ai.channels.normalize import clean_text, safe_error

TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
MAX_DOWNLOAD = 10 * 1024 * 1024


class TelegramError(RuntimeError):
    def __init__(self, message: str, retry_after: float | None = None, status: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status


class TelegramChannel(ChannelAdapter):
    channel_type = "telegram"
    label = "Telegram"
    capabilities = frozenset({"text", "image_in", "image_out", "typing"})

    def __init__(self, channel_id: str, config: dict[str, Any] | None = None, secrets: dict[str, str] | None = None,
                 data_dir: str | os.PathLike[str] | None = None, session: requests.Session | None = None, **kwargs):
        super().__init__(channel_id, config, secrets, **kwargs)
        self.api_base = str(self.config.get("api_base") or "https://api.telegram.org").rstrip("/")
        self.poll_timeout = int(self.config.get("poll_timeout") or 25)
        self.session = session or requests.Session()
        proxy = str(self.config.get("proxy") or "").strip()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        base = Path(data_dir or os.environ.get("VIOLET_DATA_DIR") or "./data")
        self.state_path = base / "channels" / f"{channel_id}.json"
        self.media_dir = base / "media" / "channels" / channel_id
        self.bot_username: str | None = None

    # -- HTTP ---------------------------------------------------------------------------------------------------
    @property
    def token(self) -> str:
        return str(self.secrets.get("bot_token") or "")

    def _call(self, method: str, payload: dict[str, Any] | None = None, timeout: float = 20.0,
              files: dict[str, Any] | None = None) -> Any:
        if not self.token:
            raise NeedsLogin("还没有填写 Bot Token")
        url = f"{self.api_base}/bot{self.token}/{method}"
        try:
            if files:
                response = self.session.post(url, data=payload or {}, files=files, timeout=(10, timeout))
            else:
                response = self.session.post(url, json=payload or {}, timeout=(10, timeout))
        except requests.RequestException as exc:
            raise TelegramError(safe_error(exc)) from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(f"HTTP {response.status_code}: not JSON", status=response.status_code) from None
        if not body.get("ok"):
            params = body.get("parameters") or {}
            description = clean_text(str(body.get("description") or ""), 200)
            if response.status_code == 401:
                raise NeedsLogin("Bot Token 无效或已经失效")
            raise TelegramError(f"HTTP {response.status_code}: {description}", params.get("retry_after"),
                                response.status_code)
        return body.get("result")

    # -- state ----------------------------------------------------------------------------------------------------
    def _offset(self) -> int:
        try:
            return int(json.loads(self.state_path.read_text(encoding="utf-8")).get("offset") or 0)
        except (OSError, ValueError):
            return 0

    def _save_offset(self, offset: int) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"offset": offset}), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # -- receiving ------------------------------------------------------------------------------------------------
    def prepare(self) -> None:
        me = self._call("getMe", timeout=15)
        self.bot_username = str((me or {}).get("username") or "") or None
        self.status_info.extra["bot"] = self.bot_username

    def idle_delay(self) -> float:
        return 0.0                                 # long polling already waits

    def receive_once(self) -> int:
        offset = self._offset()
        updates = self._call("getUpdates", {"offset": offset, "timeout": self.poll_timeout,
                                            "allowed_updates": ["message"]}, timeout=self.poll_timeout + 15)
        handled = 0
        for update in updates or []:
            update_id = int(update.get("update_id") or 0)
            message = parse_update(update, self.channel_id)
            if message is not None:
                if not self.deliver(message):
                    break                          # not stored: keep the offset, Telegram delivers it again
                handled += 1
            offset = max(offset, update_id + 1)
            self._save_offset(offset)
        return handled

    # -- sending --------------------------------------------------------------------------------------------------
    def send_text(self, peer_id: str, text: str, client_id: str | None = None) -> dict[str, Any]:
        result = self._call("sendMessage", {"chat_id": peer_id, "text": text[:4096]})
        return {"ok": True, "message_id": (result or {}).get("message_id")}

    def send_image(self, peer_id: str, path: str) -> dict[str, Any]:
        with open(path, "rb") as fh:
            result = self._call("sendPhoto", {"chat_id": peer_id}, timeout=60, files={"photo": fh})
        return {"ok": True, "message_id": (result or {}).get("message_id")}

    def send_typing(self, peer_id: str, on: bool) -> None:
        if on and peer_id:
            self._call("sendChatAction", {"chat_id": peer_id, "action": "typing"}, timeout=10)

    def download(self, part: Part) -> Path:
        file_id = part.data.get("file_id")
        if not file_id:
            raise TelegramError("no file id")
        info = self._call("getFile", {"file_id": file_id}) or {}
        file_path = str(info.get("file_path") or "")
        if not file_path or ".." in file_path or int(info.get("file_size") or 0) > MAX_DOWNLOAD:
            raise TelegramError("file not downloadable")
        url = f"{self.api_base}/file/bot{self.token}/{file_path}"
        try:
            response = self.session.get(url, timeout=(10, 60), stream=True)
            response.raise_for_status()
            self.media_dir.mkdir(parents=True, exist_ok=True)
            suffix = Path(file_path).suffix[:8] or ".jpg"
            target = self.media_dir / f"{part.data.get('dedup_key', 'x').replace(':', '_')}{suffix}"
            size = 0
            with open(target, "wb") as fh:
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise TelegramError("file too large")
                    fh.write(chunk)
            return target
        except requests.RequestException as exc:
            raise TelegramError(safe_error(exc)) from None

    def probe(self) -> dict[str, Any]:
        """For the Control Center's "测试连接": who the bot is (no message is sent)."""
        me = self._call("getMe", timeout=15) or {}
        return {"ok": True, "bot": me.get("username"), "name": me.get("first_name")}


def parse_update(update: dict[str, Any], channel_id: str) -> InboundMessage | None:
    """A private-chat message as parts; anything else (groups, channels, edits, service messages) -> None."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if chat.get("type") != "private" or sender.get("is_bot"):
        return None
    peer = str(chat.get("id") or sender.get("id") or "")
    if not peer:
        return None
    parts: list[Part] = []
    reply = message.get("reply_to_message") or {}
    if reply.get("text") or reply.get("caption"):
        parts.append(Part("quote", str(reply.get("text") or reply.get("caption"))))
    text = message.get("text")
    if isinstance(text, str) and text:
        parts.append(Part("text", text))
    if message.get("photo"):
        sizes = [p for p in message["photo"] if isinstance(p, dict) and p.get("file_id")]
        if sizes:
            biggest = max(sizes, key=lambda p: int(p.get("file_size") or p.get("width", 0) or 0))
            parts.append(Part("image", "", {"file_id": biggest["file_id"]}))
    if message.get("sticker"):
        sticker = message["sticker"]
        parts.append(Part("sticker", str(sticker.get("emoji") or "")))
    if message.get("voice") or message.get("audio"):
        parts.append(Part("voice", ""))
    if message.get("video") or message.get("video_note") or message.get("animation"):
        parts.append(Part("video", ""))
    if message.get("document") and not message.get("animation"):
        parts.append(Part("file", str((message["document"] or {}).get("file_name") or "")))
    if message.get("venue"):
        parts.append(Part("location", str((message["venue"] or {}).get("title") or "")))
    elif message.get("location"):
        parts.append(Part("location", ""))
    caption = message.get("caption")
    if isinstance(caption, str) and caption:
        parts.append(Part("text", caption))
    if not parts:
        parts.append(Part("unsupported", ""))
    from ai.channels.normalize import coalesce
    return InboundMessage(channel=channel_id, channel_type="telegram", peer_id=peer,
                          dedup_key=f"tg:{channel_id}:{update.get('update_id')}", parts=coalesce(parts),
                          message_id=str(message.get("message_id") or ""),
                          peer_name=clean_text(" ".join(str(sender.get(k) or "") for k in ("first_name", "last_name")),
                                               60).strip() or None)
