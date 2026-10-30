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

from ai import stickers, trace
from ai.channels.onebot11 import is_sticker
from ai.settings import env_bool, env_float, env_int, env_str
from ai.social.store import SocialStore

logger = logging.getLogger("violet.social")
_ENGINES: dict[int, "SocialEngine"] = {}
_ENGINES_LOCK = threading.Lock()
ACTIONS = ("silent", "say", "reply", "wait")


def social_settings() -> dict[str, Any]:
    return {"private": private_policy(), "quiet_seconds": env_float("VIOLET_SOCIAL_QUIET_SECONDS", 15.0),
            "model": env_str("VIOLET_SOCIAL_MODEL", "") or None, "context": env_int("VIOLET_SOCIAL_CONTEXT_MESSAGES", 30)}


def private_policy() -> str:
    value = env_str("VIOLET_SOCIAL_PRIVATE", "all").lower()
    return value if value in ("off", "known", "all") else "all"


def engine_for(runtime) -> "SocialEngine":
    with _ENGINES_LOCK:
        engine = _ENGINES.get(id(runtime))
        if engine is None:
            engine = _ENGINES[id(runtime)] = SocialEngine(runtime)
            engine.resume_wakeups()
            engine.start_posting()
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
        self._post_timer = None

    def start_posting(self, interval: float = 1500.0) -> None:
        """0.7.9: every ~25 minutes she may post on QQ 空间 (ai/social/qzone.py decides whether); 2026-10-30: and the
        QQ account's favourite stickers are synced when it is time (stickers.sync_qq, every 6 hours)."""
        import random

        def run():
            from ai.social import qzone
            self._post_timer = None
            try:
                qzone.tick(self.runtime)
                adapter = qzone._qq(self.runtime)
                if adapter is not None and adapter.is_running():
                    stickers.sync_qq(self.db, adapter)
                self.initiative_tick()
            except Exception:                               # noqa: BLE001 -- the next round tries again
                logger.exception("social background round failed")
            finally:
                self.start_posting(interval)
        if self._post_timer is not None:
            return
        timer = self._timer_factory(interval + random.uniform(0, 300), run)
        timer.daemon = True
        self._post_timer = timer
        timer.start()

    def initiative_tick(self, rand=None) -> list[int]:
        """2026-10-30 (user: "在群里面只会有人发言之后才会冒泡"): in a group set to 自己决定 that has gone quiet, she
        is sometimes asked whether she wants to say something herself -- start a topic, share something, pick an old
        one up. The model decides (mostly silent); the group's daily limits apply. Not at night, not in a group nobody
        has spoken in for days, not right after she spoke. VIOLET_SOCIAL_INITIATIVE (on),
        VIOLET_SOCIAL_INITIATIVE_CHANCE (0.3 per ~25-minute round). Returns the groups asked."""
        import random
        from ai import test_mode
        if not env_bool("VIOLET_SOCIAL_INITIATIVE", True) or test_mode.active():
            return []
        rand = rand or random.random
        now = datetime.fromisoformat(self.db.now()[:19])
        if not 9 <= now.hour < 23:
            return []
        chance = max(0.0, min(1.0, env_float("VIOLET_SOCIAL_INITIATIVE_CHANCE", 0.3)))
        asked = []
        for conv in self.db.fetch_all("SELECT id FROM social_conversations WHERE kind = 'group' AND mode = 'auto'"):
            row = self.db.fetch_one("SELECT MAX(created_at) AS last_any, MAX(CASE WHEN is_self = 1 THEN created_at END) "
                                    "AS last_self FROM social_messages WHERE conversation_id = ?", (conv["id"],))
            last_any, last_self = (row or {}).get("last_any"), (row or {}).get("last_self")
            if not last_any:
                continue
            quiet = now - datetime.fromisoformat(str(last_any)[:19])
            if quiet < timedelta(minutes=15) or quiet > timedelta(days=3):
                continue                                  # people are talking (the normal flow), or a dead group
            if last_self and now - datetime.fromisoformat(str(last_self)[:19]) < timedelta(minutes=90):
                continue
            if rand() >= chance:
                continue
            asked.append(conv["id"])
            self.schedule(conv["id"], delay=1.0, trigger="idle")
        return asked

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
        media = []
        for data in (images or [])[:3]:
            kind = "sticker" if is_sticker(data) else "image"
            key = str(data.get("file") or "") or None
            known = self.store.known_description(key)
            label = "表情包" if kind == "sticker" else "图片"
            media.append({"kind": kind, "file_key": key, "url": str(data.get("url") or "") or None, "description": known})
            content = (content + " " if content else "") + (f"[{label}：{known}]" if known else f"[{label}]")
        if reply_to:
            quoted = self.store.find_message(conv["id"], reply_to)
            if quoted is not None and quoted["is_self"]:
                mentioned = True                         # someone answered her
        row_id = self.store.add_message(conv["id"], content=content or "[消息]", sender_id=sender_id,
                                        sender_name=sender_name, platform_msg_id=platform_msg_id, reply_to=reply_to,
                                        mentions_self=mentioned)
        if row_id is None:
            return None
        for item in media:
            self.store.add_media(row_id, **item)
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
        from ai import test_mode
        with self._conv_lock(conversation_id):
            if not test_mode.active():
                return self._decide(conversation_id, trigger)
            # 2026-10-30 test mode: the decision's steps go into a diagnosis sent to the same chat
            with test_mode.collect() as steps:
                result = self._decide(conversation_id, trigger)
            if result is not None:
                self._send_test_diagnosis(conversation_id, result, steps)
            return result

    def _send_test_diagnosis(self, conversation_id: int, result: dict[str, Any], steps: list) -> None:
        """Only when there is something to explain: she sent something, was @-ed, wanted a sticker or to collect one,
        or something failed. A silent look at a busy group says nothing."""
        from ai import test_mode
        if not (result.get("sent") or result.get("mentioned") or result.get("error") or result.get("sticker_wanted")
                or result.get("collect_wanted") or result.get("owner_asked_sticker")):
            return
        conv = self.store.get(conversation_id)
        adapter = self._adapter(conv["channel"]) if conv else None
        if adapter is None or not adapter.is_running():
            return
        extra = [f"- 决定：{result.get('action')}（模型给的理由：{result.get('reason') or '没写'}）"]
        if result.get("cannot"):
            extra.append(f"- 模型说做不到的：{result['cannot']}")
        if not result.get("owner_known"):
            extra.append("- 没认出主人：这个 QQ 号没有绑定成主人，你的话会被当成普通群友的话、不照做"
                         "（渠道页 QQ 卡片 → 联系人里绑定）")
        if result.get("owner_asked_sticker") and not result.get("sticker_wanted"):
            numbers = result.get("sticker_numbers") or []
            extra.append("- 程序检查：你提到了表情包，但模型没有在 sticker 里选编号；" +
                         (f"它能用的编号是 {'、'.join(numbers)}" if numbers else "它没有能用的表情包（列表是空的）"))
        try:
            adapter.send_to(conv["kind"], conv["peer_id"], test_mode.diagnosis(steps, extra, db=self.db))
        except Exception:                                  # noqa: BLE001
            logger.exception("test mode diagnosis not sent to conversation %s", conversation_id)

    def _decide(self, conversation_id: int, trigger: str) -> dict[str, Any] | None:
        conv = self.store.get(conversation_id)
        if conv is None or conv["mode"] == "off":
            return None
        if trigger == "wake":
            self.store.update(conversation_id, next_wake_at=None)
        unread = self.store.unread(conv)
        newest = max([m["id"] for m in self.store.messages(conversation_id, limit=1)] or [conv["watermark"]])
        if not unread and trigger not in ("wake", "idle"):
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
        self._look_at_stickers(unread, adapter)
        from ai import test_mode
        testing = test_mode.active()       # 2026-10-30: no persona, no impressions, only the messages since the test began
        history = self.store.messages(conversation_id, limit=max(10, env_int("VIOLET_SOCIAL_CONTEXT_MESSAGES", 30)))
        from ai import context_cut
        history = context_cut.social_rows(conversation_id, history)      # 翻篇 / test mode
        speakers = sorted({m["sender_id"] for m in history if m.get("sender_id") and not m["is_self"]})
        people = self.store.people(conv["channel"], speakers) if speakers else []
        if testing:                                        # who is who (the owner is marked), but no impressions
            people = [{**p, "impression": None} for p in people]
        said = self.db.fetch_one("SELECT MAX(created_at) AS at FROM social_messages WHERE conversation_id = ? AND is_self = 1",
                                 (conversation_id,))
        from ai.social import llm, prompts
        sticker_lines = stickers.prompt_lines(self.db)
        obey = bool(owner_ids) and env_bool("VIOLET_SOCIAL_OBEY_OWNER", True)
        system = prompts.system_prompt(test_mode.TESTER_SOCIAL if testing else _persona_text(self.db, conv["channel"]),
                                       conv["kind"],
                                       testing=testing, obey_owner=obey)
        user = prompts.user_prompt({**conv, "notes": None} if testing else conv, history=history,
                                   unread_from=int(conv["watermark"] or 0), people=people,
                                   owner_ids=owner_ids, today=today, last_said_at=(said or {}).get("at"),
                                   now=self.db.now(), trigger=trigger, sticker_lines=sticker_lines)
        try:
            decision, model, latency = llm.decide(system, user)
        except llm.SocialLLMError as exc:
            self.store.record_decision(conversation_id, trigger=trigger, action="error", error=str(exc)[:300])
            trace.step("社交决定", 会话=conv["id"], 结果="失败", 原因=str(exc))
            return None
        result = self._act(conv, decision, adapter, today, newest, trigger, model, latency)
        result["mentioned"] = any(m["mentions_self"] for m in unread)
        if testing:
            # for the diagnosis: what the model said it could not do, and whether the owner asked for a sticker
            result["cannot"] = str(decision.get("cannot") or "").strip()[:200] or None
            result["owner_asked_sticker"] = any(str(m.get("sender_id")) in owner_ids and "表情" in (m.get("content") or "")
                                                for m in unread)
            result["sticker_numbers"] = [re.sub(r"^-\s*(\d+)：.*$", r"\1", line) for line in sticker_lines]
            result["owner_known"] = bool(owner_ids)
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
        cannot = str(decision.get("cannot") or "").strip()[:200]
        if cannot:                                       # what the owner asked for and she could not do (and why)
            reason = (reason + f"（做不到：{cannot}）").strip()
        error = None
        sticker = None
        wanted = str(decision.get("sticker") or "").strip().lstrip("#")
        if wanted.isdigit():
            sticker, problem = stickers.sendable(self.db, wanted)
            if problem:
                trace.step("发表情包", 编号=wanted, 结果="不能发", 原因=problem)
        elif wanted:
            trace.step("发表情包", 编号=wanted, 结果="不能发", 原因="不是收藏里的编号")
        if action in ("say", "reply") and not texts and sticker is None:
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
                if sticker is not None and error is None and not (
                        conv["daily_message_limit"] and today["messages"] + sent >= conv["daily_message_limit"]):
                    try:
                        msg_id = adapter.send_to(conv["kind"], conv["peer_id"], None,
                                                 reply_to=reply_to if action == "reply" and not sent else None,
                                                 sticker_path=str(stickers.path_of(sticker)))
                        label = f"[表情包：{sticker.get('description') or '表情'}]"
                        self.store.add_message(conv["id"], content=label, is_self=True, platform_msg_id=msg_id,
                                               message_type="sticker")
                        stickers.mark_used(self.db, sticker["id"])
                        texts.append(label)
                        sent += 1
                        trace.step("发表情包", 编号=sticker["id"], 结果="已发出")
                    except Exception as exc:          # noqa: BLE001
                        error = f"表情包没发出去：{exc}"[:300]
                        trace.step("发表情包", 编号=sticker["id"], 结果="失败", 原因=str(exc))
        collected = self._collect(conv, decision.get("collect"), adapter)
        if collected:
            reason = (reason + f"（收藏了表情包 {collected}）").strip()
        self.store.advance(conv["id"], max([newest] + [m["id"] for m in self.store.messages(conv["id"], limit=1)]))
        if action == "wait" and not check_back:
            check_back = 10.0
        if check_back:
            at = (datetime.fromisoformat(self.db.now()[:19]) + timedelta(minutes=check_back)).isoformat(timespec="seconds")
            self.store.update(conv["id"], next_wake_at=at)
            self.schedule(conv["id"], delay=check_back * 60, trigger="wake")
        from ai import test_mode
        if not test_mode.active():                       # test mode: no impressions, no group notes
            self._remember(conv, decision.get("remember"))
        self.store.record_decision(conv["id"], trigger=trigger, action=action, reply_to=reply_to,
                                   text="\n".join(texts[:sent] if sent else texts) or None,
                                   wait_minutes=check_back or None, reason=reason, model=model, latency_ms=latency,
                                   error=error)
        trace.step("社交决定", 会话=f"{conv['kind']}:{conv['peer_id']}", 触发=trigger, 决定=action, 发出=sent,
                   过会儿再看=f"{check_back:g} 分钟" if check_back else None, 理由=trace.text(reason), 错误=error,
                   模型=model, 耗时毫秒=latency)
        return {"action": action, "sent": sent, "check_back": check_back, "error": error, "reason": reason,
                "sticker_wanted": bool(wanted), "collect_wanted": bool(decision.get("collect"))}

    # -- stickers (0.7.9) ---------------------------------------------------------------------------------------
    def _download(self, adapter, url: str | None):
        from ai.channels.base import Part
        if adapter is None or not url or not hasattr(adapter, "download"):
            return None
        try:
            return adapter.download(Part("image", "", {"url": url}))
        except Exception as exc:                          # noqa: BLE001 -- an expired address, a network error
            logger.info("social picture download failed: %s", exc)
            return None

    def _look_at_stickers(self, unread: list[dict[str, Any]], adapter, limit: int = 3) -> None:
        """New stickers she has not seen before: one line each from the picture-recognition model, remembered by
        the picture's QQ file name so the same sticker is never looked at twice (VIOLET_STICKER_LOOK)."""
        if not unread or not stickers.enabled() or not env_bool("VIOLET_STICKER_LOOK", True):
            return
        for row in self.store.media([m["id"] for m in unread], kind="sticker", undescribed=True)[:limit]:
            known = self.store.known_description(row["file_key"])
            description = known
            if not description:
                path = self._download(adapter, row["url"])
                description = stickers.describe(path) if path is not None else None
            if description:
                self.store.describe_media(row["id"], description)
                trace.step("看表情包", 消息=row["message_id"], 内容=description)

    def _collect(self, conv: dict[str, Any], collect: Any, adapter) -> int | None:
        """She keeps a sticker (or a funny picture) someone sent: the decision's "collect" names the message."""
        if not collect or not stickers.enabled():
            return None
        meaning = None
        if isinstance(collect, dict):
            meaning = collect.get("meaning") or collect.get("note")
            collect = collect.get("message") or collect.get("id")
        ref = str(collect or "").strip().lstrip("#")

        def failed(why: str) -> None:                    # 2026-10-30: every reason a collect does not happen is visible
            trace.step("收藏表情包", 消息=ref or None, 结果="没收藏", 原因=why)
        if not ref.isdigit():
            failed("模型给的不是消息编号")
            return None
        message = self.store.find_message(conv["id"], ref)
        if message is None:
            row = self.db.fetch_one("SELECT * FROM social_messages WHERE id = ? AND conversation_id = ?",
                                    (int(ref), conv["id"]))
            message = row
        if message is None or message["is_self"]:
            failed("找不到这条消息" if message is None else "那是她自己发的消息")
            return None
        media = self.store.media([message["id"]])
        if not media:
            failed("那条消息里没有图片或表情包")
            return None
        item = next((m for m in media if m["kind"] == "sticker"), media[0])
        path = self._download(adapter, item["url"])
        if path is None:
            failed("下载图片失败（地址可能过期了）")
            return None
        description = item.get("description") or (str(meaning).strip()[:60] if meaning else None) or stickers.describe(path)
        try:
            row = stickers.add_file(self.db, path, source="group" if conv["kind"] == "group" else "private",
                                    description=description, file_key=item.get("file_key"),
                                    source_detail=conv.get("title") or conv["peer_id"])
        except Exception as exc:                          # noqa: BLE001
            logger.info("sticker not collected: %s", exc)
            failed(str(exc)[:120])
            return None
        self.db.execute("UPDATE social_media SET sticker_id = ? WHERE id = ?", (row["id"], item["id"]))
        trace.step("收藏表情包", 编号=row["id"], 内容=row.get("description"))
        return int(row["id"])

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


