"""Violet's social engine (2026-10-29): QQ groups and other people's private chats.

  receive()  store the message (only for conversations that are switched on), then wait until the chat is quiet for a
             moment (VIOLET_SOCIAL_QUIET_SECONDS; a few seconds when she is @-ed or in a private chat)
  decide()   one model call with the layered prompt (ai/social/prompts.py) that answers ONE fixed-format decision:
             silent / say / reply (quote) / wait -- the code acts on it; the model never calls tools here
  act        send through the QQ channel, store her own words, move the watermark (what she has looked at), schedule a
             wake-up when she wants to check back, apply what she wants to remember about the group / people

Per conversation: mode (off / observe / mention / auto), a daily limit of decisions and of her messages (0 = no
limit, the default). Every decision is recorded (social_decisions) and written to the troubleshooting log.
Nothing here touches the owner's chat, memories or context.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta
from typing import Any

from ai import trace
from ai.settings import env_float, env_int, env_str
from ai.social.store import SocialStore

logger = logging.getLogger("violet.social")
_ENGINES: dict[int, "SocialEngine"] = {}
_ENGINES_LOCK = threading.Lock()
ACTIONS = ("silent", "say", "reply", "wait")


def social_settings() -> dict[str, Any]:
    return {"private": private_policy(), "quiet_seconds": env_float("VIOLET_SOCIAL_QUIET_SECONDS", 15.0),
            "model": env_str("VIOLET_SOCIAL_MODEL", "") or None, "context": env_int("VIOLET_SOCIAL_CONTEXT_MESSAGES", 30)}


def private_policy() -> str:
    value = env_str("VIOLET_SOCIAL_PRIVATE", "known").lower()
    return value if value in ("off", "known", "all") else "known"


def engine_for(runtime) -> "SocialEngine":
    with _ENGINES_LOCK:
        engine = _ENGINES.get(id(runtime))
        if engine is None:
            engine = _ENGINES[id(runtime)] = SocialEngine(runtime)
            engine.resume_wakeups()
        return engine


class SocialEngine:
    def __init__(self, runtime, timer_factory=threading.Timer):
        self.runtime = runtime
        self.db = runtime.db
        self.store = SocialStore(self.db)
        self._timers: dict[int, threading.Timer] = {}
        self._first_pending: dict[int, float] = {}
        self._locks: dict[int, threading.Lock] = {}
        self._lock = threading.Lock()
        self._timer_factory = timer_factory

    # -- receiving --------------------------------------------------------------------------------------------------
    def receive(self, adapter, *, kind: str, peer_id: str, sender_id: str, sender_name: str | None, text: str,
                mentioned: bool = False, reply_to: str | None = None, platform_msg_id: str | None = None,
                images: list[dict[str, Any]] | None = None, group_name: str | None = None) -> int | None:
        channel = adapter.channel_id
        if kind == "private":
            policy = private_policy()
            known = self.db.fetch_one("SELECT 1 FROM social_people p JOIN social_messages m ON m.sender_id = p.user_id "
                                      "JOIN social_conversations c ON c.id = m.conversation_id AND c.kind = 'group' "
                                      "WHERE p.channel = ? AND p.user_id = ? LIMIT 1", (channel, sender_id))
            if policy == "off" or (policy == "known" and not known):
                trace.step("私聊未处理", 渠道=channel, QQ=sender_id, 原因="设置不允许" if policy == "off" else "不认识的人")
                return None
        conv = self.store.conversation(channel, kind, peer_id, group_name if kind == "group" else sender_name)
        if conv["mode"] == "off":
            return None                                  # a group she does not take part in: nothing is stored
        self.store.person(channel, sender_id, sender_name, seen=True)
        content = text or ""
        if images:
            content = (content + " " if content else "") + "[图片]" * min(len(images), 3)
        if reply_to:
            quoted = self.store.find_message(conv["id"], reply_to)
            if quoted is not None and quoted["is_self"]:
                mentioned = True                         # someone answered her
        row_id = self.store.add_message(conv["id"], content=content or "[消息]", sender_id=sender_id,
                                        sender_name=sender_name, platform_msg_id=platform_msg_id, reply_to=reply_to,
                                        mentions_self=mentioned)
        if row_id is None:
            return None
        trace.step("社交消息", 渠道=channel, 会话=f"{kind}:{peer_id}", 发送者=sender_name or sender_id,
                   提到她=mentioned or None, 内容=trace.text(content))
        if conv["mode"] != "observe":
            self.schedule(conv["id"], urgent=mentioned or kind == "private")
        return row_id

    def schedule(self, conversation_id: int, urgent: bool = False, delay: float | None = None,
                 trigger: str = "messages") -> None:
        """Debounce: decide once the chat was quiet for a moment, but never later than 4x that after the first one."""
        import time
        quiet = env_float("VIOLET_SOCIAL_QUIET_SECONDS", 15.0)
        if delay is None:
            delay = min(3.0, quiet) if urgent else quiet
            with self._lock:
                first = self._first_pending.setdefault(conversation_id, time.monotonic())
            delay = max(0.5, min(delay, quiet * 4 - (time.monotonic() - first)))
        with self._lock:
            old = self._timers.pop(conversation_id, None)
            if old is not None:
                old.cancel()
            timer = self._timer_factory(delay, self._run, args=(conversation_id, trigger))
            timer.daemon = True
            self._timers[conversation_id] = timer
        timer.start()

    def _run(self, conversation_id: int, trigger: str) -> None:
        with self._lock:
            self._timers.pop(conversation_id, None)
            self._first_pending.pop(conversation_id, None)
        try:
            self.decide(conversation_id, trigger)
        except Exception:
            logger.exception("social decision failed for conversation %s", conversation_id)

    def resume_wakeups(self) -> None:
        """After a restart: the wake-ups she had planned."""
        now = datetime.fromisoformat(self.db.now()[:19])
        for c in self.db.fetch_all("SELECT id, next_wake_at FROM social_conversations WHERE next_wake_at IS NOT NULL "
                                   "AND mode != 'off'"):
            try:
                at = datetime.fromisoformat(str(c["next_wake_at"])[:19])
            except ValueError:
                continue
            self.schedule(c["id"], delay=max(5.0, (at - now).total_seconds()), trigger="wake")

    # -- deciding ---------------------------------------------------------------------------------------------------
    def _conv_lock(self, conversation_id: int) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(conversation_id, threading.Lock())

    def _adapter(self, channel: str):
        manager = getattr(self.runtime, "channels", None)
        adapter = manager.get(channel) if manager is not None else None
        return adapter if adapter is not None and hasattr(adapter, "send_to") else None

    def decide(self, conversation_id: int, trigger: str = "messages") -> dict[str, Any] | None:
        with self._conv_lock(conversation_id):
            return self._decide(conversation_id, trigger)

    def _decide(self, conversation_id: int, trigger: str) -> dict[str, Any] | None:
        conv = self.store.get(conversation_id)
        if conv is None or conv["mode"] == "off":
            return None
        if trigger == "wake":
            self.store.update(conversation_id, next_wake_at=None)
        unread = self.store.unread(conv)
        newest = max([m["id"] for m in self.store.messages(conversation_id, limit=1)] or [conv["watermark"]])
        if not unread and trigger != "wake":
            return None
        if conv["mode"] == "observe":
            self.store.advance(conversation_id, newest)
            return None
        if conv["mode"] == "mention" and trigger == "messages" and not any(m["mentions_self"] for m in unread):
            self.store.advance(conversation_id, newest)
            return None
        today = self.store.today(conversation_id)
        if conv["daily_decision_limit"] and today["decisions"] >= conv["daily_decision_limit"]:
            self.store.advance(conversation_id, newest)
            trace.step("社交决定", 会话=conv["id"], 结果="今天的决定次数到上限了")
            return None
        adapter = self._adapter(conv["channel"])
        owner_ids = adapter.owner_ids() if adapter is not None and hasattr(adapter, "owner_ids") else set()
        history = self.store.messages(conversation_id, limit=max(10, env_int("VIOLET_SOCIAL_CONTEXT_MESSAGES", 30)))
        speakers = sorted({m["sender_id"] for m in history if m.get("sender_id") and not m["is_self"]})
        people = self.store.people(conv["channel"], speakers) if speakers else []
        said = self.db.fetch_one("SELECT MAX(created_at) AS at FROM social_messages WHERE conversation_id = ? AND is_self = 1",
                                 (conversation_id,))
        from ai.social import llm, prompts
        system = prompts.system_prompt(_persona_text(self.db), conv["kind"])
        user = prompts.user_prompt(conv, history=history, unread_from=int(conv["watermark"] or 0), people=people,
                                   owner_ids=owner_ids, today=today, last_said_at=(said or {}).get("at"),
                                   now=self.db.now(), trigger=trigger)
        try:
            decision, model, latency = llm.decide(system, user)
        except llm.SocialLLMError as exc:
            self.store.record_decision(conversation_id, trigger=trigger, action="error", error=str(exc)[:300])
            trace.step("社交决定", 会话=conv["id"], 结果="失败", 原因=str(exc))
            return None
        result = self._act(conv, decision, adapter, today, newest, trigger, model, latency)
        return result

    def _act(self, conv: dict[str, Any], decision: dict[str, Any], adapter, today: dict[str, int], newest: int,
             trigger: str, model: str, latency: int) -> dict[str, Any]:
        action = str(decision.get("action") or "silent").lower()
        if action not in ACTIONS:
            action = "silent"
        texts = decision.get("messages")
        if isinstance(texts, str):
            texts = [texts]
        texts = [re.sub(r"\s+\n", "\n", str(t)).strip()[:500] for t in (texts or []) if str(t or "").strip()][:3]
        reply_to = str(decision.get("reply_to") or "").lstrip("#").strip() or None
        try:
            check_back = float(decision.get("check_back_minutes") or 0)
        except (TypeError, ValueError):
            check_back = 0.0
        check_back = max(0.0, min(check_back, 24 * 60))
        reason = str(decision.get("reason") or "")[:300]
        error = None
        if action in ("say", "reply") and not texts:
            action = "silent"
        if action in ("say", "reply") and conv["daily_message_limit"] and today["messages"] >= conv["daily_message_limit"]:
            action, error = "silent", "今天说话的次数到上限了"
        sent = 0
        if action in ("say", "reply"):
            if adapter is None or not adapter.is_running():
                action, error = "silent", "QQ 没有连接，没有发出去"
            else:
                for i, text in enumerate(texts):
                    if conv["daily_message_limit"] and today["messages"] + sent >= conv["daily_message_limit"]:
                        break
                    try:
                        msg_id = adapter.send_to(conv["kind"], conv["peer_id"], text,
                                                 reply_to=reply_to if action == "reply" and i == 0 else None)
                    except Exception as exc:          # noqa: BLE001
                        error = f"发送失败：{exc}"[:300]
                        break
                    self.store.add_message(conv["id"], content=text, is_self=True, platform_msg_id=msg_id)
                    sent += 1
        self.store.advance(conv["id"], max([newest] + [m["id"] for m in self.store.messages(conv["id"], limit=1)]))
        if action == "wait" and not check_back:
            check_back = 10.0
        if check_back:
            at = (datetime.fromisoformat(self.db.now()[:19]) + timedelta(minutes=check_back)).isoformat(timespec="seconds")
            self.store.update(conv["id"], next_wake_at=at)
            self.schedule(conv["id"], delay=check_back * 60, trigger="wake")
        self._remember(conv, decision.get("remember"))
        self.store.record_decision(conv["id"], trigger=trigger, action=action, reply_to=reply_to,
                                   text="\n".join(texts[:sent] if sent else texts) or None,
                                   wait_minutes=check_back or None, reason=reason, model=model, latency_ms=latency,
                                   error=error)
        trace.step("社交决定", 会话=f"{conv['kind']}:{conv['peer_id']}", 触发=trigger, 决定=action, 发出=sent,
                   过会儿再看=f"{check_back:g} 分钟" if check_back else None, 理由=trace.text(reason), 错误=error,
                   模型=model, 耗时毫秒=latency)
        return {"action": action, "sent": sent, "check_back": check_back, "error": error}

    def _remember(self, conv: dict[str, Any], remember: Any) -> None:
        if not isinstance(remember, dict):
            return
        group = remember.get("group")
        if conv["kind"] == "group" and isinstance(group, str) and group.strip():
            self.store.update(conv["id"], notes=group.strip()[:300])
        people = remember.get("people")
        if isinstance(people, dict):
            for uid, impression in list(people.items())[:8]:
                uid = re.sub(r"\D", "", str(uid))
                if uid and isinstance(impression, str) and impression.strip():
                    exists = self.db.fetch_one("SELECT 1 FROM social_people WHERE channel = ? AND user_id = ?",
                                               (conv["channel"], uid))
                    if exists:
                        self.store.set_impression(conv["channel"], uid, impression.strip()[:200])


def _persona_text(db) -> str:
    try:
        from ai.persona import PersonaCore
        return PersonaCore.load().render()
    except Exception:                                 # noqa: BLE001
        logger.exception("persona could not be loaded for the social prompt")
        return "【你自己】一个普通、友善的人。"


def owner_digest(db, hours: float = 24.0, limit: int = 6) -> str:
    """For the owner's chat: what she said in groups lately (the owner may know about her group life; the reverse
    never happens). Empty when nothing."""
    since = (datetime.fromisoformat(db.now()[:19]) - timedelta(hours=hours)).isoformat(sep=" ")
    rows = db.fetch_all("SELECT c.title, c.peer_id, m.content, m.created_at FROM social_messages m "
                        "JOIN social_conversations c ON c.id = m.conversation_id WHERE m.is_self = 1 AND c.kind = 'group' "
                        "AND m.created_at >= ? ORDER BY m.id DESC LIMIT ?", (since, limit))
    if not rows:
        return ""
    lines = [f"- {str(r['created_at'])[5:16]} 在「{r['title'] or r['peer_id']}」说：{(r['content'] or '')[:80]}"
             for r in reversed(rows)]
    return "【你最近在 QQ 群里说过的话（你的主人可以知道；别人私下说的不要转述细节）】\n" + "\n".join(lines)
