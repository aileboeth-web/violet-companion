"""Message runtime: receive -> inbox -> debounce -> serial turn worker -> outbox.

Replaces the module-level loop/threads that used to live in main.py.
Guarantees (single WeChat user, single process):

* Every received item is written to ``inbound_messages`` before the poll
  cursor is committed; redelivered items are dropped by ``dedup_key``.
* One bad item (download/typing/DB error) never stops the poll loop; the loop
  itself runs under a supervisor that restarts it after unexpected errors.
* Turns are processed one at a time by a single worker thread, and the
  proactive scheduler shares ``pipeline_lock``; agents never run two
  pipelines concurrently, so context cannot bleed between turns.
* The user's message is stored in ``messages`` exactly once per turn, even
  when the pipeline fails.
* Every outgoing message gets an ``outbound_messages`` row with a final status
  (sent / failed / cancelled / skipped).
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import random
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import requests

from ai.chat_output import attach_images, normalize_chat_output
from ai.conversation_state import TopicTracker
from ai.memory_jobs import MemoryWorker
from ai.onboarding import OnboardingPlanner
from ai.profile_store import Profile
from ai.settings import env_bool, env_float, env_int, env_str

logger = logging.getLogger("violet.runtime")

ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
_ITEM_NAMES = {ITEM_TEXT: "text", ITEM_IMAGE: "image", ITEM_VOICE: "voice", ITEM_FILE: "file", ITEM_VIDEO: "video"}

DEFAULT_FALLBACK_REPLY = "我这边刚刚卡了一下，没接上你的话，你再说一次好不好"
IMAGE_DOWNLOAD_FAILED_TEXT = "[用户发来一张图片，但图片没能加载出来]"
IMAGE_VISION_FAILED_TEXT = "[用户发来一张图片，但暂时没能看清图片内容]"
IMAGE_INTERRUPTED_TEXT = "[用户之前发来一张图片，但处理被中断了]"

TYPING_ON, TYPING_OFF = 1, 2


# ---------------------------------------------------------------------------
# Turn completion detection (rule-based, no LLM) — unchanged from main.py
# ---------------------------------------------------------------------------

_INCOMPLETE_PREFIXES = ("我跟你说", "就是", "那个", "你知道吗")
_INCOMPLETE_SUFFIXES = ("，", "然后", "但是", "因为", "就是")


def is_incomplete(messages: list[str]) -> bool:
    """Return True if the buffered messages look unfinished and we should wait longer."""
    if not messages:
        return False
    last = messages[-1].strip()
    if len(messages) == 1 and len(last) < 5:
        if any(last.startswith(p) for p in _INCOMPLETE_PREFIXES):
            return True
    if any(last.endswith(s) for s in _INCOMPLETE_SUFFIXES):
        return True
    return False


# ---------------------------------------------------------------------------
# Turn + debounce buffer
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    inbound_ids: list[int]
    texts: list[str]
    received_at: str | None = None
    origin: str = "live"            # live | recovered
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    channel: str = "wechat"         # phase 10: where the words came from -- and where the reply goes
    peer_id: str = ""

    @property
    def combined(self) -> str:
        return "\n".join(t for t in self.texts if t)


@dataclass
class _Entry:
    inbound_id: int
    text: str | None = None
    future: Future | None = None
    received_at: str | None = None


class TurnBuffer:
    """Collects items until the user pauses, then emits one Turn.

    Images are added as futures (download + vision run in the background); the
    flush waits for them so the combined text keeps the original order.
    """

    def __init__(
        self,
        on_ready: Callable[[Turn], None],
        normal_delay: float,
        extra_delay: float,
        media_wait_max: float = 75.0,
        timer_factory: Callable[..., threading.Timer] = threading.Timer,
        conversation: tuple[str, str] = ("wechat", ""),
    ):
        self.conversation = conversation          # phase 10: one buffer per (channel, peer)
        self._on_ready = on_ready
        self.normal_delay = normal_delay
        self.extra_delay = extra_delay
        self.media_wait_max = media_wait_max
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._entries: list[_Entry] = []
        self._timer: threading.Timer | None = None
        self._flush_requested_at: float | None = None

    def set_delays(self, normal: float, extra: float) -> None:
        with self._lock:
            self.normal_delay, self.extra_delay = normal, extra

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._entries)

    def add_text(self, inbound_id: int, text: str, received_at: str | None = None) -> float:
        with self._lock:
            self._entries.append(_Entry(inbound_id, text=text, received_at=received_at))
            return self._schedule_locked(self._delay_locked())

    def add_media(self, inbound_id: int, future: Future, received_at: str | None = None) -> float:
        with self._lock:
            self._entries.append(_Entry(inbound_id, future=future, received_at=received_at))
            return self._schedule_locked(self._delay_locked())

    def flush_now(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._flush()

    def _delay_locked(self) -> float:
        texts = [e.text for e in self._entries if e.text is not None]
        return self.extra_delay if is_incomplete(texts) else self.normal_delay

    def _schedule_locked(self, delay: float) -> float:
        if self._timer is not None:
            self._timer.cancel()
        self._flush_requested_at = None
        self._timer = self._timer_factory(delay, self._flush)
        self._timer.daemon = True
        self._timer.start()
        return delay

    def _flush(self) -> None:
        with self._lock:
            if not self._entries:
                self._timer = None
                return
            unresolved = [e for e in self._entries if e.future is not None and not e.future.done()]
            if unresolved:
                if self._flush_requested_at is None:
                    self._flush_requested_at = time.monotonic()
                if time.monotonic() - self._flush_requested_at < self.media_wait_max:
                    self._timer = self._timer_factory(0.3, self._flush)
                    self._timer.daemon = True
                    self._timer.start()
                    return
            entries, self._entries = self._entries, []
            self._timer = None
            self._flush_requested_at = None

        texts = []
        for entry in entries:
            if entry.future is None:
                texts.append(entry.text or "")
                continue
            try:
                texts.append(entry.future.result(timeout=0) or IMAGE_VISION_FAILED_TEXT)
            except Exception:
                texts.append(IMAGE_VISION_FAILED_TEXT)
        turn = Turn(
            inbound_ids=[e.inbound_id for e in entries],
            texts=texts,
            received_at=entries[-1].received_at,
            channel=self.conversation[0],
            peer_id=self.conversation[1],
        )
        try:
            self._on_ready(turn)
        except Exception:
            logger.exception("could not enqueue turn %s", turn.turn_id)


# ---------------------------------------------------------------------------
# Sending with explicit status (outbox)
# ---------------------------------------------------------------------------

class SendRejected(RuntimeError):
    """The iLink API answered but refused the message (ret/errcode != 0)."""


@dataclass
class SendReport:
    sent: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    cancelled: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    blocked: str | None = None          # phase 10: the outbound guard refused the whole send (reason code)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.cancelled and self.blocked is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sent_count": len(self.sent),
            "failed_count": len(self.failed),
            "cancelled_count": len(self.cancelled),
            "skipped_count": len(self.skipped),
            "sent_messages": self.sent,
            "error": self.failed[0].get("error") if self.failed else (f"blocked: {self.blocked}" if self.blocked else None),
            "blocked": self.blocked,
        }


def _response_error(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    ret, errcode = response.get("ret"), response.get("errcode")
    if ret not in (None, 0) or errcode not in (None, 0):
        return f"ret={ret} errcode={errcode} errmsg={response.get('errmsg')}"
    return None


def is_retryable_send_error(exc: BaseException) -> bool:
    # ConnectTimeout / connection refused: the request never reached the server.
    # ReadTimeout is NOT retried: the message may already have been delivered.
    if isinstance(exc, requests.ConnectionError):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in (502, 503, 504)
    return False


class MessageSender:
    """Sends text messages one by one with simulated typing and outbox records."""

    def __init__(
        self,
        client,
        db,
        simulate_typing: bool | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int | None = None,
        retry_delay: float | None = None,
        stats: dict[str, int] | None = None,
        channel: str | None = None,
        peer_id: str | None = None,
    ):
        # client: the WeChat ILinkClient, or (phase 10) a channels.base.PeerTransport -- the same send methods
        self.client = client
        self.db = db
        self.channel = channel
        self.peer_id = peer_id
        self.simulate_typing = env_bool("VIOLET_SIMULATE_TYPING", True) if simulate_typing is None else simulate_typing
        self.sleep = sleep
        self.max_attempts = max(1, max_attempts if max_attempts is not None else env_int("VIOLET_SEND_MAX_ATTEMPTS", 2))
        self.retry_delay = retry_delay if retry_delay is not None else env_float("VIOLET_SEND_RETRY_DELAY_SECONDS", 2.0)
        self.stats = stats if stats is not None else {}

    # -- typing (cosmetic; never allowed to break sending) --------------------
    def typing(self, status: int) -> bool:
        try:
            self.client.send_typing(status=status)
            return True
        except Exception as exc:
            count = self.stats.get("typing_failures", 0) + 1
            self.stats["typing_failures"] = count
            if count == 1 or count % 20 == 0:
                logger.warning("typing indicator failed (%d so far): %s", count, exc)
            return False

    def _simulate_typing_delay(self, text: str) -> None:
        if not self.simulate_typing:
            return
        min_delay = env_float("VIOLET_TYPING_MIN_DELAY_SECONDS", 1.0)
        max_delay = env_float("VIOLET_TYPING_MAX_DELAY_SECONDS", 20.0)
        upper = min(min_delay + len(text) * 0.3, max_delay)
        delay = random.uniform(min_delay, max(min_delay, upper))
        self.typing(TYPING_ON)
        elapsed = 0.0
        while elapsed < delay:
            step = min(5.0, delay - elapsed)
            self.sleep(step)
            elapsed += step
            if elapsed < delay:
                self.typing(TYPING_ON)
        self.typing(TYPING_OFF)

    # -- sending ------------------------------------------------------------
    def send_messages(
        self,
        messages: list[dict[str, Any]],
        origin: str,
        turn_id: str | None = None,
        proactive_event_id: int | None = None,
        record_history: bool = True,
    ) -> SendReport:
        report = SendReport()
        stop_reason: str | None = None
        # Phase 10: the one outbound checkpoint. Every text of every origin passes it just before a channel sees it:
        # a loaded agent instruction, an internal context header, tool data or internal JSON is never delivered --
        # the whole send is refused (a reply missing one line could still be the wrong reply).
        from ai.channels.outbound import find_reply_leak, is_sealed, without_seal
        # phase 10.6, structural first: only messages built by ai/channels/outbound.py (the Chat agent's final text or
        # a sentence in Violet's code) may be sent; anything else -- e.g. a dict taken out of an agent result -- is
        # refused without reading it. Then the content scan, as a second line.
        leak = "unsealed" if any(not is_sealed(m) for m in messages) else find_reply_leak(messages)
        messages = [without_seal(m) for m in messages]
        if leak:
            for msg in messages:
                client_id = f"pybot-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
                outbound_id = self.db.add_outbound_message(client_id, origin, str(msg.get("type") or "text"),
                                                           "[已拦截：内部内容，没有发送]", turn_id, proactive_event_id,
                                                           status="skipped", channel=self.channel, peer_id=self.peer_id)
                self.db.update_outbound_message(outbound_id, error=f"blocked: {leak}")
                report.skipped.append({**msg, "content": None, "error": f"blocked: {leak}"})
            report.blocked = leak
            self.stats["outbound_blocked"] = self.stats.get("outbound_blocked", 0) + 1
            logger.error("outbound guard refused a %s send (%s); nothing was delivered", origin, leak)
            return report
        for msg in messages:
            msg_type = msg.get("type")
            content = msg.get("content")
            client_id = f"pybot-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
            if msg_type == "image":
                error = self._send_image_message(msg, client_id, origin, turn_id, proactive_event_id, record_history,
                                                 stop_reason, report)
                if error is not None and stop_reason is None:
                    stop_reason = f"previous message failed: {error}"[:300]
                continue
            if msg_type == "sticker":
                # 0.7.9: a sticker is decoration -- if it cannot go out, the words after it still do
                self._send_sticker_message(msg, client_id, origin, turn_id, proactive_event_id, record_history,
                                           stop_reason, report)
                continue
            if msg_type != "text" or not content:
                self.db.add_outbound_message(client_id, origin, str(msg_type or "unknown"), content,
                                             turn_id, proactive_event_id, status="skipped", channel=self.channel,
                                             peer_id=self.peer_id)
                report.skipped.append(msg)
                continue
            outbound_id = self.db.add_outbound_message(client_id, origin, "text", content, turn_id, proactive_event_id,
                                                       channel=self.channel, peer_id=self.peer_id)
            if stop_reason is not None:
                # Never send later lines after an earlier one failed: order matters.
                self.db.update_outbound_message(outbound_id, status="cancelled", error=stop_reason)
                report.cancelled.append(msg)
                continue
            self._simulate_typing_delay(content)
            error = self._send_one(outbound_id, client_id, content, record_history)
            if error is None:
                report.sent.append(msg)
            else:
                report.failed.append({**msg, "error": error})
                stop_reason = f"previous message failed: {error}"[:300]
        if report.failed:
            self.stats["send_failures"] = self.stats.get("send_failures", 0) + len(report.failed)
        return report

    def _send_image_message(self, msg: dict[str, Any], client_id: str, origin: str, turn_id: str | None,
                            proactive_event_id: int | None, record_history: bool, stop_reason: str | None,
                            report: "SendReport") -> str | None:
        """Phase 6: send one stored image. Only complete, intact outbound assets; never twice."""
        from ai.images.media import MediaAssetStore
        store = MediaAssetStore(self.db)
        asset_id = msg.get("asset_id")
        denied = self._authorize_image(msg, origin)
        if denied:
            # never on the model's word alone: a marker is not a permission
            self.db.add_outbound_message(client_id, origin, "image", f"asset:{asset_id}", turn_id, proactive_event_id,
                                         status="skipped", channel=self.channel, peer_id=self.peer_id)
            self.db.execute("UPDATE outbound_messages SET error = ? WHERE client_id = ?",
                            (f"unauthorized_image: {denied}"[:300], client_id))
            logger.warning("image %s not sent: %s", asset_id, denied)
            report.skipped.append({**msg, "error": f"unauthorized_image: {denied}"})
            return None
        asset, problem = store.sendable(int(asset_id))
        if asset is not None and asset["send_status"] in ("sent", "unknown"):
            # already delivered, or delivery unknown after a crash (needs manual reconciliation): never resend
            self.db.add_outbound_message(client_id, origin, "image", f"asset:{asset_id}", turn_id, proactive_event_id,
                                         status="skipped", channel=self.channel, peer_id=self.peer_id)
            report.skipped.append(msg)
            return None
        outbound_id = self.db.add_outbound_message(client_id, origin, "image", f"asset:{asset_id}", turn_id,
                                                   proactive_event_id, channel=self.channel, peer_id=self.peer_id)
        if stop_reason is not None:
            self.db.update_outbound_message(outbound_id, status="cancelled", error=stop_reason)
            report.cancelled.append(msg)
            return None
        if problem:
            self.db.update_outbound_message(outbound_id, status="failed", error=problem[:300])
            if asset is not None:
                store.mark_send(asset["id"], "failed", problem[:300])
            report.failed.append({**msg, "error": problem})
            return problem
        store.mark_send(asset["id"], "sending")
        last_error = "unknown error"
        for attempt in range(1, self.max_attempts + 1):
            self.db.update_outbound_message(outbound_id, status="sending", increment_attempts=True)
            try:
                response = self.client.send_image(str(store.path_of(asset)))
                rejected = _response_error(response)
                if rejected:
                    raise SendRejected(rejected)
            except Exception as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"[:300]
                if attempt < self.max_attempts and is_retryable_send_error(exc):
                    self.sleep(self.retry_delay * attempt)
                    continue
                logger.error("image send failed (outbound #%s, asset %s): %s", outbound_id, asset["id"], last_error)
                self.db.update_outbound_message(outbound_id, status="failed", error=last_error)
                store.mark_send(asset["id"], "failed", last_error)
                report.failed.append({**msg, "error": last_error})
                return last_error
            label = f"[图片：{asset.get('caption') or 'AI 生成的图片'}]"
            message_id = self.db.add_assistant_message("image", label, channel=self.channel) if record_history else None
            self.db.update_outbound_message(outbound_id, status="sent", message_id=message_id)
            store.mark_send(asset["id"], "sent")
            shared_event = self._mark_life_event_shown(asset)
            report.sent.append({**msg, "content": label, "life_event_id": shared_event})
            return None
        return last_error

    def _send_sticker_message(self, msg: dict[str, Any], client_id: str, origin: str, turn_id: str | None,
                              proactive_event_id: int | None, record_history: bool, stop_reason: str | None,
                              report: "SendReport") -> None:
        """0.7.9: one sticker of her collection (ai/stickers.py), looked up by number only."""
        from ai import stickers
        sticker, problem = stickers.sendable(self.db, msg.get("sticker_id"))
        label = f"[表情包：{(sticker or {}).get('description') or '表情'}]"
        outbound_id = self.db.add_outbound_message(client_id, origin, "sticker", label, turn_id, proactive_event_id,
                                                   channel=self.channel, peer_id=self.peer_id)
        from ai import trace
        if stop_reason is not None or problem:
            self.db.update_outbound_message(outbound_id, status="cancelled" if stop_reason else "skipped",
                                            error=(stop_reason or problem)[:300])
            report.skipped.append({**msg, "error": stop_reason or problem})
            trace.step("发表情包", 编号=msg.get("sticker_id"), 结果="不能发", 原因=stop_reason or problem)
            return
        send = getattr(self.client, "send_sticker", None) or self.client.send_image
        try:
            self.db.update_outbound_message(outbound_id, status="sending", increment_attempts=True)
            rejected = _response_error(send(str(stickers.path_of(sticker))))
            if rejected:
                raise SendRejected(rejected)
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"[:300]
            logger.warning("sticker %s not sent: %s", sticker["id"], error)
            self.db.update_outbound_message(outbound_id, status="failed", error=error)
            report.skipped.append({**msg, "error": error})      # not a failed reply: no fallback text for it
            trace.step("发表情包", 编号=sticker["id"], 结果="失败", 原因=error)
            return
        message_id = self.db.add_assistant_message("text", label, channel=self.channel) if record_history else None
        self.db.update_outbound_message(outbound_id, status="sent", message_id=message_id)
        stickers.mark_used(self.db, sticker["id"])
        report.sent.append({**msg, "content": label})
        trace.step("发表情包", 编号=sticker["id"], 结果="已发出")

    def _mark_life_event_shown(self, asset: dict[str, Any]) -> int | None:
        """Phase 7 (D5): a picture of a life event that actually reached WeChat counts as telling it.

        Only after a successful send (this is the only caller), only once (an event already shared,
        by text or by an earlier picture, is not counted again), and only the sharing fields change --
        never whether or when the event happened.
        """
        event_id = asset.get("life_event_id")
        if not event_id:
            return None
        try:
            from ai.conversation_state import life_topic_key
            from ai.life_store import LifeStore
            store = LifeStore(self.db)
            event = store.event(int(event_id))
            if event is None or event["status"] != "happened" or event["share_count"] > 0:
                return None
            store.mark_shared(event["id"])
            self.db.add_topic_mention(life_topic_key(event["id"]), "violet_reply")
            return event["id"]
        except Exception:
            logger.exception("could not record the life event shown in image %s", asset.get("id"))
            return None

    def _authorize_image(self, msg: dict[str, Any], origin: str) -> str | None:
        """None if this image may be sent in this reply, else why not (phase 7, D2)."""
        asset_id, turn_key = msg.get("asset_id"), msg.get("turn_key")
        if origin != "reply":
            return f"images are only sent in replies, not in {origin} messages"
        if not isinstance(asset_id, int) or isinstance(asset_id, bool) or asset_id <= 0:
            return "invalid asset id"
        if not turn_key:
            return "no turn authorisation"
        asset = self.db.fetch_one("SELECT * FROM media_assets WHERE id = ?", (asset_id,))
        if asset is None:
            return "unknown asset"
        if asset["direction"] != "outbound" or not asset["ai_generated"] or asset["status"] != "ready":
            return "not a ready, AI-generated outbound image"
        task = self.db.fetch_one("SELECT * FROM image_tasks WHERE task_id = ?", (asset["task_id"],)) if asset["task_id"] else None
        if task is None or task["origin"] != "chat" or task["status"] != "succeeded":
            return "not produced by a successful chat image task"
        if task["turn_key"] != turn_key:
            return "produced in another turn"
        if asset_id not in json.loads(task["output_asset_ids"] or "[]"):
            return "not an output of its task"
        return None

    def _send_one(self, outbound_id: int, client_id: str, content: str, record_history: bool) -> str | None:
        last_error = "unknown error"
        for attempt in range(1, self.max_attempts + 1):
            self.db.update_outbound_message(outbound_id, status="sending", increment_attempts=True)
            try:
                response = self.client.send_text(content, client_id=client_id)
                rejected = _response_error(response)
                if rejected:
                    raise SendRejected(rejected)
            except Exception as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"[:300]
                if attempt < self.max_attempts and is_retryable_send_error(exc):
                    logger.warning("send attempt %d/%d failed, retrying: %s", attempt, self.max_attempts, last_error)
                    self.sleep(self.retry_delay * attempt)
                    continue
                logger.error("send failed (outbound #%s): %s", outbound_id, last_error)
                self.db.update_outbound_message(outbound_id, status="failed", error=last_error)
                from ai import trace
                trace.step("发送", 渠道=self.channel, 消息号=outbound_id, 结果="失败", 尝试次数=attempt, 原因=last_error)
                return last_error
            message_id = self.db.add_assistant_message("text", content, channel=self.channel) if record_history else None
            self.db.update_outbound_message(outbound_id, status="sent", message_id=message_id)
            from ai import trace
            trace.step("发送", 渠道=self.channel, 消息号=outbound_id, 结果="已发出", 尝试次数=attempt, 内容=trace.text(content))
            return None
        self.db.update_outbound_message(outbound_id, status="failed", error=last_error)
        return last_error


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

def _dedup_key(raw: dict[str, Any], index: int, item: dict[str, Any]) -> str:
    """Kept for callers of the old name; the key itself now lives with the WeChat channel."""
    from ai.channels.wechat_ilink import dedup_key
    return dedup_key(raw, index, item)


PAIRING_REPLIES = {
    "bound": "绑定好了，以后在这里也能和 Violet 聊天啦",
    "invalid": "绑定码不对或者已经过期了，请在 Violet 的管理后台重新生成一个",
    "locked": "绑定码错的次数太多了，请过一小时再试",
}


class VioletRuntime:
    def __init__(
        self,
        client,
        db,
        agents: dict[str, Any],
        proactive_scheduler=None,
        pipeline_runner: Callable[..., dict[str, Any]] | None = None,
        vision: Callable[[str], str] | None = None,
        sender: MessageSender | None = None,
        timer_factory: Callable[..., threading.Timer] = threading.Timer,
    ):
        if pipeline_runner is None:
            from ai.chat import run_pipeline as pipeline_runner
        if vision is None:
            from ai.vision import describe_image as vision
        self.client = client
        self.db = db
        self.started_at = db.now()          # work last touched before this belongs to a previous run
        self.agents = agents
        self.proactive_scheduler = proactive_scheduler
        self.pipeline_runner = pipeline_runner
        self.vision = vision
        self.stats: dict[str, int] = {}
        self.sender = sender or MessageSender(client, db, stats=self.stats, channel="wechat")
        self.pipeline_lock = threading.RLock()
        self.turn_queue: "queue.Queue[Turn]" = queue.Queue()
        self._timer_factory = timer_factory
        self.buffer = self._new_buffer(("wechat", ""))
        # phase 10: every (channel, peer) has its own debounce buffer and outbox sender; WeChat keeps the two above
        self.buffers: dict[tuple[str, str], TurnBuffer] = {("wechat", ""): self.buffer}
        self._senders: dict[tuple[str, str], MessageSender] = {}
        self.channels = None                    # the ChannelManager sets itself here
        self.channels_active = False
        self._poll_stop = threading.Event()
        from ai.channels.identity import IdentityStore
        self.identities = IdentityStore(db)
        from ai import permissions                      # 2026-10-30: approvals tell the owner through this runtime
        permissions.attach_runtime(self)
        self.media_executor = ThreadPoolExecutor(max_workers=env_int("VIOLET_MEDIA_WORKERS", 2), thread_name_prefix="violet-media")
        self.poll_timeout = env_int("VIOLET_POLL_TIMEOUT_SECONDS", 3)
        self.poll_interval = env_float("VIOLET_POLL_INTERVAL_SECONDS", 1.0)
        self.recovery_max_age = env_float("VIOLET_RECOVERY_MAX_AGE_SECONDS", 600.0)
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self.last_poll_ok_at: float | None = None
        self.last_poll_error: str | None = None
        self.consecutive_poll_errors = 0
        self.poll_restarts = 0
        if proactive_scheduler is not None and hasattr(proactive_scheduler, "attach_runtime"):
            proactive_scheduler.attach_runtime(is_busy=self.is_busy, pipeline_lock=self.pipeline_lock,
                                               channel_context=self.proactive_channel_context)
        # Phase 2: memory consolidation runs after the reply, in the background.
        self.tracker = TopicTracker(db)
        self.profile = Profile(db)
        self.onboarding = OnboardingPlanner(db, self.profile.facts)
        self.async_memory = env_bool("VIOLET_ASYNC_MEMORY", True) and agents.get("memory") is not None
        self.memory_worker = MemoryWorker(db, agents["memory"]) if self.async_memory else None

    # -- conversations (phase 10) -------------------------------------------
    def _new_buffer(self, conversation: tuple[str, str]) -> TurnBuffer:
        return TurnBuffer(
            self._enqueue_turn,
            normal_delay=env_float("DEBOUNCE_NORMAL", 5.0),
            extra_delay=env_float("DEBOUNCE_EXTRA", 10.0),
            media_wait_max=env_float("VISION_TIMEOUT_SECONDS", 60.0) + 15.0,
            timer_factory=self._timer_factory,
            conversation=conversation,
        )

    def buffer_for(self, conversation: tuple[str, str]) -> TurnBuffer:
        if conversation == ("wechat", ""):
            return self.buffer
        buffer = self.buffers.get(conversation)
        if buffer is None:
            buffer = self.buffers.setdefault(conversation, self._new_buffer(conversation))
            buffer.set_delays(self.buffer.normal_delay, self.buffer.extra_delay)
        return buffer

    def set_debounce(self, normal: float, extra: float) -> None:
        for buffer in list(self.buffers.values()):
            buffer.set_delays(normal, extra)

    @property
    def simulate_typing(self) -> bool:
        return self.sender.simulate_typing

    @simulate_typing.setter
    def simulate_typing(self, value: bool) -> None:
        # phase 10: main's reload hook used to set a plain attribute here that no sender read
        self.sender.simulate_typing = bool(value)
        for sender in list(self._senders.values()):
            sender.simulate_typing = bool(value)

    def adapter_for(self, channel: str):
        return self.channels.get(channel) if self.channels is not None and channel != "wechat" else None

    def sender_for(self, conversation: tuple[str, str]) -> MessageSender:
        """The outbox sender of one conversation. WeChat (and any channel that is not configured any more) uses the
        WeChat sender; other channels get the same MessageSender over their adapter."""
        channel, peer = conversation
        if channel == "wechat":
            return self.sender
        sender = self._senders.get(conversation)
        adapter = self.adapter_for(channel)
        if adapter is None:
            raise LookupError(f"channel {channel} is not configured")
        if sender is None or sender.client.adapter is not adapter:
            from ai.channels.base import PeerTransport
            base = self.sender
            sender = MessageSender(PeerTransport(adapter, peer), self.db, simulate_typing=base.simulate_typing,
                                   sleep=base.sleep, max_attempts=base.max_attempts, retry_delay=base.retry_delay,
                                   stats=self.stats, channel=channel, peer_id=peer)
            self._senders[conversation] = sender
        return sender

    def channel_context(self, channel: str | None):
        from ai.channels.context import DEFAULT
        if not channel or channel == "wechat" or self.channels is None:
            return DEFAULT
        try:
            return self.channels.context_for(channel)
        except Exception:
            logger.exception("channel context for %s unavailable", channel)
            return DEFAULT

    def proactive_conversation(self) -> tuple[str, str]:
        """Where a message Violet starts goes: the channel chosen in the Control Center, else the conversation the owner
        used last (a bound account on a running channel), else WeChat."""
        choice = self.channels.proactive_channel() if self.channels is not None else "auto"
        candidates: list[tuple[str, str]] = []
        if choice not in ("auto", "", "wechat"):
            candidates.append((choice, self.identities.primary_peer(choice) or ""))
        if choice == "auto":
            row = self.db.fetch_one("SELECT channel, peer_id FROM inbound_messages WHERE status = 'done' "
                                    "ORDER BY id DESC LIMIT 1")
            if row and row.get("channel") and row["channel"] != "wechat":
                candidates.append((row["channel"], row.get("peer_id") or ""))
        for channel, peer in candidates:
            adapter = self.adapter_for(channel)
            entry = self.channels.entry(channel) if self.channels is not None else None
            if adapter is not None and entry and entry.get("enabled", True) and peer:
                return channel, peer
        return "wechat", ""

    # -- stats ------------------------------------------------------------
    def _bump(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount

    # -- lifecycle ----------------------------------------------------------
    def start_worker(self) -> None:
        if self.memory_worker is not None:
            self.memory_worker.start()
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._worker_thread = threading.Thread(target=self._worker_main, name="violet-turn-worker", daemon=True)
        self._worker_thread.start()

    def start_polling(self) -> None:
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_thread_main, name="violet-poll", daemon=True)
        self._poll_thread.start()

    def stop_polling(self, timeout: float = 5.0) -> None:
        """Phase 10: the WeChat channel switched off in the Control Center (the rest of Violet keeps running)."""
        self._poll_stop.set()
        thread = self._poll_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _polling_stopped(self) -> bool:
        return self._stop.is_set() or self._poll_stop.is_set()

    def start(self, poll: bool = True) -> None:
        self.recover_pending()
        self.start_worker()
        if poll:
            self.start_polling()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._poll_stop.set()
        for thread in (self._poll_thread, self._worker_thread):
            if thread and thread.is_alive():
                thread.join(timeout=timeout)
        self.media_executor.shutdown(wait=False, cancel_futures=True)
        if self.memory_worker is not None:
            self.memory_worker.stop(timeout=timeout)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Test helper: wait until no buffered, queued or running turn remains."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_busy():
                return True
            time.sleep(0.02)
        return not self.is_busy()

    def wait_memory_idle(self, timeout: float = 30.0) -> bool:
        """Test helper: wait until background memory jobs are processed."""
        return self.memory_worker.wait_idle(timeout) if self.memory_worker else True

    def is_busy(self) -> bool:
        return any(b.has_pending() for b in list(self.buffers.values())) or self.turn_queue.unfinished_tasks > 0

    def health(self) -> dict[str, Any]:
        try:
            from ai.mcp import get_health_snapshot
            mcp_health = get_health_snapshot()
        except Exception:
            mcp_health = []
        return {
            "poll_thread_alive": bool(self._poll_thread and self._poll_thread.is_alive()),
            "worker_thread_alive": bool(self._worker_thread and self._worker_thread.is_alive()),
            "last_poll_ok_at": self.last_poll_ok_at,
            "last_poll_error": self.last_poll_error,
            "consecutive_poll_errors": self.consecutive_poll_errors,
            "poll_restarts": self.poll_restarts,
            "queued_turns": self.turn_queue.unfinished_tasks,
            "buffer_pending": any(b.has_pending() for b in list(self.buffers.values())),
            "stats": dict(self.stats),
            "inbound": self.db.count_inbound_by_status(),
            "outbound": self.db.count_outbound_by_status(),
            "mcp": mcp_health,
            "memory_worker_alive": bool(self.memory_worker and self.memory_worker.is_alive()),
            "memory_jobs": self.db.count_memory_jobs_by_status(),
        }

    # -- polling --------------------------------------------------------------
    def wechat_logged_in(self) -> bool:
        state = getattr(self.client, "state", None)
        return bool(getattr(state, "logged_in", True)) if state is not None else True

    def _poll_thread_main(self) -> None:
        # Supervisor: _poll_loop already handles per-iteration errors; this only
        # catches the unexpected so the thread never silently disappears.
        while not self._polling_stopped():
            try:
                self._poll_loop()
            except Exception:
                self.poll_restarts += 1
                logger.critical("poll loop crashed; restarting in 5s (restart #%d)", self.poll_restarts, exc_info=True)
                self._poll_stop.wait(5)

    def _poll_loop(self) -> None:
        while not self._polling_stopped():
            wait = self.poll_interval
            if not self.wechat_logged_in():
                # phase 10: Violet Core runs before the QR login; polling waits for it instead of logging errors
                self._poll_stop.wait(1.0)
                continue
            try:
                self.poll_once()
                self.consecutive_poll_errors = 0
                self.last_poll_ok_at = time.time()
                self.last_poll_error = None
            except RuntimeError as exc:
                self.consecutive_poll_errors += 1
                self.last_poll_error = str(exc)[:300]
                text = str(exc)
                if "errcode=-14" in text or "session timeout" in text.lower():
                    logger.warning("iLink session timeout; clearing credentials and requesting a new QR login")
                    try:
                        self.client.clear_credentials()
                        self.client.start_login_async(force_refresh=True)
                    except Exception:
                        logger.exception("re-login trigger failed")
                else:
                    logger.error("fetch_messages error: %s", exc)
                wait = min(30.0, 5.0 * self.consecutive_poll_errors)
            except Exception as exc:
                self.consecutive_poll_errors += 1
                self.last_poll_error = f"{exc.__class__.__name__}: {exc}"[:300]
                logger.exception("unexpected polling error, will retry")
                wait = min(30.0, 5.0 * self.consecutive_poll_errors)
            self._poll_stop.wait(wait)          # stop() sets it too

    def poll_once(self) -> int:
        """Fetch one batch, persist every item, then commit the cursor."""
        messages, _ = self.client.fetch_messages(timeout=self.poll_timeout, persist_buf=False)
        handled = 0
        durable = True
        for message in messages:   # all of them: the old [:10] cap silently dropped the rest
            try:
                handled += self.handle_message(message)
            except Exception:
                durable = False
                logger.exception("could not persist an incoming message; cursor will not be committed")
        if durable:
            self.client.commit_sync_buf()
        return handled

    def handle_message(self, message: dict[str, Any]) -> int:
        """Persist and dispatch every item of one WeChat message. Returns new items handled.
        Phase 10: parsed by the WeChat channel (same de-duplication keys), then the common ingest path.

        Violet stage 1: a message whose sender is not the logged-in account is not taken as the owner's words. It is
        handled like a stranger on another channel (placeholder row, listed as a pending contact in the Control
        Center) and never bound automatically; if the admin approves that sender, its messages are the owner's."""
        from ai.channels.wechat_ilink import parse_message, unexpected_sender
        parsed = parse_message(message)
        sender = unexpected_sender(message, getattr(getattr(self.client, "credentials", None), "user_id", None))
        if sender:
            verdict = self.identities.admit(parsed.channel, sender, "owner")
            if verdict != "owner":
                self._bump("wechat_unexpected_sender")
                logger.warning("WeChat message from an account that is not the logged-in one: not processed "
                               "(pending contact in the Control Center)")
                parsed.peer_id = sender
                return self._ingest_stranger(parsed, verdict, pairing=False)
        return self._ingest(parsed)

    # -- phase 10: one ingest path for every channel ------------------------------------------------------------
    def ingest_message(self, message) -> bool:
        """The sink adapters deliver into: True once every part is stored (the adapter may acknowledge it)."""
        try:
            self._ingest(message)
            return True
        except Exception:
            logger.exception("could not store a message from channel %s", getattr(message, "channel", "?"))
            return False

    def _ingest(self, message) -> int:
        """Identity check, one inbox row per part (de-duplicated), then the conversation's buffer. Raises when a
        row could not be written (the message is not durable: the channel must deliver it again)."""
        channel, peer = message.channel, message.peer_id or ""
        if peer:
            policy = self.channels.accept_policy(channel) if self.channels is not None else "owner"
            verdict = self.identities.admit(channel, peer, policy, message.peer_name)
            if verdict != "owner":
                return self._ingest_stranger(message, verdict)
        handled = 0
        from ai.channels.normalize import render_part
        for index, part in enumerate(message.parts):
            key = part.data.get("dedup_key") or f"{message.dedup_key}:{index}"
            item_type = part.data.get("item_type") or (ITEM_IMAGE if part.kind == "image" else ITEM_TEXT)
            content = "[image]" if part.kind == "image" else render_part(part)
            inbound_id = self.db.add_inbound_message(key, message.message_id, index, item_type, content,
                                                     channel=channel, peer_id=peer or None)
            if inbound_id is None:
                self._bump("duplicates_dropped")
                logger.info("duplicate item ignored (channel=%s, message_id=%s, index=%d)", channel,
                            message.message_id, index)
                continue
            handled += 1
            self._bump("items_received")
            try:
                self._dispatch_part(inbound_id, part, content, (channel, peer))
            except Exception as exc:
                self._bump("dispatch_errors")
                logger.exception("dispatch failed for inbound #%s", inbound_id)
                self.db.update_inbound_messages([inbound_id], status="failed", error=f"dispatch: {exc}"[:300])
        return handled

    def _ingest_stranger(self, message, verdict: str, pairing: bool = True) -> int:
        """A peer that is not bound to the owner. Never answered by Violet and never stored as the owner's words: only a
        placeholder row (de-duplicated) -- except a pairing message, which gets a short system answer.
        pairing=False: no pairing codes (WeChat iLink can only send to the logged-in account, so an answer to an
        unexpected sender would reach the owner instead); only the admin can approve such a sender."""
        channel, peer = message.channel, message.peer_id
        text = next((p.text for p in message.parts if p.kind == "text"), "")
        if verdict == "pending" and pairing:
            result = self.identities.try_pairing(channel, peer, text)
            if result is not None:
                inbound_id = self.db.add_inbound_message(f"{message.dedup_key}:0", message.message_id, 0, ITEM_TEXT,
                                                         "[绑定请求，内容没有保存]", status="skipped",
                                                         channel=channel, peer_id=peer)
                if inbound_id is not None:
                    self.db.update_inbound_messages([inbound_id], error=f"pairing: {result}")
                    try:
                        from ai.channels.outbound import fixed_text
                        self.sender_for((channel, peer)).send_messages(fixed_text(PAIRING_REPLIES[result]),
                                                                       origin="system", record_history=False)
                    except Exception:
                        logger.exception("pairing answer on %s failed", channel)
                logger.warning("pairing attempt on %s: %s", channel, result)
                return 0
        inbound_id = self.db.add_inbound_message(f"{message.dedup_key}:0", message.message_id, 0, None,
                                                 "[未绑定的联系人发来消息，内容没有保存]", status="skipped",
                                                 channel=channel, peer_id=peer)
        if inbound_id is not None:
            self.db.update_inbound_messages([inbound_id], error=f"unbound_peer: {verdict}")
            self._bump("strangers_ignored")
            logger.info("message from an unbound %s peer on %s not processed", verdict, channel)
        return 0

    def _user_activity(self) -> None:
        if self.proactive_scheduler is None:
            return
        try:
            self.proactive_scheduler.on_user_message()
        except Exception:
            logger.exception("proactive_scheduler.on_user_message failed")

    def _typing_on(self, conversation: tuple[str, str]) -> None:
        try:
            self.sender_for(conversation).typing(TYPING_ON)
        except LookupError:
            pass

    def _dispatch_part(self, inbound_id: int, part, content: str, conversation: tuple[str, str]) -> None:
        received_at = self.db.now()
        buffer = self.buffer_for(conversation)
        if part.kind == "image":
            self._user_activity()
            future = self.media_executor.submit(self._describe_image_part, inbound_id, part, conversation)
            buffer.add_media(inbound_id, future, received_at)
            self._typing_on(conversation)
            logger.info("[buffer] +image inbound #%s (%s)", inbound_id, conversation[0])
            return
        if not content.strip():
            self.db.update_inbound_messages([inbound_id], status="skipped", error="empty text")
            return
        # phase 10: voice / file / video / stickers / unknown types arrive as safe text ("[对方发来一段语音…]")
        # instead of being dropped without an answer
        self._user_activity()
        delay = buffer.add_text(inbound_id, content, received_at)
        self._typing_on(conversation)
        logger.info("[buffer] +%s inbound #%s (%s), flush in %.1fs", part.kind, inbound_id, conversation[0], delay)

    def _describe_image_part(self, inbound_id: int, part, conversation: tuple[str, str]) -> str:
        if conversation[0] == "wechat":
            return self._describe_image_item(inbound_id, part.data.get("item") or {})
        adapter = self.adapter_for(conversation[0])
        if adapter is None or not (part.data or {}).get("sticker"):
            return self._describe_image_item(inbound_id, part, download=(adapter.download if adapter else None))
        paths: list[Any] = []

        def download(item):
            path = adapter.download(item)
            paths.append(path)
            return path
        text = self._describe_image_item(inbound_id, part, download=download)
        return self._collect_owner_sticker(inbound_id, part, paths[0], text) if paths else text

    def _collect_owner_sticker(self, inbound_id: int, part, path, text: str) -> str:
        """0.7.9: a sticker the owner sends goes into her collection (VIOLET_STICKER_AUTO_COLLECT); the chat sees
        that it was a sticker. Never raises."""
        from ai import stickers, trace
        text = text.replace("[用户发来一张图片]", "[用户发来一个表情包]", 1)
        if not stickers.enabled() or not env_bool("VIOLET_STICKER_AUTO_COLLECT", True):
            trace.step("收藏表情包", 来源="你发的", 结果="没收藏", 原因="表情包或“收藏你发的表情包”关着")
            return text
        match = re.search(r"图片内容[^：\n]*：(.+)", text)
        description = match.group(1).strip()[:60] if match else None
        try:
            row = stickers.add_file(self.db, path, source="owner", description=description,
                                    file_key=str((part.data or {}).get("file") or "") or None)
        except Exception as exc:                             # noqa: BLE001
            logger.info("owner sticker not collected (inbound #%s): %s", inbound_id, exc)
            trace.step("收藏表情包", 来源="你发的", 结果="没收藏", 原因=str(exc)[:120])
            return text
        if not row.get("description"):
            described = stickers.describe(path)
            if described:
                row = stickers.update(self.db, row["id"], description=described) or row
        trace.step("收藏表情包", 来源="你发的", 编号=row["id"], 结果="已收藏",
                   说明=row.get("description") or "没有说明（识图没成功：模型暂时看不到它，之后自动补）")
        text += f"\n（这个表情包已经存进你的收藏，编号 {row['id']}）"
        self.db.update_inbound_messages([inbound_id], content=text)
        return text

    def _describe_image_item(self, inbound_id: int, item: Any, download: Callable | None = None) -> str:
        try:
            if download is None and not isinstance(item, dict):
                raise RuntimeError("channel is not available")
            path = download(item) if download is not None else self.client.download_media(item)
        except Exception as exc:
            self._bump("media_download_failures")
            logger.warning("image download failed for inbound #%s: %s", inbound_id, exc)
            self.db.update_inbound_messages([inbound_id], error=f"download_failed: {exc}"[:300])
            return IMAGE_DOWNLOAD_FAILED_TEXT
        from ai.images.chat_tools import intake_inbound_image
        from ai.images.service import images_enabled
        if images_enabled():
            # phase 6: validated, stored media asset + routed vision; the legacy vision is only a fallback
            text = intake_inbound_image(self.db, inbound_id, path, legacy_vision=self.vision)
            self.db.update_inbound_messages([inbound_id], content=text)
            return text
        try:
            description = self.vision(path)
        except Exception as exc:
            self._bump("vision_failures")
            logger.warning("vision failed for inbound #%s: %s", inbound_id, exc)
            self.db.update_inbound_messages([inbound_id], error=f"vision_failed: {exc}"[:300])
            return IMAGE_VISION_FAILED_TEXT
        text = f"[用户发来一张图片]\n图片内容：{description}"
        self.db.update_inbound_messages([inbound_id], content=text)
        return text

    # -- direct ingestion (compatibility with main.Eve_Message / Eve_File) ------
    def ingest_text(self, text: str) -> int | None:
        from ai.channels.base import Part
        inbound_id = self.db.add_inbound_message(f"local:{uuid.uuid4().hex}", None, 0, ITEM_TEXT, text, channel="wechat")
        if inbound_id is not None:
            self._dispatch_part(inbound_id, Part("text", text), text, ("wechat", ""))
        return inbound_id

    # -- turn processing -------------------------------------------------------
    def _enqueue_turn(self, turn: Turn) -> None:
        self.turn_queue.put(turn)

    def _worker_main(self) -> None:
        while not self._stop.is_set():
            try:
                turn = self.turn_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.process_turn(turn)
            except Exception:
                logger.exception("turn %s crashed the worker iteration", turn.turn_id)
            finally:
                self.turn_queue.task_done()

    def _agent_kwargs(self) -> dict[str, Any]:
        return {
            "intent_agent": self.agents["intent"],
            "chat_agent": self.agents["chat"],
            "performer_agent": self.agents["performer"],
            "memory_agent": self.agents["memory"],
        }

    def process_turn(self, turn: Turn) -> str:
        """Run one turn end to end. Returns the final inbound status. Phase 10.6: the turn id is the correlation id of
        every log line and diagnosis event written while it runs (also while sending)."""
        from ai import test_mode
        from ai.diagnostics import turn_scope
        with turn_scope(turn.turn_id):
            if not test_mode.active():
                return self._process_turn(turn)
            # 2026-10-30: test mode -- the turn's steps are collected and sent after the reply as a diagnosis
            with test_mode.collect() as steps:
                return self._process_turn(turn, test_steps=steps)

    def _process_turn(self, turn: Turn, test_steps: list | None = None) -> str:
        combined = turn.combined
        testing = test_steps is not None                 # test mode: nothing is learned from this turn
        self.db.update_inbound_messages(turn.inbound_ids, status="processing", turn_id=turn.turn_id, increment_attempts=True)
        conversation = (turn.channel or "wechat", turn.peer_id or "")
        from ai import trace
        peer = conversation[1]
        trace.step("收到消息", 渠道=conversation[0], 对方=(peer[:4] + "…" + peer[-3:]) if len(peer) > 10 else peer or None,
                   条数=len(turn.inbound_ids) or None, 内容=trace.text(combined))
        try:
            sender = self.sender_for(conversation)
        except LookupError:
            sender = None                       # the channel was removed while this turn waited
        if sender is not None:
            sender.typing(TYPING_OFF)
            if self._answer_approval(turn, combined, conversation, sender):
                return "done"
        status, error = "done", None
        pipeline: dict[str, Any] | None = None
        try:
            if not testing:
                self._before_pipeline(combined, turn.turn_id)
        except Exception:
            logger.exception("engagement / preference bookkeeping failed for turn %s", turn.turn_id)
        extra = {"defer_memory": True, "turn_id": turn.turn_id} if self.async_memory else {"turn_id": turn.turn_id}
        if turn.inbound_ids:
            extra["turn_key"] = "in:" + "-".join(str(i) for i in sorted(turn.inbound_ids))
        sent_texts: list[str] = []
        report = None
        memory_job = False
        from ai.channels.context import channel_scope
        with self.pipeline_lock:
            try:
                if sender is None:
                    raise LookupError(f"channel {conversation[0]} is not available any more")
                with channel_scope(self.channel_context(conversation[0])):
                    pipeline = self.pipeline_runner(user_message=combined, **self._agent_kwargs(), **extra)
            except Exception as exc:
                logger.exception("pipeline failed for turn %s", turn.turn_id)
                error = f"pipeline: {exc.__class__.__name__}: {exc}"[:500]
            finally:
                # Always persist the user's words once, whatever happened above.
                message_id = None
                try:
                    message_id = self.db.add_user_message("text", combined, created_at=turn.received_at,
                                                          channel=conversation[0])
                    self.db.update_inbound_messages(turn.inbound_ids, stored_message_id=message_id)
                    try:
                        from ai.images.media import MediaAssetStore
                        MediaAssetStore(self.db).link_message(turn.inbound_ids, message_id)
                    except Exception:
                        logger.exception("could not link images of turn %s", turn.turn_id)
                except Exception:
                    logger.exception("could not store user message for turn %s", turn.turn_id)
                # Queue memory consolidation (runs after the reply is sent).
                silent = pipeline is not None and pipeline.get("should_reply") is False
                # Phase 9.1 (A6): a silent turn ("晚安啦，对了明早八点去复查") may still carry news; only a bare
                # closing word ("晚安" / "嗯嗯") skips the memory call.
                from ai.memory_model import is_engaged_reply
                if self.memory_worker is not None and not testing and (not silent or is_engaged_reply(combined)):
                    try:
                        memory_job = self.memory_worker.enqueue(turn.turn_id, combined, message_id) is not None
                    except Exception:
                        logger.exception("could not queue memory job for turn %s", turn.turn_id)

            if pipeline is not None and pipeline.get("errors"):
                error = "; ".join(f"{k}: {v}" for k, v in pipeline["errors"].items())[:500]

            if sender is None:
                status = "failed"
                self._bump("turns_failed")
            elif pipeline is None or pipeline.get("chat_failed"):
                status = "failed"
                self._bump("turns_failed")
                report, sent_texts, error = self._send_fallback(sender, turn, error)
            else:
                # phase 10: the reply is built in ONE place from the Chat agent's final content (outbound contract)
                from ai.channels.outbound import build_reply
                envelope = build_reply(pipeline, turn_key=extra.get("turn_key") or turn.turn_id)
                if envelope.should_send:
                    report = sender.send_messages(envelope.send_list(), origin="reply", turn_id=turn.turn_id)
                    sent_texts = [m["content"] for m in report.sent]
                    if report.blocked:
                        # internal content in the reply: nothing of it went out; the person gets the fallback line
                        status = "failed"
                        self._bump("turns_reply_blocked")
                        error = f"reply blocked: {report.blocked}"
                        report, sent_texts, error = self._send_fallback(sender, turn, error)
                    elif report.failed or report.cancelled:
                        status = "failed"
                        self._bump("turns_send_failed")
                        error = f"send: {report.failed[0]['error'] if report.failed else 'cancelled'}"[:500]
        self.db.update_inbound_messages(turn.inbound_ids, status=status, error=error)
        trace.step("这一轮结束", 结果=status, 错误=error, 回复=trace.text("\n".join(sent_texts)) if sent_texts else "没有发出回复",
                   不回复=(pipeline or {}).get("should_reply") is False or None, 记忆整理="已排队" if memory_job else None)
        if testing and sender is not None:
            self._send_test_diagnosis(sender, turn, test_steps, report, status, error, pipeline)
        if pipeline is not None and sent_texts and not testing:
            try:
                self.tracker.note_eve_reply("\n".join(sent_texts), pipeline.get("recalled_memories") or [],
                                            pipeline.get("user_raised_memory_ids") or [])
            except Exception:
                logger.exception("could not record Violet follow-up topics")
            try:
                from ai.chat import note_life_mentions
                shown = [m.get("life_event_id") for m in getattr(report, "sent", []) if m.get("life_event_id")]
                note_life_mentions("\n".join(sent_texts), turn.combined, skip_event_ids=shown)
            except Exception:
                logger.exception("could not record Violet life mentions")
            try:
                from ai.chat import note_web_mentions
                note_web_mentions("\n".join(sent_texts), (pipeline.get("chat") or {}).get("web_finding_ids"))
            except Exception:
                logger.exception("could not record web finding mentions")
        if memory_job:
            # The reply (whatever was actually delivered) is known now.
            self.memory_worker.mark_ready(turn.turn_id, "\n".join(sent_texts) or None)
        self._bump("turns_processed")
        return status

    def _before_pipeline(self, text: str, turn_id: str) -> None:
        """Engagement (topic fatigue), implicit preferences and onboarding refusals for this turn."""
        last_eve = self.db.get_recent_messages(limit=1)
        violet_asked = bool(last_eve) and last_eve[-1]["role"] == "assistant" and \
            (last_eve[-1].get("content") or "").rstrip().endswith(("?", "？"))
        result = self.tracker.evaluate(text)
        category = result["category"]
        if any(source == "violet_proactive" for _, source in result["evaluated"]) and category != "neutral":
            self.profile.preferences.observe("proactive_chat", "welcome" if category == "engaged" else "less",
                                             "engagement", turn_id)
        if violet_asked and category != "neutral":
            self.profile.preferences.observe("follow_up_questions", "ok" if category == "engaged" else "less",
                                             "engagement", turn_id)
        self.onboarding.note_user_turn(text)
        if self.proactive_scheduler is not None and hasattr(self.proactive_scheduler, "on_user_engagement"):
            self.proactive_scheduler.on_user_engagement(category != "disengaged" or result["user_initiated"])

    def _answer_approval(self, turn: Turn, combined: str, conversation: tuple[str, str], sender: "MessageSender") -> bool:
        """2026-10-30 (ai/permissions.py): "同意 3" / "不同意 3" from the owner answers an approval -- carried out
        here, the outcome is the reply; no model call. False: not such a message (or no such approval)."""
        from ai import permissions
        command = permissions.owner_command(combined)
        if command is None:
            return False
        approve, number = command
        if not self.db.fetch_one("SELECT 1 FROM approvals WHERE id = ?", (number,)):
            return False                                  # "好 3" that is not about an approval: normal chat
        try:
            outcome = permissions.decide(self.db, number, approve)["result"]
        except ValueError as exc:
            outcome = str(exc)
        from ai.channels.outbound import fixed_text
        try:
            self.db.add_user_message("text", combined, created_at=turn.received_at, channel=conversation[0])
            sender.send_messages(fixed_text(outcome), origin="reply", turn_id=turn.turn_id)
        except Exception:
            logger.exception("approval answer of turn %s not sent", turn.turn_id)
        self.db.update_inbound_messages(turn.inbound_ids, status="done")
        self._bump("turns_processed")
        return True

    def _send_test_diagnosis(self, sender: "MessageSender", turn: Turn, steps: list, report, status: str,
                             error: str | None, pipeline: dict[str, Any] | None) -> None:
        """Test mode: after the reply, one message with what the code saw this turn (ai/test_mode.py). It is not
        kept in the chat history, so the next turn of the test does not read it as part of the conversation."""
        from ai import test_mode
        from ai.channels.outbound import fixed_text
        extra = []
        if pipeline is not None and pipeline.get("should_reply") is False:
            extra.append("- 回复：意图识别判断这条不用回复，所以没有回复")
        if report is not None:
            kinds: dict[str, int] = {}
            for m in report.sent:
                kinds[str(m.get("type") or "text")] = kinds.get(str(m.get("type") or "text"), 0) + 1
            names = {"text": "文字", "image": "图片", "sticker": "表情包"}
            if kinds:
                extra.append("- 发出：" + "，".join(f"{names.get(k, k)} {n} 条" for k, n in kinds.items()))
            for m in [*report.skipped, *report.failed]:
                kind = names.get(str(m.get("type") or "text"), m.get("type"))
                what = f"表情包 {m.get('sticker_id')}" if m.get("type") == "sticker" else kind
                extra.append(f"- 没发出：{what}（{m.get('error') or '跳过'}）")
        if status != "done" or error:
            extra.append(f"- 这一轮：{status}" + (f"（{error}）" if error else ""))
        try:
            from ai import usage
            u = usage.turn(self.db, turn.turn_id)
            if u["calls"]:
                extra.append(f"- 用量：{u['calls']} 次模型调用（{'、'.join(u['sources'])}），输入 {u['prompt']} token"
                             f"（命中缓存 {u['cached']}），输出 {u['completion']} token")
        except Exception:                                # noqa: BLE001
            pass
        try:
            text = test_mode.diagnosis(steps, extra, db=self.db)
            sender.send_messages(fixed_text(text), origin="test_diagnosis", turn_id=turn.turn_id, record_history=False)
        except Exception:
            logger.exception("test mode diagnosis not sent for turn %s", turn.turn_id)

    def _send_fallback(self, sender: "MessageSender", turn: Turn, error: str | None):
        fallback = env_str("VIOLET_FALLBACK_REPLY", DEFAULT_FALLBACK_REPLY)
        if not fallback:
            return None, [], error
        from ai.channels.outbound import fixed_text
        report = sender.send_messages(fixed_text(fallback), origin="fallback", turn_id=turn.turn_id)
        if report.failed:
            error = f"{error or ''}; fallback send failed: {report.failed[0]['error']}"[:500]
        return report, [m["content"] for m in report.sent], error

    # -- proactive sending (called by ProactiveScheduler) ---------------------
    def _proactive_sender(self) -> "MessageSender":
        try:
            return self.sender_for(self.proactive_conversation())
        except LookupError:
            return self.sender

    def send_proactive(self, messages: list[dict[str, Any]], proactive_event_id: int | None = None) -> dict[str, Any]:
        report = self._proactive_sender().send_messages(messages, origin="proactive", proactive_event_id=proactive_event_id)
        return report.to_dict()

    def send_system_text(self, text: str) -> bool:
        """System notices (e.g. startup banner): sent, logged in outbox, not added to chat history."""
        from ai.channels.outbound import fixed_text
        report = self._proactive_sender().send_messages(fixed_text(text), origin="system",
                                                        record_history=False)
        return report.ok

    def proactive_channel_context(self):
        """The channel a proactive message being written now will go to (prompt wording, channel note)."""
        return self.channel_context(self.proactive_conversation()[0])

    # -- crash recovery ----------------------------------------------------------
    def recover_pending(self) -> int:
        """Re-queue fresh items left in received/processing state by a previous crash.

        Also recovers interrupted background memory jobs.

        Items older than VIOLET_RECOVERY_MAX_AGE_SECONDS, or whose user message was
        already stored (the crash happened mid-reply), are not answered again;
        they are marked expired and their text is kept in the chat history.
        """
        try:
            from ai.reconcile import reconcile_interrupted
            reconcile_interrupted(self.db, self.started_at)
        except Exception:
            logger.exception("startup reconciliation failed")
        if self.memory_worker is not None:
            self.memory_worker.recover()
        try:
            imported = self.profile.facts.import_legacy_core_memories()
            if imported:
                logger.info("imported %d legacy core memories as unconfirmed profile candidates", imported)
        except Exception:
            logger.exception("legacy core memory import failed")
        rows = self.db.list_inbound_messages(statuses=["received", "processing"], limit=1000)
        if not rows:
            return 0
        now = datetime.fromisoformat(self.db.now())
        recover: list[dict[str, Any]] = []
        expire: list[dict[str, Any]] = []

        def answerable(row: dict[str, Any]) -> bool:
            # text, an image, or (phase 10) a voice / file / sticker row already rendered as safe text
            return row["item_type"] == ITEM_IMAGE or (row["item_type"] is not None and bool(row.get("content")))

        def text_of(row: dict[str, Any]) -> str:
            return self._recovered_image_text(row) if row["item_type"] == ITEM_IMAGE else row["content"]

        for row in rows:
            try:
                age = (now - datetime.fromisoformat(row["received_at"])).total_seconds()
            except (TypeError, ValueError):
                age = float("inf")
            if answerable(row) and row.get("stored_message_id") is None and age <= self.recovery_max_age:
                recover.append(row)
            else:
                expire.append(row)

        for row in expire:
            if answerable(row) and row.get("stored_message_id") is None and row.get("content"):
                message_id = self.db.add_user_message("text", text_of(row), created_at=row["received_at"],
                                                      channel=row.get("channel") or "wechat")
                self.db.update_inbound_messages([row["id"]], stored_message_id=message_id)
        if expire:
            self.db.update_inbound_messages([r["id"] for r in expire], status="expired",
                                            error="not answered: left over from a previous run")
            logger.warning("recovery: %d stale inbound item(s) marked expired", len(expire))

        conversations: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in recover:
            conversations.setdefault((row.get("channel") or "wechat", row.get("peer_id") or ""), []).append(row)
        for (channel, peer), group in conversations.items():
            turn = Turn([r["id"] for r in group], [text_of(r) for r in group], received_at=group[-1]["received_at"],
                        origin="recovered", channel=channel, peer_id=peer)
            self._enqueue_turn(turn)
            logger.warning("recovery: re-queued %d unanswered item(s) of %s as turn %s", len(group), channel,
                           turn.turn_id)
        self._bump("recovered_items", len(recover))
        return len(recover)

    @staticmethod
    def _recovered_image_text(row: dict[str, Any]) -> str:
        content = row.get("content") or ""
        return content if content.startswith("[用户发来一张图片]") else IMAGE_INTERRUPTED_TEXT
