"""The channel "QQ" (2026-10-29): a personal QQ account through SnowLuma (OneBot v11; NapCat until the user found its
risk control too strict), installed by Violet itself (tools/violet_snowluma.py, the channels page's one-click install).
The channel keeps its stored type and id "napcat" from 0.7.8, so bindings, groups and history stay as they are.

One local WebSocket (SnowLuma's "wsServers" entry, published on 127.0.0.1 only, token) carries both directions: events
come in as JSON, actions go out as {"action", "params", "echo"} and their answers come back with the same echo. The
receive loop is the only reader; a sender waits for its echo. SnowLuma opens it only once QQ is logged in.

Who gets what:
  * a private message from the owner (a bound account, or "/bind <code>") -> the sink: Violet's normal one-to-one chat,
    exactly like WeChat / Telegram;
  * everything else -- group messages, private messages of other people -> Violet's social life (ai/social/engine.py),
    which stores, decides and answers on its own. Nothing of it reaches the owner's chat or memories.

The QR login happens in QQ's own window (ai/channels/vnc.py), not through here; probe_login() tells whether it is done.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests

from ai.channels.base import ChannelAdapter, InboundMessage, NeedsLogin, Part
from ai.channels.normalize import clean_text, coalesce, safe_error
from ai.channels.onebot11 import parse_cq_string, segment_parts

MAX_DOWNLOAD = 10 * 1024 * 1024


class QQError(RuntimeError):
    pass


def account_token(config_dir: str | None) -> str | None:
    """The token SnowLuma really uses for the logged-in account: its config/onebot_<uin>.json (the newest one), the
    WebSocket server on the container's port 3001. None when there is none or the folder cannot be read."""
    if not config_dir or not Path(config_dir).is_absolute():
        return None
    try:
        files = sorted(Path(config_dir).glob("onebot_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            servers = (data.get("networks") or {}).get("wsServers") or []
        except (OSError, ValueError, AttributeError):
            continue
        for server in servers if isinstance(servers, list) else []:
            if isinstance(server, dict) and server.get("enabled", True) and str(server.get("port")) == "3001" \
                    and server.get("accessToken"):
                return str(server["accessToken"])
    return None


def connection() -> dict[str, Any] | None:
    """Where SnowLuma listens and its token, the VNC of QQ's window: what the installer exported
    (<data>/control/snowluma.json), with the account file's token when SnowLuma wrote its own."""
    from ai.root_tasks import snowluma_export
    info = snowluma_export()
    if info is None:
        return None
    token = account_token(info.get("config_dir"))
    return {**info, "ws_token": token} if token else info


def probe_login(info: dict[str, Any], ws_factory=None, timeout: float = 4.0) -> dict[str, Any] | None:
    """{"uin", "nick"} when QQ is logged in (SnowLuma answers get_login_info on its WebSocket), else None."""
    if ws_factory is None:
        from ai.channels.wsclient import WebSocket
        ws_factory = WebSocket.connect
    token = str(info.get("ws_token") or "")
    try:
        ws = ws_factory(info["ws_url"], timeout=timeout, headers={"Authorization": f"Bearer {token}"} if token else {})
    except Exception:                                    # noqa: BLE001 -- not logged in yet: nothing listens
        return None
    try:
        ws.send_text(json.dumps({"action": "get_login_info", "params": {}, "echo": "probe"}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = ws.recv(timeout=max(0.2, deadline - time.monotonic()))
            if text is None:
                continue
            try:
                frame = json.loads(text)
            except ValueError:
                continue
            if isinstance(frame, dict) and frame.get("echo") == "probe":
                data = frame.get("data") or {}
                uin = str(data.get("user_id") or "")
                return {"uin": uin, "nick": clean_text(str(data.get("nickname") or ""), 40)} if uin else None
    except Exception:                                    # noqa: BLE001
        return None
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return None


def message_text(segments: list[dict[str, Any]], self_id: str) -> tuple[str, bool, str | None]:
    """(plain text for the record, mentions her?, the platform id of a quoted message)."""
    from ai.channels.normalize import render_parts
    mentioned, reply_to, parts = False, None, []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        kind, data = seg.get("type"), seg.get("data") or {}
        if kind == "at":
            qq = str(data.get("qq") or "")
            if qq and qq == self_id:
                mentioned = True
                parts.append(Part("mention", "你"))
                continue
            if qq == "all":
                parts.append(Part("mention", "全体成员"))
                continue
        if kind == "reply" and data.get("id") is not None:
            reply_to = str(data.get("id"))
            continue
        parts.extend(segment_parts([seg]))
    return render_parts(coalesce(parts)) if parts else "", mentioned, reply_to


class SnowLumaChannel(ChannelAdapter):
    channel_type = "napcat"                    # the stored type since 0.7.8 (see the module text)
    label = "QQ"
    capabilities = frozenset({"text", "image_in", "image_out"})

    def __init__(self, channel_id: str, config: dict[str, Any] | None = None, secrets: dict[str, str] | None = None,
                 data_dir: str | os.PathLike[str] | None = None, ws_factory=None, session: requests.Session | None = None,
                 **kwargs):
        super().__init__(channel_id, config, secrets, **kwargs)
        if ws_factory is None:
            from ai.channels.wsclient import WebSocket
            ws_factory = WebSocket.connect
        self.ws_factory = ws_factory
        self.session = session or requests.Session()
        base = Path(data_dir or os.environ.get("VIOLET_DATA_DIR") or "./data")
        self.media_dir = base / "media" / "channels" / channel_id
        self.runtime = None                    # set by the channel manager: the owner check and the social engine
        self._ws = None
        self._echo = itertools.count(1)
        self._waiting: dict[str, dict[str, Any]] = {}
        self._waiting_lock = threading.Lock()
        self.self_id = ""
        self.nickname = ""

    # -- connection -------------------------------------------------------------------------------------------------
    def _target(self) -> tuple[str, str]:
        url = str(self.config.get("ws_url") or "").strip()
        token = str(self.secrets.get("ws_token") or "")
        if not url:
            info = connection()
            if not info:
                raise NeedsLogin("还没有安装 QQ（SnowLuma）：在渠道页点“一键安装”")
            url, token = info["ws_url"], str(info.get("ws_token") or "")
        return url, token

    def prepare(self) -> None:
        self.on_stop()
        url, token = self._target()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            self._ws = self.ws_factory(url, timeout=15, headers=headers)
        except Exception as exc:                        # noqa: BLE001 -- QQ still starting / not logged in yet
            raise QQError("连不上 QQ（可能还在启动，或者还没有扫码登录）：" + safe_error(exc)) from None
        info = self._call_inline("get_login_info") or {}
        self.self_id = str(info.get("user_id") or "")
        self.nickname = clean_text(str(info.get("nickname") or ""), 40)
        if not self.self_id:
            raise NeedsLogin("QQ 还没有登录：在渠道页扫码")
        self.status_info.extra.update(self_id=self.self_id, nickname=self.nickname)
        self._set_state("connected", f"已登录 {self.nickname or 'QQ'}（{self.self_id}）")
        self._social()                                   # starts the social engine: the wake-ups she had planned
        self._sync_stickers_soon()

    def _sync_stickers_soon(self, delay: float = 20.0) -> None:
        """The account's favourite stickers into her collection once connected (stickers.sync_qq; after a while,
        because the actions need the receive loop that starts when prepare() returns)."""
        db = getattr(self.runtime, "db", None)
        if db is None:
            return
        from ai import stickers
        timer = threading.Timer(delay, lambda: self.is_running() and stickers.sync_qq(db, self))
        timer.daemon = True
        timer.start()

    def on_stop(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        with self._waiting_lock:
            for slot in self._waiting.values():
                slot["error"] = "connection closed"
                slot["event"].set()

    def idle_delay(self) -> float:
        return 0.0                                      # recv() waits by itself

    def _read(self, timeout: float) -> dict[str, Any] | None:
        ws = self._ws
        if ws is None:
            raise QQError("not connected")
        text = ws.recv(timeout=timeout)
        if text is None:
            return None
        try:
            frame = json.loads(text)
        except ValueError:
            return None
        return frame if isinstance(frame, dict) else None

    def receive_once(self) -> int:
        frame = self._read(timeout=30)
        if frame is None:
            return 0
        if "echo" in frame and "post_type" not in frame:
            self._answer(frame)
            return 0
        return self.handle_event(frame)

    # -- actions --------------------------------------------------------------------------------------------------------
    def _answer(self, frame: dict[str, Any]) -> None:
        with self._waiting_lock:
            slot = self._waiting.get(str(frame.get("echo")))
        if slot is not None:
            slot["frame"] = frame
            slot["event"].set()

    def _call_inline(self, action: str, params: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
        """During prepare(): this thread is the reader, so it reads until its own answer arrives."""
        echo = f"v{next(self._echo)}"
        self._ws.send_text(json.dumps({"action": action, "params": params or {}, "echo": echo}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self._read(timeout=max(0.5, deadline - time.monotonic()))
            if frame is None:
                continue
            if str(frame.get("echo")) == echo:
                return self._result(action, frame)
            if "post_type" in frame:
                self.handle_event(frame)
        raise QQError(f"{action}: no answer")

    def call(self, action: str, params: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
        """An OneBot action from any thread; the receive loop hands back the answer."""
        ws = self._ws
        if ws is None:
            raise QQError("QQ 没有连接")
        echo = f"v{next(self._echo)}"
        slot = {"event": threading.Event()}
        with self._waiting_lock:
            self._waiting[echo] = slot
        try:
            ws.send_text(json.dumps({"action": action, "params": params or {}, "echo": echo}))
            if not slot["event"].wait(timeout):
                raise QQError(f"{action}: QQ 没有在 {timeout:.0f} 秒内回答")
            if slot.get("error"):
                raise QQError(f"{action}: {slot['error']}")
            return self._result(action, slot["frame"])
        finally:
            with self._waiting_lock:
                self._waiting.pop(echo, None)

    @staticmethod
    def _result(action: str, frame: dict[str, Any]) -> Any:
        if frame.get("status") == "failed" or frame.get("retcode") not in (0, None):
            raise QQError(f"{action}: retcode={frame.get('retcode')} "
                          + clean_text(str(frame.get("wording") or frame.get("message") or ""), 120))
        return frame.get("data")

    # -- events -----------------------------------------------------------------------------------------------------
    def handle_event(self, event: dict[str, Any]) -> int:
        post = event.get("post_type")
        if post == "meta_event":
            return 0
        if post != "message":
            return 0
        if str(event.get("user_id") or "") == self.self_id:
            return 0                                     # her own messages (sent from another device)
        message = event.get("message")
        segments = parse_cq_string(message) if isinstance(message, str) else (message or [])
        if event.get("message_type") == "private" and self._for_owner(event, segments):
            return 1 if self.deliver(self._inbound(event, segments)) else 0
        social = self._social()
        if social is None:
            return 0
        text, mentioned, reply_to = message_text(segments, self.self_id)
        images = [s.get("data") or {} for s in segments if isinstance(s, dict) and s.get("type") == "image"]
        sender = event.get("sender") or {}
        social.receive(self, kind="group" if event.get("message_type") == "group" else "private",
                       peer_id=str(event.get("group_id") if event.get("message_type") == "group" else event.get("user_id")),
                       sender_id=str(event.get("user_id") or ""),
                       sender_name=clean_text(str(sender.get("card") or sender.get("nickname") or ""), 40) or None,
                       text=text, mentioned=mentioned, reply_to=reply_to,
                       platform_msg_id=str(event.get("message_id") or "") or None, images=images,
                       group_name=clean_text(str(event.get("group_name") or ""), 60) or None)
        self.status_info.received += 1
        return 1

    def _for_owner(self, event: dict[str, Any], segments: list[dict[str, Any]]) -> bool:
        peer = str(event.get("user_id") or "")
        text = "".join(str((s.get("data") or {}).get("text") or "") for s in segments
                       if isinstance(s, dict) and s.get("type") == "text").strip()
        if text.startswith("/bind"):
            return True
        runtime = self.runtime
        identities = getattr(runtime, "identities", None)
        if identities is None:
            return True                                   # no social engine (tests): the old one-to-one behaviour
        try:
            return peer in identities.bound_peers(self.channel_id)
        except Exception:
            return False

    def _inbound(self, event: dict[str, Any], segments: list[dict[str, Any]]) -> InboundMessage:
        sender = event.get("sender") or {}
        return InboundMessage(channel=self.channel_id, channel_type=self.channel_type, peer_id=str(event.get("user_id")),
                              dedup_key=f"nc:{self.channel_id}:{event.get('self_id')}:{event.get('message_id')}",
                              parts=coalesce(segment_parts(segments)) or [Part("unsupported", "")],
                              message_id=str(event.get("message_id") or ""),
                              peer_name=clean_text(str(sender.get("nickname") or ""), 60) or None)

    def _social(self):
        if self.runtime is None:
            return None
        from ai.social.engine import engine_for
        return engine_for(self.runtime)

    def owner_ids(self) -> set[str]:
        identities = getattr(self.runtime, "identities", None)
        if identities is None:
            return set()
        try:
            return set(identities.bound_peers(self.channel_id))
        except Exception:
            return set()

    # -- sending ----------------------------------------------------------------------------------------------------
    def send_text(self, peer_id: str, text: str, client_id: str | None = None) -> dict[str, Any]:
        data = self.call("send_private_msg", {"user_id": int(peer_id), "message": [{"type": "text", "data": {"text": text}}]})
        self.status_info.sent += 1
        return {"ok": True, "message_id": (data or {}).get("message_id")}

    def send_image(self, peer_id: str, path: str) -> dict[str, Any]:
        encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        data = self.call("send_private_msg", {"user_id": int(peer_id),
                                              "message": [{"type": "image", "data": {"file": f"base64://{encoded}"}}]},
                         timeout=60)
        return {"ok": True, "message_id": (data or {}).get("message_id")}

    @staticmethod
    def _sticker_segment(path: str) -> dict[str, Any]:
        """A picture QQ shows as a sticker (small, no preview frame), like one sent from the favourites."""
        encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        return {"type": "image", "data": {"file": f"base64://{encoded}", "sub_type": 1, "summary": "[动画表情]"}}

    def send_sticker(self, peer_id: str, path: str) -> dict[str, Any]:
        data = self.call("send_private_msg", {"user_id": int(peer_id), "message": [self._sticker_segment(path)]},
                         timeout=60)
        return {"ok": True, "message_id": (data or {}).get("message_id")}

    def send_to(self, kind: str, peer_id: str, text: str | None, reply_to: str | None = None,
                sticker_path: str | None = None, image_path: str | None = None) -> str | None:
        """The social engine's send: a group or someone's private chat, optionally quoting a message; text, one
        sticker of her collection, or (2026-10-30) one picture she drew."""
        message: list[dict[str, Any]] = []
        if reply_to:
            message.append({"type": "reply", "data": {"id": str(reply_to)}})
        if sticker_path:
            message.append(self._sticker_segment(sticker_path))
        elif image_path:
            encoded = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
            message.append({"type": "image", "data": {"file": f"base64://{encoded}"}})
        else:
            message.append({"type": "text", "data": {"text": text or ""}})
        timeout = 60 if (sticker_path or image_path) else None
        extra = {"timeout": timeout} if timeout else {}
        if kind == "group":
            data = self.call("send_group_msg", {"group_id": int(peer_id), "message": message}, **extra)
        else:
            data = self.call("send_private_msg", {"user_id": int(peer_id), "message": message}, **extra)
        self.status_info.sent += 1
        return str((data or {}).get("message_id") or "") or None

    def groups(self) -> list[dict[str, Any]]:
        rows = self.call("get_group_list", {}) or []
        return [{"group_id": str(g.get("group_id")), "group_name": clean_text(str(g.get("group_name") or ""), 60),
                 "member_count": g.get("member_count")} for g in rows if isinstance(g, dict) and g.get("group_id")]

    def favourite_stickers(self, count: int = 48) -> list[str]:
        """The addresses of the account's own favourite stickers (QQ's 收藏的表情)."""
        rows = self.call("fetch_custom_face", {"count": int(count), "return_type": "url"}, timeout=30) or []
        if isinstance(rows, dict):
            rows = rows.get("urls") or rows.get("data") or []
        return [str(u) for u in rows if isinstance(u, str) and re.match(r"^https?://", u)]

    def publish_qzone(self, text: str) -> str:
        """A QQ 空间 post ("说说") visible to everyone, through SnowLuma's own send_qzone_msg. Returns its id."""
        data = self.call("send_qzone_msg", {"content": text, "ugc_right": 1}, timeout=60) or {}
        return str(data.get("tid") or "") if isinstance(data, dict) else ""

    def download(self, part: Part) -> Path:
        url = str(part.data.get("url") or "")
        if not re.match(r"^https?://", url):
            raise QQError("image has no download address")
        try:
            response = self.session.get(url, timeout=(10, 60), stream=True)
            response.raise_for_status()
            self.media_dir.mkdir(parents=True, exist_ok=True)
            target, size = self.media_dir / (hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + ".jpg"), 0
            with open(target, "wb") as fh:
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise QQError("image too large")
                    fh.write(chunk)
            return target
        except requests.RequestException as exc:
            raise QQError(safe_error(exc)) from None

    def probe(self) -> dict[str, Any]:
        info = self.call("get_login_info") or {}
        return {"ok": bool(info.get("user_id")), "self_id": info.get("user_id"), "nickname": info.get("nickname")}
