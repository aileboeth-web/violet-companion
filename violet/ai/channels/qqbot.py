"""QQ official bot channel (phase 10.5): QQ 开放平台机器人, AppID + AppSecret, official API over WebSocket.

This is the QQ access that works without any third-party client (the same protocol the official qq-botpy SDK uses);
OneBot v11 (onebot11.py, SnowLuma and similar) stays as a separate, optional channel.

  * credentials: AppID (config) + AppSecret (secret) -> ``POST https://bots.qq.com/app/getAppAccessToken`` gives an
    access token (about 2 hours; renewed a minute before it ends, and at once when the API answers 401);
  * receiving: ``GET /gateway`` -> WebSocket: Hello (heartbeat interval) -> Identify with the C2C / group intent
    (1 << 25) -> heartbeats with the last sequence number -> ``C2C_MESSAGE_CREATE`` events (private messages to the bot).
    After a disconnect the session is RESUMED (op 6) with the last sequence, so events in between are delivered;
    "invalid session" starts a new one. Group @ messages are ignored -- Violet talks with one person;
  * sending: ``POST /v2/users/{openid}/messages``. QQ allows a PASSIVE reply (with the inbound ``msg_id``) for 60
    minutes, at most 5 replies per inbound message; ``msg_seq`` numbers them and QQ drops a repeated (msg_id, msg_seq),
    so a retry of the same outbox row (same client_id -> same seq) can never arrive twice. Outside that window the
    message is sent as an ACTIVE message, which QQ limits strictly (it may be refused: the error says so plainly and
    the Control Center's diagnosis explains it). The last inbound message per user is kept in
    <data_dir>/channels/<id>.json so a restart keeps the window;
  * message content: QQ face markup (``<faceType=1,faceId="13",ext="...">``) becomes an emoji part, attachments become
    image / voice (with QQ's own transcript when given) / video / file parts -- never raw markup to the model.
  * sandbox: ``sandbox: true`` uses QQ's sandbox API (for a bot still under review).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests

from ai.channels.base import ChannelAdapter, InboundMessage, NeedsLogin, Part
from ai.channels.normalize import clean_text, coalesce, qq_face_name, safe_error

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
INTENT_GROUP_AND_C2C = 1 << 25
PASSIVE_WINDOW = 55 * 60              # QQ: 60 minutes; a margin for clock skew and the send itself
PASSIVE_MAX_REPLIES = 5
MAX_DOWNLOAD = 10 * 1024 * 1024
APPID_RE = re.compile(r"^\d{6,20}$")
DUPLICATE_CODES = {40054005}          # "消息被去重": this msg_seq was already sent for this msg_id
_FACE = re.compile(r"<faceType=\d+,faceId=\"?(\d+)\"?(?:,ext=\"([^\"]*)\")?>")
_MENTION = re.compile(r"<@!?[A-Za-z0-9_-]+>")
# op codes
OP_DISPATCH, OP_HEARTBEAT, OP_IDENTIFY, OP_RESUME, OP_RECONNECT, OP_INVALID, OP_HELLO, OP_ACK = 0, 1, 2, 6, 7, 9, 10, 11


class QQBotError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


def _face_name(face_id: str, ext: str | None) -> str:
    if ext:
        try:
            data = json.loads(base64.b64decode(ext + "=" * (-len(ext) % 4)).decode("utf-8"))
            text = str(data.get("text") or "").strip("[]/ ")
            if text:
                return clean_text(text, 20)
        except (ValueError, UnicodeDecodeError):
            pass
    return qq_face_name(face_id) or ""


def content_parts(content: str) -> list[Part]:
    """QQ message text with face markup and mentions as parts (never the markup itself)."""
    parts: list[Part] = []
    text = _MENTION.sub("", content or "")
    pos = 0
    for match in _FACE.finditer(text):
        if match.start() > pos:
            parts.append(Part("text", text[pos:match.start()]))
        parts.append(Part("emoji", _face_name(match.group(1), match.group(2)), {"platform_label": "QQ"}))
        pos = match.end()
    if pos < len(text):
        parts.append(Part("text", text[pos:]))
    return [p for p in parts if p.kind != "text" or p.text.strip()]


def attachment_parts(attachments: list[dict[str, Any]] | None) -> list[Part]:
    parts = []
    for item in attachments or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("content_type") or "").lower()
        url = str(item.get("url") or "")
        if url.startswith("//"):
            url = "https:" + url
        if kind.startswith("image"):
            parts.append(Part("image", "", {"url": url}))
        elif kind in ("voice", "audio") or kind.startswith("audio"):
            parts.append(Part("voice", str(item.get("asr_refer_text") or "")))
        elif kind.startswith("video"):
            parts.append(Part("video", ""))
        elif kind:
            parts.append(Part("file", str(item.get("filename") or "")))
    return parts


def parse_c2c(event: dict[str, Any], channel_id: str) -> InboundMessage | None:
    msg_id = str(event.get("id") or "")
    peer = str((event.get("author") or {}).get("user_openid") or "")
    if not msg_id or not peer:
        return None
    parts = content_parts(str(event.get("content") or "")) + attachment_parts(event.get("attachments"))
    if not parts:
        parts = [Part("unsupported", "")]
    return InboundMessage(channel=channel_id, channel_type="qqbot", peer_id=peer, dedup_key=f"qqbot:{channel_id}:{msg_id}",
                          parts=coalesce(parts), message_id=msg_id)


class QQBotChannel(ChannelAdapter):
    channel_type = "qqbot"
    label = "QQ"
    capabilities = frozenset({"text", "image_in", "image_out", "voice_text"})

    def __init__(self, channel_id: str, config: dict[str, Any] | None = None, secrets: dict[str, str] | None = None,
                 data_dir: str | os.PathLike[str] | None = None, session: requests.Session | None = None,
                 token_url: str | None = None, api_base: str | None = None, ws_factory=None, **kwargs):
        super().__init__(channel_id, config, secrets, **kwargs)
        self.app_id = str(self.config.get("app_id") or "")
        self.api_base = (api_base or (SANDBOX_API_BASE if self.config.get("sandbox") else API_BASE)).rstrip("/")
        self.token_url = token_url or TOKEN_URL
        self.session = session or requests.Session()
        if ws_factory is None:
            from ai.channels.wsclient import WebSocket
            ws_factory = WebSocket.connect
        self.ws_factory = ws_factory
        base = Path(data_dir or os.environ.get("VIOLET_DATA_DIR") or "./data")
        self.state_path = base / "channels" / f"{channel_id}.json"
        self.media_dir = base / "media" / "channels" / channel_id
        self._token: str | None = None
        self._token_until = 0.0
        self._token_lock = threading.Lock()
        self._ws = None
        self._heartbeat_every = 40.0
        self._next_heartbeat = 0.0
        self._acked = True
        self._seq: int | None = None
        self._session_id: str | None = None
        self._state_lock = threading.Lock()
        self._sent_seq: dict[str, tuple[str | None, int]] = {}      # client_id -> (msg_id, msg_seq) for retries
        self._load_state()

    # -- persisted state (last inbound per user, resume data) --------------------------------------------------------
    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self._peers: dict[str, dict[str, Any]] = dict(data.get("peers") or {})
        self._session_id = data.get("session_id")
        self._seq = data.get("seq")

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        with self._state_lock:
            data = {"peers": self._peers, "session_id": self._session_id, "seq": self._seq}
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.state_path)

    # -- token and HTTP ------------------------------------------------------------------------------------------
    def access_token(self, force: bool = False) -> str:
        secret = str(self.secrets.get("app_secret") or "")
        if not self.app_id or not secret:
            raise NeedsLogin("还没有填写 AppID 和 AppSecret")
        with self._token_lock:
            if not force and self._token and time.time() < self._token_until - 60:
                return self._token
            try:
                response = self.session.post(self.token_url, json={"appId": self.app_id, "clientSecret": secret},
                                             timeout=(10, 20))
                body = response.json()
            except requests.RequestException as exc:
                raise QQBotError(safe_error(exc)) from None
            except ValueError:
                raise QQBotError(f"token: HTTP {response.status_code}: not JSON", status=response.status_code) from None
            token = body.get("access_token")
            if not token:
                # wrong AppID / AppSecret: wait for the admin, do not hammer QQ
                raise NeedsLogin("AppID 或 AppSecret 不正确（QQ 没有给出访问令牌：" +
                                 clean_text(str(body.get("message") or body.get("code") or ""), 80) + "）")
            self._token = str(token)
            self._token_until = time.time() + float(body.get("expires_in") or 7200)
            return self._token

    def _headers(self, force: bool = False) -> dict[str, str]:
        return {"Authorization": f"QQBot {self.access_token(force)}", "X-Union-Appid": self.app_id}

    def _api(self, method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
        for attempt in (0, 1):
            try:
                response = self.session.request(method, self.api_base + path, json=payload,
                                                headers=self._headers(force=attempt == 1), timeout=(10, timeout))
            except requests.RequestException as exc:
                raise QQBotError(safe_error(exc)) from None
            if response.status_code == 401 and attempt == 0:
                continue                                     # token ended early: one new token, one retry
            try:
                body = response.json() if response.content else {}
            except ValueError:
                body = {}
            if response.status_code >= 400 or (isinstance(body, dict) and body.get("code") not in (None, 0)):
                code = body.get("code") if isinstance(body, dict) else None
                message = clean_text(str((body or {}).get("message") or ""), 160) if isinstance(body, dict) else ""
                if response.status_code == 401:
                    raise NeedsLogin("QQ 拒绝了访问令牌（AppID / AppSecret 可能已更换）")
                raise QQBotError(f"HTTP {response.status_code} code={code} {message}".strip(), code, response.status_code)
            return body
        raise QQBotError("unreachable")

    # -- receiving ---------------------------------------------------------------------------------------------------
    def prepare(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        gateway = self._api("GET", "/gateway") or {}
        url = str(gateway.get("url") or "")
        if not url.startswith(("wss://", "ws://")):
            raise QQBotError("QQ 没有返回网关地址")
        self._ws = self.ws_factory(url, timeout=15)
        hello = self._read_json(timeout=15)
        if not hello or hello.get("op") != OP_HELLO:
            raise QQBotError("网关没有发送 Hello")
        self._heartbeat_every = max(5.0, float((hello.get("d") or {}).get("heartbeat_interval") or 41250) / 1000.0)
        token = f"QQBot {self.access_token()}"
        if self._session_id and self._seq is not None:
            self._send({"op": OP_RESUME, "d": {"token": token, "session_id": self._session_id, "seq": self._seq}})
        else:
            self._send({"op": OP_IDENTIFY, "d": {"token": token, "intents": INTENT_GROUP_AND_C2C, "shard": [0, 1],
                                                 "properties": {"$os": os.name, "$browser": "violet",
                                                                "$device": "violet"}}})
        self._acked = True
        self._next_heartbeat = time.monotonic() + self._heartbeat_every
        # the first answer: READY / RESUMED (or invalid session)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            frame = self._read_json(timeout=max(0.5, deadline - time.monotonic()))
            if frame is None:
                continue
            if frame.get("op") == OP_INVALID:
                self._session_id, self._seq = None, None
                self._save_state()
                raise QQBotError("会话无效，重新连接")
            if frame.get("op") == OP_DISPATCH and frame.get("t") in ("READY", "RESUMED"):
                if frame.get("t") == "READY":
                    data = frame.get("d") or {}
                    self._session_id = data.get("session_id")
                    self.status_info.extra["bot"] = clean_text(str((data.get("user") or {}).get("username") or ""), 40)
                self._track(frame)
                self._save_state()
                return
            self._handle(frame)
        raise QQBotError("网关没有确认登录（READY）")

    def _send(self, frame: dict[str, Any]) -> None:
        self._ws.send_text(json.dumps(frame))

    def _read_json(self, timeout: float) -> dict[str, Any] | None:
        text = self._ws.recv(timeout=timeout)
        if text is None:
            return None
        try:
            frame = json.loads(text)
        except ValueError:
            return None
        return frame if isinstance(frame, dict) else None

    def _track(self, frame: dict[str, Any]) -> None:
        if frame.get("s") is not None:
            self._seq = int(frame["s"])

    def idle_delay(self) -> float:
        return 0.0                                       # recv() already waits until the next heartbeat

    def receive_once(self) -> int:
        now = time.monotonic()
        if now >= self._next_heartbeat:
            if not self._acked:
                raise QQBotError("心跳没有回应，重新连接")
            self._acked = False
            self._send({"op": OP_HEARTBEAT, "d": self._seq})
            self._next_heartbeat = now + self._heartbeat_every
        frame = self._read_json(timeout=max(0.2, self._next_heartbeat - time.monotonic()))
        if frame is None:
            return 0
        return self._handle(frame)

    def _handle(self, frame: dict[str, Any]) -> int:
        op = frame.get("op")
        if op == OP_ACK:
            self._acked = True
            return 0
        if op == OP_RECONNECT:
            raise QQBotError("网关要求重新连接")
        if op == OP_INVALID:
            self._session_id, self._seq = None, None
            self._save_state()
            raise QQBotError("会话无效，重新连接")
        if op != OP_DISPATCH:
            return 0
        self._track(frame)
        if frame.get("t") != "C2C_MESSAGE_CREATE":
            self._save_state()
            return 0                                     # group @ messages and other events: Violet talks with one person
        event = frame.get("d") or {}
        message = parse_c2c(event, self.channel_id)
        if message is None:
            return 0
        stored = self.deliver(message)
        if stored:
            with self._state_lock:
                self._peers[message.peer_id] = {"msg_id": message.message_id, "at": time.time(), "replies": 0}
        else:
            # the gateway does not redeliver; the event id is logged so the loss is visible
            self.status_info.extra["lost_events"] = int(self.status_info.extra.get("lost_events") or 0) + 1
        self._save_state()
        return 1 if stored else 0

    def on_stop(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    # -- sending -------------------------------------------------------------------------------------------------------
    def _reply_ref(self, peer_id: str, client_id: str | None) -> tuple[str | None, int]:
        """(msg_id or None for an active message, msg_seq). The same client_id always gets the same pair."""
        with self._state_lock:
            if client_id and client_id in self._sent_seq:
                return self._sent_seq[client_id]
            last = self._peers.get(peer_id) or {}
            ref: tuple[str | None, int]
            if last.get("msg_id") and time.time() - float(last.get("at") or 0) < PASSIVE_WINDOW \
                    and int(last.get("replies") or 0) < PASSIVE_MAX_REPLIES:
                last["replies"] = int(last.get("replies") or 0) + 1
                ref = (str(last["msg_id"]), last["replies"])
            else:
                ref = (None, 1)
            if client_id:
                self._sent_seq[client_id] = ref
            return ref

    def send_text(self, peer_id: str, text: str, client_id: str | None = None) -> dict[str, Any]:
        msg_id, seq = self._reply_ref(peer_id, client_id)
        payload: dict[str, Any] = {"content": text[:2000], "msg_type": 0, "msg_seq": seq}
        if msg_id:
            payload["msg_id"] = msg_id
        try:
            body = self._api("POST", f"/v2/users/{peer_id}/messages", payload)
        except QQBotError as exc:
            if msg_id is not None and exc.code in DUPLICATE_CODES:
                # the same (msg_id, msg_seq) arrived before: an earlier attempt of this outbox row was delivered
                return {"ok": True, "message_id": None, "passive": True, "duplicate": True}
            if msg_id is None:
                # an active message: QQ limits these strictly -- say so instead of a bare error code
                raise QQBotError(f"QQ 拒绝了主动消息（对方超过 60 分钟没有发消息，官方机器人只能有限地主动发送）：{exc}",
                                 exc.code, exc.status) from None
            raise
        finally:
            self._save_state()
        return {"ok": True, "message_id": (body or {}).get("id"), "passive": msg_id is not None}

    def send_image(self, peer_id: str, path: str) -> dict[str, Any]:
        """2026-10-29: a picture = upload it as rich media (POST /v2/users/{openid}/files, file_type 1, the bytes as
        file_data), then a media message (msg_type 7) with the returned file_info -- QQ's documented two steps."""
        import base64
        with open(path, "rb") as fh:
            data = fh.read()
        if len(data) > MAX_DOWNLOAD:
            raise QQBotError("image too large")
        uploaded = self._api("POST", f"/v2/users/{peer_id}/files",
                             {"file_type": 1, "file_data": base64.b64encode(data).decode("ascii"), "srv_send_msg": False},
                             timeout=60) or {}
        file_info = uploaded.get("file_info") if isinstance(uploaded, dict) else None
        if not file_info:
            raise QQBotError("QQ 没有返回图片的 file_info")
        msg_id, seq = self._reply_ref(peer_id, None)
        payload: dict[str, Any] = {"content": " ", "msg_type": 7, "media": {"file_info": file_info}, "msg_seq": seq}
        if msg_id:
            payload["msg_id"] = msg_id
        try:
            body = self._api("POST", f"/v2/users/{peer_id}/messages", payload)
        finally:
            self._save_state()
        return {"ok": True, "message_id": (body or {}).get("id"), "passive": msg_id is not None}

    def download(self, part: Part) -> Path:
        url = str(part.data.get("url") or "")
        if not re.match(r"^https://", url):
            raise QQBotError("image has no https download address")
        try:
            response = self.session.get(url, timeout=(10, 60), stream=True)
            response.raise_for_status()
            self.media_dir.mkdir(parents=True, exist_ok=True)
            target = self.media_dir / (hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + ".jpg")
            size = 0
            with open(target, "wb") as fh:
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise QQBotError("image too large")
                    fh.write(chunk)
            return target
        except requests.RequestException as exc:
            raise QQBotError(safe_error(exc)) from None

    def probe(self) -> dict[str, Any]:
        """"测试连接": a token and the gateway address (no message is sent)."""
        self.access_token(force=True)
        gateway = self._api("GET", "/gateway") or {}
        return {"ok": bool(gateway.get("url")), "gateway": "ok" if gateway.get("url") else "missing",
                "sandbox": bool(self.config.get("sandbox"))}
