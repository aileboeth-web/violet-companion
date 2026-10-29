"""The channel interface (phase 10).

A platform message arrives as an ``InboundMessage``: who sent it on which channel, a stable id for de-duplication and
a list of ``Part`` objects (text, emoji, image, voice, file, ...). Adapters parse their own protocol into parts -- they
never hand raw protocol text (CQ codes, JSON payloads, XML) to Violet; ``normalize.render_part`` decides what the model
reads.

A ``ChannelAdapter`` runs its own receive loop (long polling, a webhook, a socket) in the background and calls the
sink for every message; the sink returns True once the message is stored durably, so the adapter may advance its
cursor. Sending goes through ``send_text`` / ``send_image`` / ``send_typing`` for one peer; the runtime wraps these in
``PeerTransport`` so the existing outbox (``violet_runtime.MessageSender``) works for every channel unchanged.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PART_KINDS = ("text", "emoji", "sticker", "image", "voice", "file", "video", "location", "link", "quote", "mention",
              "unsupported")

STATES = ("stopped", "starting", "connected", "reconnecting", "needs_login", "error", "disabled")


@dataclass(frozen=True)
class Part:
    """One piece of a message. ``text``: the text, an emoji's name, a file name, a caption, a transcript.
    ``data``: platform fields an adapter needs later (a media id to download) -- never shown to the model."""
    kind: str
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    def __post_init__(self):
        if self.kind not in PART_KINDS:
            raise ValueError(f"unknown part kind {self.kind!r}")


@dataclass
class InboundMessage:
    channel: str                       # the configured channel's id ("wechat", "telegram")
    channel_type: str                  # wechat_ilink | telegram | onebot11 | ...
    peer_id: str                       # the sender on that platform ("" = the channel has only one possible sender)
    dedup_key: str                     # stable across redelivery
    parts: list[Part]
    message_id: str | None = None      # the platform's id, for diagnosis
    peer_name: str | None = None
    received_at: str | None = None


@dataclass
class ChannelStatus:
    channel: str
    channel_type: str
    state: str = "stopped"
    detail: str = ""                   # plain words for the admin, no addresses or secrets
    last_ok_at: float | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    restarts: int = 0
    received: int = 0
    sent: int = 0
    send_failures: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("channel", "channel_type", "state", "detail", "last_ok_at", "last_error",
                                               "consecutive_errors", "restarts", "received", "sent", "send_failures",
                                               "extra")}


Sink = Callable[[InboundMessage], bool]


class ChannelAdapter(ABC):
    """Base class: a background receive loop under a supervisor (errors back off and retry; the thread never dies
    silently), status bookkeeping, and the send methods subclasses implement."""

    channel_type = "base"
    label = "渠道"
    # what the platform can do: text, image_in, image_out, typing, voice_text (voice arrives with a transcript)
    capabilities: frozenset[str] = frozenset({"text"})
    poll_backoff_max = 60.0

    def __init__(self, channel_id: str, config: dict[str, Any] | None = None, secrets: dict[str, str] | None = None,
                 sleep: Callable[[float], None] | None = None):
        self.channel_id = channel_id
        self.config = dict(config or {})
        self.secrets = dict(secrets or {})
        self.status_info = ChannelStatus(channel=channel_id, channel_type=self.channel_type)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sink: Sink | None = None
        self._sleep = sleep
        self._lock = threading.RLock()

    # -- lifecycle --------------------------------------------------------------------------------------------
    def start(self, sink: Sink) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._sink = sink
            self._stop.clear()
            self._set_state("starting", "正在连接")
            self._thread = threading.Thread(target=self._supervise, name=f"violet-channel-{self.channel_id}", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self.on_stop()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._set_state("stopped", "已停止")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def on_stop(self) -> None:
        """Hook: wake a blocking receive (close a socket) so stop() returns quickly."""

    def _wait(self, seconds: float) -> bool:
        """Sleep unless stopped; True when stop was requested."""
        if self._sleep is not None:
            self._sleep(seconds)
            return self._stop.is_set()
        return self._stop.wait(seconds)

    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                self.prepare()
                # 0.7.6: logged in = connected now, not after the first receive returns (QQ: the next heartbeat, ~40 s;
                # Telegram: one long poll) -- the Control Center showed "正在连接" in red until then
                self._ok()
                while not self._stop.is_set():
                    handled = self.receive_once()
                    self._ok()
                    if handled == 0 and self.idle_delay() > 0 and self._wait(self.idle_delay()):
                        return
            except NeedsLogin as exc:
                self._set_state("needs_login", str(exc) or "需要重新登录")
                if self._wait(self.login_wait()):
                    return
            except Exception as exc:  # noqa: BLE001 -- the loop must survive anything a platform throws
                self._failed(exc)
                if self._wait(min(self.poll_backoff_max, 2.0 * (2 ** min(self.status_info.consecutive_errors, 5)))):
                    return
                self.status_info.restarts += 1

    # -- hooks for subclasses -----------------------------------------------------------------------------------
    def prepare(self) -> None:
        """Called before the receive loop (and again after an error): log in, check the token, open a socket."""

    @abstractmethod
    def receive_once(self) -> int:
        """Fetch what is waiting, pass every message to deliver(), return how many arrived. May block (long poll)."""

    def idle_delay(self) -> float:
        return float(self.config.get("poll_interval", 1.0))

    def login_wait(self) -> float:
        return 5.0

    def deliver(self, message: InboundMessage) -> bool:
        """Hand one message to Violet; True once it is stored (the adapter may then acknowledge it)."""
        sink = self._sink
        if sink is None:
            return False
        stored = bool(sink(message))
        if stored:
            self.status_info.received += 1
        return stored

    @abstractmethod
    def send_text(self, peer_id: str, text: str, client_id: str | None = None) -> dict[str, Any]:
        """Send one text; return the platform response (a dict). Raise on failure."""

    def send_image(self, peer_id: str, path: str) -> dict[str, Any]:
        raise UnsupportedOperation(f"{self.label} 不支持发送图片")

    def send_typing(self, peer_id: str, on: bool) -> None:
        """Cosmetic; platforms without a typing indicator ignore it."""

    def download(self, part: Part) -> Path:
        raise UnsupportedOperation(f"{self.label} 不支持下载这类消息")

    def default_peer(self) -> str | None:
        """Where messages go when no peer is known (a single-user channel such as WeChat iLink)."""
        return None

    # -- status ---------------------------------------------------------------------------------------------------
    def status(self) -> ChannelStatus:
        if self._thread is not None and not self._thread.is_alive() and self.status_info.state not in (
                "stopped", "disabled"):
            self.status_info.state, self.status_info.detail = "error", "接收线程已经停止"
        return self.status_info

    def _set_state(self, state: str, detail: str = "") -> None:
        self.status_info.state = state
        self.status_info.detail = detail

    def _ok(self) -> None:
        info = self.status_info
        info.last_ok_at = time.time()
        info.consecutive_errors = 0
        if info.state != "connected":
            self._set_state("connected", "已连接")

    def _failed(self, exc: BaseException) -> None:
        from ai.channels.normalize import safe_error
        info = self.status_info
        info.consecutive_errors += 1
        info.last_error = safe_error(exc)
        self._set_state("reconnecting" if info.consecutive_errors < 5 else "error",
                        "连接出错，正在重试" if info.consecutive_errors < 5 else "连续出错，请检查配置或网络")


class NeedsLogin(RuntimeError):
    """The platform session ended (QR login, expired token): the adapter waits instead of hammering the server."""


class UnsupportedOperation(RuntimeError):
    pass


class PeerTransport:
    """The send side of one conversation (adapter + peer) with the method names MessageSender has always called on the
    WeChat client, so the outbox, retries, typing simulation and image authorisation stay in one place."""

    def __init__(self, adapter: ChannelAdapter, peer_id: str | None):
        self.adapter = adapter
        self.peer_id = peer_id or adapter.default_peer() or ""

    def send_text(self, text: str, client_id: str | None = None, **_ignored) -> dict[str, Any]:
        try:
            result = self.adapter.send_text(self.peer_id, text, client_id=client_id)
        except Exception:
            self.adapter.status_info.send_failures += 1
            raise
        self.adapter.status_info.sent += 1
        return result

    def send_image(self, image_path: str, **_ignored) -> dict[str, Any]:
        try:
            result = self.adapter.send_image(self.peer_id, str(image_path))
        except Exception:
            self.adapter.status_info.send_failures += 1
            raise
        self.adapter.status_info.sent += 1
        return result

    def send_typing(self, status: int = 1, **_ignored) -> Any:
        return self.adapter.send_typing(self.peer_id, status == 1)

    def download_media(self, item: Any) -> Path:
        return self.adapter.download(item)
