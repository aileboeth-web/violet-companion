"""OneBot v11 channel (phase 10): QQ through NapCat / Lagrange / go-cqhttp style implementations.

Two directions, both plain HTTP:
  * events -> Violet: the OneBot implementation POSTs every event to Violet's webhook
    ``/channels/hook/<channel id>`` (configure it as an "HTTP POST" / "HTTP 客户端" report URL). The body must be
    signed with the channel's ``webhook_secret`` (``X-Signature: sha1=<HMAC-SHA1 of the body>``, the OneBot v11 rule);
    unsigned or wrongly signed requests are refused before anything is parsed. By default only connections from this
    machine are accepted (``webhook_loopback_only``).
  * Violet -> QQ: the OneBot HTTP API at ``api_base`` (``send_private_msg``), with ``access_token`` as Bearer token.

Only private messages are handled (``message_type == "private"``); group messages, notices and requests are ignored.
Messages are read as SEGMENTS (array format); a message in string format is parsed into segments first -- a CQ code
such as ``[CQ:face,id=14]`` or ``[CQ:image,file=...]`` is never handed to the model as text.

The receive "loop" of this adapter only checks the API every ``check_interval`` seconds (``get_login_info``) so the
Control Center can show whether the QQ side is reachable; messages arrive through the webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from ai.channels.base import ChannelAdapter, InboundMessage, NeedsLogin, Part
from ai.channels.normalize import clean_text, coalesce, qq_face_name, safe_error

MAX_DOWNLOAD = 10 * 1024 * 1024
_CQ = re.compile(r"\[CQ:([a-zA-Z0-9_.-]+)((?:,[^\],]*)*)\]")


class OneBotError(RuntimeError):
    pass


def _unescape(text: str, in_param: bool = False) -> str:
    text = text.replace("&#91;", "[").replace("&#93;", "]")
    if in_param:
        text = text.replace("&#44;", ",")
    return text.replace("&amp;", "&")


def parse_cq_string(message: str) -> list[dict[str, Any]]:
    """A OneBot string-format message as array-format segments."""
    segments, pos = [], 0
    for match in _CQ.finditer(message or ""):
        if match.start() > pos:
            segments.append({"type": "text", "data": {"text": _unescape(message[pos:match.start()])}})
        data = {}
        for item in (match.group(2) or "").split(",")[1:]:
            if "=" in item:
                key, value = item.split("=", 1)
                data[key] = _unescape(value, in_param=True)
        segments.append({"type": match.group(1), "data": data})
        pos = match.end()
    if pos < len(message or ""):
        segments.append({"type": "text", "data": {"text": _unescape(message[pos:])}})
    return segments


def segment_parts(segments: list[dict[str, Any]]) -> list[Part]:
    parts: list[Part] = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        kind, data = str(seg.get("type") or ""), seg.get("data") or {}
        if kind == "text":
            if data.get("text"):
                parts.append(Part("text", str(data["text"])))
        elif kind == "face":
            parts.append(Part("emoji", qq_face_name(data.get("id")) or "", {"platform_label": "QQ"}))
        elif kind in ("mface", "marketface"):
            parts.append(Part("sticker", str(data.get("summary") or "").strip("[]")))
        elif kind == "image":
            # a "summary" of [动画表情] marks a sticker picture; it is still a picture Violet can look at
            parts.append(Part("image", "", {"url": str(data.get("url") or ""), "file": str(data.get("file") or "")}))
        elif kind == "record":
            parts.append(Part("voice", ""))
        elif kind == "video":
            parts.append(Part("video", ""))
        elif kind == "file":
            parts.append(Part("file", str(data.get("name") or data.get("file") or "")))
        elif kind == "at":
            parts.append(Part("mention", str(data.get("name") or "")))
        elif kind == "reply":
            parts.append(Part("quote", ""))
        elif kind == "location":
            parts.append(Part("location", str(data.get("title") or data.get("content") or "")))
        elif kind == "share":
            parts.append(Part("link", str(data.get("title") or ""), {"url": str(data.get("url") or "")}))
        else:                                   # json / xml cards, poke, dice, rps, forward, contact, music, ...
            parts.append(Part("unsupported", ""))
    return parts


def parse_event(event: dict[str, Any], channel_id: str) -> InboundMessage | None:
    if event.get("post_type") != "message" or event.get("message_type") != "private":
        return None
    peer = str(event.get("user_id") or "")
    if not peer or str(event.get("self_id") or "") == peer:
        return None
    message = event.get("message")
    segments = parse_cq_string(message) if isinstance(message, str) else (message or [])
    parts = segment_parts(segments) or [Part("unsupported", "")]
    sender = event.get("sender") or {}
    return InboundMessage(channel=channel_id, channel_type="onebot11", peer_id=peer,
                          dedup_key=f"ob:{channel_id}:{event.get('self_id')}:{event.get('message_id')}",
                          parts=coalesce(parts), message_id=str(event.get("message_id") or ""),
                          peer_name=clean_text(str(sender.get("nickname") or ""), 60) or None)


def signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha1="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(expected, header[5:].strip().lower())


class OneBotChannel(ChannelAdapter):
    channel_type = "onebot11"
    label = "QQ"
    capabilities = frozenset({"text", "image_in", "image_out"})

    def __init__(self, channel_id: str, config: dict[str, Any] | None = None, secrets: dict[str, str] | None = None,
                 data_dir: str | os.PathLike[str] | None = None, session: requests.Session | None = None, **kwargs):
        super().__init__(channel_id, config, secrets, **kwargs)
        self.api_base = str(self.config.get("api_base") or "http://127.0.0.1:3000").rstrip("/")
        self.session = session or requests.Session()
        self.session.trust_env = not _is_loopback_url(self.api_base)     # never send a local API through a proxy
        base = Path(data_dir or os.environ.get("VIOLET_DATA_DIR") or "./data")
        self.media_dir = base / "media" / "channels" / channel_id

    def _call(self, action: str, params: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
        headers = {}
        if self.secrets.get("access_token"):
            headers["Authorization"] = f"Bearer {self.secrets['access_token']}"
        try:
            response = self.session.post(f"{self.api_base}/{action}", json=params or {}, headers=headers,
                                         timeout=(5, timeout))
        except requests.RequestException as exc:
            raise OneBotError(safe_error(exc)) from None
        if response.status_code in (401, 403):
            raise NeedsLogin("OneBot 接口拒绝了访问令牌（access_token）")
        try:
            body = response.json()
        except ValueError:
            raise OneBotError(f"HTTP {response.status_code}: not JSON") from None
        if body.get("status") == "failed" or body.get("retcode") not in (0, None):
            raise OneBotError(f"retcode={body.get('retcode')} {clean_text(str(body.get('wording') or body.get('msg') or ''), 120)}")
        return body.get("data")

    # receiving happens in the webhook; the loop only watches whether the API answers
    def prepare(self) -> None:
        info = self._call("get_login_info") or {}
        self.status_info.extra["self_id"] = str(info.get("user_id") or "") or None

    def receive_once(self) -> int:
        self._call("get_status")
        return 0

    def idle_delay(self) -> float:
        return float(self.config.get("check_interval") or 30)

    def handle_webhook(self, body: bytes, signature: str | None, remote_addr: str | None) -> tuple[int, str]:
        """(HTTP status, short reason). Never raises."""
        if self.config.get("webhook_loopback_only", True) and not _is_loopback(remote_addr):
            return 403, "only connections from this machine are accepted"
        if not signature_ok(str(self.secrets.get("webhook_secret") or ""), body, signature):
            return 401, "bad signature"
        try:
            event = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return 400, "bad json"
        if not isinstance(event, dict):
            return 400, "bad json"
        if event.get("post_type") == "meta_event":
            self._ok()
            return 204, "ok"
        message = parse_event(event, self.channel_id)
        if message is None:
            return 204, "ignored"
        if not self.deliver(message):
            return 503, "not stored"                 # the implementation retries (or the event is logged there)
        self._ok()
        return 204, "ok"

    def send_text(self, peer_id: str, text: str, client_id: str | None = None) -> dict[str, Any]:
        data = self._call("send_private_msg", {"user_id": int(peer_id),
                                               "message": [{"type": "text", "data": {"text": text}}]})
        return {"ok": True, "message_id": (data or {}).get("message_id")}

    def send_image(self, peer_id: str, path: str) -> dict[str, Any]:
        import base64
        encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        data = self._call("send_private_msg", {"user_id": int(peer_id),
                                               "message": [{"type": "image", "data": {"file": f"base64://{encoded}"}}]},
                          timeout=60)
        return {"ok": True, "message_id": (data or {}).get("message_id")}

    def download(self, part: Part) -> Path:
        url = str(part.data.get("url") or "")
        if not re.match(r"^https?://", url):
            raise OneBotError("image has no download address")
        try:
            response = self.session.get(url, timeout=(10, 60), stream=True)
            response.raise_for_status()
            self.media_dir.mkdir(parents=True, exist_ok=True)
            name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + ".jpg"
            target, size = self.media_dir / name, 0
            with open(target, "wb") as fh:
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise OneBotError("image too large")
                    fh.write(chunk)
            return target
        except requests.RequestException as exc:
            raise OneBotError(safe_error(exc)) from None

    def probe(self) -> dict[str, Any]:
        info = self._call("get_login_info") or {}
        return {"ok": True, "self_id": info.get("user_id"), "nickname": info.get("nickname")}


def _is_loopback(address: str | None) -> bool:
    try:
        return ipaddress.ip_address((address or "").split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _is_loopback_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "localhost" or _is_loopback(host)