def _persona_text(db, channel: str | None = None) -> str:
    """The persona of the channel (2026-10-30: a channel may have its own) and the relationship to the owner."""
    try:
        from ai.persona import PersonaCore
        from ai.persona_templates import RELATIONSHIPS, relationship
        text = PersonaCore.load(channel=channel).render()
        rel = relationship(channel)
        call = f"你称呼主人“{rel['call']}”。" if rel["call"] else ""
        return text + f"\n【你和你的主人的关系：{rel['label']}】" + RELATIONSHIPS[rel["kind"]][1].replace("对方", "主人") + call
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
    posts = db.fetch_all("SELECT content, created_at FROM social_posts WHERE status = 'sent' AND created_at >= ? "
                         "ORDER BY id DESC LIMIT 3", (since,))
    from ai import context_cut                         # 2026-10-30: nothing from before a 翻篇 / the start of a test
    cut = max([t for t in (context_cut.owner_since(), context_cut.social_since()) if t] or [""])
    rows = [r for r in rows if str(r["created_at"]) >= cut]
    posts = [p for p in posts if str(p["created_at"]) >= cut]
    if not rows and not posts:
        return ""
    lines = [f"- {str(r['created_at'])[5:16]} 在「{r['title'] or r['peer_id']}」说：{(r['content'] or '')[:80]}"
             for r in reversed(rows)]
    lines += [f"- {str(p['created_at'])[5:16]} 你在 QQ 空间发了动态：{(p['content'] or '')[:80]}" for p in reversed(posts)]
    return "【你最近在 QQ 群里说过的话（你的主人可以知道；别人私下说的不要转述细节）】\n" + "\n".join(lines)
