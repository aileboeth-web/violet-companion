"""Scheduler for Violet's proactive message pipeline."""

import json
import logging
import random
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ai.chat import violetdb, run_commitment_pipeline, run_proactive_pipeline
from ai.chat_output import normalize_chat_output, strip_images
from ai.conversation_state import (VIOLET_SOURCES, LIFE_TOPIC, WEB_TOPIC, TopicTracker, life_topic_key, memory_topic_key,
                                   web_topic_key)
from ai.memory_model import PROGRESS_LABELS, event_deadline, parse_time
from ai.profile_schema import BUSY_VALUES_RE
from ai.profile_store import Profile

logger = logging.getLogger("violet.proactive")

CHINA_TZ = timezone(timedelta(hours=8))
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "proactive_config.json"

# Defaults for every key; proactive_config.json overrides them. Chat frequency is
# deliberately low; explicit reminders (commitments) are not limited by it.
DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "check_interval_seconds": 60,
    "quiet_hours": {"start": "23:30", "end": "08:00"},
    "daily_message_limit": 3,
    "min_minutes_after_user_message": 90,
    "min_minutes_between_proactive_messages": 180,
    "random_delay_minutes": {
        "morning": [90, 180],
        "noon": [90, 180],
        "afternoon": [120, 240],
        "evening": [90, 180],
        "night": [180, 300],
        "other": [120, 240],
    },
    # After proactive messages that got no / only a brief reply, wait longer
    # (interval x factor^cold_streak) and stop generic chat at max_cold_streak
    # until the user engages again. Reminders are not affected.
    "unanswered_backoff_factor": 2.0,
    "max_backoff_multiplier": 8,
    "max_cold_streak": 2,
    # Fixed-time tasks: once per day per task, outside quiet hours, and not
    # right after another proactive message.
    "fixed_task_min_gap_minutes": 30,
    # Phase 3: a planned event the user raised themselves recently may be
    # mentioned ONCE before it happens (e.g. wishing them luck). Never after it
    # happened: no "how did it go" follow-ups.
    "event_topics": {
        "enabled": True,
        "lookahead_hours": 24,
        "max_days_since_user_mention": 2,
    },
    "commitments": {
        "enabled": True,
        "respect_quiet_hours": True,
        "max_attempts": 3,
        "retry_minutes": 5,
    },
    "allowed_tasks": {},
}


# Phase 8.9.5: the check-in question every real proactive message ended with (8.9.4 A1/A2/A3).
CHECKIN_RE = re.compile(r"(今天|今晚|今早|最近|这两天|这几天)[^，。！？!?\n]{0,8}(怎么样|怎么过|还好吗|还顺|顺不顺|累不累|忙不忙|过得)|"
                        r"吃(饭)?了(吗|没)|吃得怎么样|在干嘛|在做什么|起床了(吗|没)")


def now_china() -> datetime:
    """Current time in the configured zone (VIOLET_TIMEZONE; name kept for compatibility)."""
    from ai.settings import now_local
    return now_local()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_zone())
    return parsed.astimezone(_zone())


def _zone():
    from ai.settings import local_timezone
    return local_timezone()


def time_bucket(current: datetime | None = None) -> str:
    hour = (current or now_china()).hour
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 14:
        return "noon"
    if 14 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    if 23 <= hour or hour < 5:
        return "night"
    return "other"


def minutes_since(value: str | None, current: datetime | None = None) -> float | None:
    parsed = parse_iso(value)
    if not parsed:
        return None
    return ((current or now_china()) - parsed).total_seconds() / 60


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
        return dict(default)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(default)
    if not isinstance(loaded, dict):
        return dict(default)
    return deep_merge(default, loaded)


class ProactiveScheduler:
    """Background scheduler that triggers the proactive pipeline."""

    def __init__(
        self,
        proactive_agent,
        chat_agent,
        performer_agent,
        send_messages: Callable[[list[dict[str, Any]], int], dict[str, Any] | None],
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        is_busy: Callable[[], bool] | None = None,
        pipeline_lock=None,
    ):
        self.proactive_agent = proactive_agent
        self.chat_agent = chat_agent
        self.performer_agent = performer_agent
        self.send_messages = send_messages
        self.config_path = Path(config_path)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # Shared with the message runtime so a proactive run never overlaps a
        # user turn (the agents are shared objects).
        self._is_busy = is_busy or (lambda: False)
        self._pipeline_lock = pipeline_lock or threading.RLock()
        self.tracker = TopicTracker(violetdb)
        self.profile = Profile(violetdb)
        self.config = self.load_config()

    def attach_runtime(self, is_busy: Callable[[], bool], pipeline_lock, channel_context: Callable | None = None) -> None:
        self._is_busy = is_busy
        self._pipeline_lock = pipeline_lock
        if channel_context is not None:
            self._channel_context = channel_context

    def _channel_scope(self):
        """Phase 10: a proactive message is written for the channel it will be sent on."""
        from ai.channels.context import channel_scope
        provider = getattr(self, "_channel_context", None)
        try:
            context = provider() if provider is not None else None
        except Exception:
            logger.exception("[ProactiveScheduler] channel of the next proactive message unknown; using WeChat")
            context = None
        return channel_scope(context)

    def load_config(self) -> dict[str, Any]:
        self.config = load_json(self.config_path, DEFAULT_CONFIG)
        try:                                          # phase 9.0: the Control Center's saved proactive settings
            from ai.control_config import store
            overrides = store().proactive_overrides()
            if overrides:
                self.config = deep_merge(self.config, overrides)
        except Exception:
            logger.exception("[ProactiveScheduler] control center overrides unavailable; using the config file")
        return self.config

    def reload(self) -> None:
        with self._lock:
            self.load_config()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def on_user_message(self) -> None:
        with self._lock:
            self.load_config()
            self._set_next_proactive_at(self._random_next_time(now_china()))

    def on_user_engagement(self, engaged: bool) -> None:
        """Called once a user turn is classified: a brief reply keeps the backoff."""
        with self._lock:
            self.load_config()
            current = now_china()
            self._set_next_proactive_at(self._random_next_time(current) if engaged else self._backoff_next_time(current))

    def _get_next_proactive_at(self) -> datetime | None:
        return parse_iso(violetdb.get_proactive_setting("next_proactive_at"))

    def _set_next_proactive_at(self, value: datetime) -> None:
        violetdb.set_proactive_setting("next_proactive_at", value.isoformat())

    def _random_next_time(self, current: datetime) -> datetime:
        bucket = time_bucket(current)
        ranges = self.config.get("random_delay_minutes", {})
        min_delay, max_delay = ranges.get(bucket) or ranges.get("other") or [60, 150]
        return current + timedelta(minutes=random.randint(int(min_delay), int(max_delay)))

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("[ProactiveScheduler] tick failed")
            interval = int(self.config.get("check_interval_seconds", 60))
            self._stop_event.wait(max(5, interval))

    def tick(self) -> None:
        # A user turn is buffered / queued / running: try again next tick
        # (checked before fixed tasks are marked as done for today).
        if self._is_busy():
            return
        if not self._pipeline_lock.acquire(blocking=False):
            return
        try:
            with self._channel_scope():
                self._tick_locked()
        finally:
            self._pipeline_lock.release()

    def _tick_locked(self) -> None:
        with self._lock:
            self.load_config()
            if not self.config.get("enabled", True):
                return
            current = now_china()
            violetdb.refresh_event_states()
            self.tracker.expire_unanswered()

        # 明确约定的提醒：独立于普通闲聊的频率限制
        if self._deliver_due_commitments(current):
            return

        with self._lock:
            # 固定时间任务检查（独立于随机调度，但仍有安全/重复检查）
            fixed_tasks = self._get_due_fixed_tasks(current)
            if fixed_tasks:
                event_context = self._build_event_context(current, task_whitelist=fixed_tasks)
                event_context["event_type"] = "fixed_time_task_check"

        if fixed_tasks:
            logger.info("[ProactiveScheduler] triggering fixed-time task check: %s", fixed_tasks)
            pipeline = run_proactive_pipeline(
                event_context=event_context,
                proactive_agent=self.proactive_agent,
                chat_agent=self.chat_agent,
                performer_agent=self.performer_agent,
            )
            self._handle_pipeline_result(pipeline, event_context, kind="fixed")
            return

        with self._lock:
            due, reason = self._is_due(current)
            if not due:
                return
            event_context = self._build_event_context(current)
            self._set_next_proactive_at(self._random_next_time(current))

        logger.info("[ProactiveScheduler] triggering proactive pipeline: %s", reason)
        pipeline = run_proactive_pipeline(
            event_context=event_context,
            proactive_agent=self.proactive_agent,
            chat_agent=self.chat_agent,
            performer_agent=self.performer_agent,
        )
        self._handle_pipeline_result(pipeline, event_context, kind="scheduled")

    def status_snapshot(self) -> dict[str, Any]:
        with self._lock:
            self.load_config()
            current = now_china()
            due, reason = self._is_due(current)
            return {
                "enabled": self.config.get("enabled", True),
                "current_time": current.isoformat(),
                "next_proactive_at": violetdb.get_proactive_setting("next_proactive_at"),
                "last_user_message_at": violetdb.get_latest_message_at(role="user"),
                "last_proactive_sent_at": violetdb.get_latest_proactive_event_at(status="sent"),
                "minutes_since_user_message": minutes_since(violetdb.get_latest_message_at(role="user"), current),
                "minutes_since_last_proactive": minutes_since(violetdb.get_latest_proactive_event_at(status="sent"), current),
                "daily_sent_count": self._today_sent_count(current),
                "daily_message_limit": self.config.get("daily_message_limit"),
                "is_due": due,
                "due_reason": reason,
                "cold_streak": self.tracker.cold_streak(),
                "cooling_topics": self.tracker.cooling_topics(),
                "pending_commitments": violetdb.list_commitments(["pending", "sending"], limit=20),
                "allowed_tasks": self._build_event_context(current)["allowed_tasks"],
            }

    def force_run(
        self,
        send: bool = False,
        event_context_patch: dict[str, Any] | None = None,
        force_task: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self.load_config()
            current = now_china()
            event_context = self._build_event_context(current)
            if force_task:
                task_config = self.config.get("allowed_tasks", {}).get(force_task)
                if not task_config:
                    raise ValueError(f"Unknown proactive task: {force_task}")
                event_context["allowed_tasks"] = {
                    force_task: {
                        **task_config,
                        "enabled": True,
                        "name": force_task,
                        "daily_used": self._task_day_count(force_task, current),
                        "weekly_used": self._task_week_count(force_task, current),
                    }
                }
                event_context["task_block_reason"] = None
                event_context["force_task"] = force_task
                event_context["test_instruction"] = f"这是手动测试，请优先选择任务 {force_task}，不要因为真实时间窗或概率跳过。"
            if event_context_patch:
                event_context = deep_merge(event_context, event_context_patch)
            event_context["event_type"] = "manual_proactive_test"
            event_context["manual_test"] = True

        if not self._pipeline_lock.acquire(timeout=180):
            raise RuntimeError("pipeline busy: a user turn is still being processed")
        try:
            with self._channel_scope():
                pipeline = run_proactive_pipeline(
                    event_context=event_context,
                    proactive_agent=self.proactive_agent,
                    chat_agent=self.chat_agent,
                    performer_agent=self.performer_agent,
                )
                if send:
                    self._handle_pipeline_result(pipeline)
        finally:
            self._pipeline_lock.release()
        return pipeline

    def _is_due(self, current: datetime) -> tuple[bool, str]:
        from ai import test_mode
        if test_mode.active():
            return False, "test mode"                    # 2026-10-30: nothing by herself while testing
        next_at = self._get_next_proactive_at()
        if not next_at:
            self._set_next_proactive_at(self._random_next_time(current))
            return False, "next proactive time initialized"
        if current < next_at:
            return False, "not due yet"
        if self._in_quiet_hours(current):
            self._set_next_proactive_at(self._random_next_time(current))
            return False, "quiet hours"
        since_user = minutes_since(violetdb.get_latest_message_at(role="user"), current)
        if since_user is None:
            return False, "no user message yet"
        min_after_user = float(self.config.get("min_minutes_after_user_message", 30))
        if since_user < min_after_user:
            return False, "too soon after user message"
        since_sent = minutes_since(violetdb.get_latest_proactive_event_at(status="sent"), current)
        min_between = float(self.config.get("min_minutes_between_proactive_messages", 120))
        if since_sent is not None and since_sent < min_between:
            return False, "too soon after proactive message"
        prefs = self.profile.preferences.effective()
        proactive_pref = prefs.get("proactive_chat") or {}
        if proactive_pref.get("origin") == "explicit" and proactive_pref.get("value") == "no":
            return False, "the user asked Violet not to start conversations"
        daily_limit = int(self.config.get("daily_message_limit", 3))
        if proactive_pref.get("origin") == "explicit" and proactive_pref.get("value") == "less":
            daily_limit = min(daily_limit, 1)
        busy = self._busy_situation()
        if busy is not None:
            return False, f"the user said they are busy ({busy['value']}) until {busy['expires_at'][5:16]}"
        # phase 9.4.4 (VIOLET_SCHEDULE_PROACTIVE): a class / exam by the timetable -- after the user's own wishes and
        # busy state, before the opportunity checks; it only ever adds a reason not to write
        gate = self._schedule_gate(current)
        if gate is not None and gate.blocked:
            self._postpone_past(gate)
            return False, gate.reason()
        if self._today_sent_count(current) >= daily_limit:
            return False, "daily proactive limit reached"
        cold = self.tracker.cold_streak()
        if cold >= int(self.config.get("max_cold_streak", 2)):
            return False, f"waiting for the user to re-engage (cold streak {cold})"
        return True, "due"

    def _in_quiet_hours(self, current: datetime) -> bool:
        quiet = self.config.get("quiet_hours") or {}
        start = quiet.get("start")
        end = quiet.get("end")
        if not start or not end:
            return False
        current_minutes = current.hour * 60 + current.minute
        start_minutes = self._hhmm_to_minutes(start)
        end_minutes = self._hhmm_to_minutes(end)
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes < end_minutes
        return current_minutes >= start_minutes or current_minutes < end_minutes

    @staticmethod
    def _hhmm_to_minutes(value: str) -> int:
        hour, minute = value.split(":", 1)
        return int(hour) * 60 + int(minute)

    def _day_start(self, current: datetime) -> str:
        return current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    def _week_start(self, current: datetime) -> str:
        start = current - timedelta(days=current.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    def _today_sent_count(self, current: datetime) -> int:
        # Explicit reminders do not use up the chat budget.
        return violetdb.count_proactive_events_since(self._day_start(current), status="sent",
                                                  exclude_event_types=("commitment_reminder",))

    def _task_week_count(self, task_name: str, current: datetime) -> int:
        return violetdb.count_proactive_events_since(
            self._week_start(current),
            action_type="task",
            task_name=task_name,
        )

    def _task_day_count(self, task_name: str, current: datetime) -> int:
        return violetdb.count_proactive_events_since(
            self._day_start(current),
            action_type="task",
            task_name=task_name,
        )

    def _get_due_fixed_tasks(self, current: datetime) -> list[str]:
        """返回当前时间命中固定检查时间、且今天尚未检查过的任务名列表。"""
        due = []
        if self._in_quiet_hours(current):
            return due
        # A fixed-time task is still Violet starting a conversation (unlike a reminder the user asked for):
        # an explicit "do not start conversations" and a current busy situation always win.
        # Not marked as done, so nothing is queued up for later -- that day's check is simply skipped.
        proactive_pref = self.profile.preferences.effective().get("proactive_chat") or {}
        if proactive_pref.get("origin") == "explicit" and proactive_pref.get("value") == "no":
            return due
        if self._busy_situation() is not None:
            return due
        gate = self._schedule_gate(current)                # phase 9.4.4: same as busy -- skipped, not queued
        if gate is not None and gate.blocked:
            return due
        since_sent = minutes_since(violetdb.get_latest_proactive_event_at(status="sent"), current)
        if since_sent is not None and since_sent < float(self.config.get("fixed_task_min_gap_minutes", 30)):
            return due
        current_minutes = current.hour * 60 + current.minute
        # 允许在固定时间点前后 check_interval_seconds/2 的窗口内命中
        tolerance = int(self.config.get("check_interval_seconds", 60)) // 2
        for task_name, task_config in (self.config.get("allowed_tasks") or {}).items():
            if not task_config.get("enabled", False):
                continue
            fixed_time = task_config.get("fixed_check_time")
            if not fixed_time:
                continue
            target_minutes = self._hhmm_to_minutes(fixed_time)
            if abs(current_minutes - target_minutes) > tolerance:
                continue
            # 检查今天是否已经针对该任务做过 fixed_time 触发（避免同一分钟重复）
            checked_key = f"fixed_check_done_{task_name}_{current.date().isoformat()}"
            if violetdb.get_proactive_setting(checked_key):
                continue
            violetdb.set_proactive_setting(checked_key, "1")
            due.append(task_name)
        return due

    def _is_task_time_allowed(self, task_config: dict[str, Any], current: datetime) -> bool:
        windows = task_config.get("allowed_time_windows") or []
        if not windows:
            return True
        current_minutes = current.hour * 60 + current.minute
        for window in windows:
            start, end = window.split("-", 1)
            if self._hhmm_to_minutes(start) <= current_minutes <= self._hhmm_to_minutes(end):
                return True
        return False

    def _build_event_context(self, current: datetime, task_whitelist: list[str] | None = None) -> dict[str, Any]:
        allowed_tasks = {}
        blocked_reasons = []
        for task_name, task_config in (self.config.get("allowed_tasks") or {}).items():
            # 白名单模式：只包含指定任务，且强制启用（跳过概率/时间窗检查）
            if task_whitelist is not None:
                if task_name not in task_whitelist:
                    continue
                task_allowed = bool(task_config.get("enabled", False))
                # 白名单任务仍然检查次数限制
                daily_limit = int(task_config.get("daily_limit", 0) or 0)
                weekly_limit = int(task_config.get("weekly_limit", 0))
                if daily_limit and self._task_day_count(task_name, current) >= daily_limit:
                    task_allowed = False
                    blocked_reasons.append(f"{task_name} daily limit reached")
                if weekly_limit and self._task_week_count(task_name, current) >= weekly_limit:
                    task_allowed = False
                    blocked_reasons.append(f"{task_name} weekly limit reached")
                allowed_tasks[task_name] = {
                    **task_config,
                    "enabled": task_allowed,
                    "name": task_name,
                    "daily_used": self._task_day_count(task_name, current),
                    "weekly_used": self._task_week_count(task_name, current),
                }
                continue

            task_allowed = bool(task_config.get("enabled", False))
            daily_limit = int(task_config.get("daily_limit", 0) or 0)
            weekly_limit = int(task_config.get("weekly_limit", 0))
            trigger_probability = float(task_config.get("trigger_probability", 1.0))
            if task_allowed and trigger_probability < 1.0 and random.random() > trigger_probability:
                task_allowed = False
                blocked_reasons.append(f"{task_name} skipped by trigger probability")
            if daily_limit and self._task_day_count(task_name, current) >= daily_limit:
                task_allowed = False
                blocked_reasons.append(f"{task_name} daily limit reached")
            if weekly_limit and self._task_week_count(task_name, current) >= weekly_limit:
                task_allowed = False
                blocked_reasons.append(f"{task_name} weekly limit reached")
            if not self._is_task_time_allowed(task_config, current):
                task_allowed = False
                blocked_reasons.append(f"{task_name} outside allowed time windows")
            allowed_tasks[task_name] = {
                **task_config,
                "enabled": task_allowed,
                "name": task_name,
                "daily_used": self._task_day_count(task_name, current),
                "weekly_used": self._task_week_count(task_name, current),
            }

        return {
            "event_type": "scheduled_proactive_check",
            "scheduled_at": current.isoformat(),
            "current_time": current.isoformat(),
            "time_bucket": time_bucket(current),
            "minutes_since_user_message": minutes_since(violetdb.get_latest_message_at(role="user"), current),
            "minutes_since_last_proactive": minutes_since(violetdb.get_latest_proactive_event_at(status="sent"), current),
            "daily_sent_count": self._today_sent_count(current),
            "daily_message_limit": self.config.get("daily_message_limit"),
            "allowed_tasks": allowed_tasks,
            "task_selection_instruction": (
                "allowed_tasks 是可选主动任务列表。每个任务都包含 description/task_info/constraints。"
                "如果选择任务，task.name 必须等于其中一个 key，task.description 应基于配置 description，"
                "task.task_info 和 task.constraints 应合并配置中的默认值，不要自行发明越权任务。"
            ),
            "task_block_reason": "; ".join(blocked_reasons) if blocked_reasons else None,
            "conversation_state": (state := self._conversation_state()),
            **self._motives(current, state),
            **self._schedule_context(current, state),           # phase 9.4.4 (off: nothing)
        }

    def _motives(self, current: datetime, state: dict[str, Any]) -> dict[str, Any]:
        """Phase 8.9.3: Violet's own reasons to get in touch (share / miss / ask_opinion), from the relationship and
        her life -- so a proactive message comes because she has something, not because it is morning."""
        try:
            from ai.daily_life import mentions_life
            from ai.life_store import LifeStore
            from ai.relationship import assess, proactive_motives
            view = assess(violetdb)
            minutes = minutes_since(violetdb.get_latest_message_at(role="user"), current)
            plans = LifeStore(violetdb).plans(["planned", "active"])
            # phase 8.9.5: a plan of hers that came up in the chats of the last two days can be picked up again
            since = current - timedelta(days=2)
            recent_text = [r["content"] or "" for r in violetdb.fetch_all(
                "SELECT content, created_at FROM messages ORDER BY id DESC LIMIT 120")
                if r["created_at"] and parse_time(r["created_at"]) >= since]
            talked = {p["id"] for p in plans if any(mentions_life(t, p["content"]) for t in recent_text)}
            motives = proactive_motives(view, None if minutes is None else minutes / 60, state.get("cold_streak", 0),
                                        state.get("allowed_life_events") or [], plans, talked,
                                        unanswered=self._unanswered_proactive(current),
                                        afterglow=self._afterglow(current))
            # phase 8.9.8: where the talk about it ended ("周末一起找吧 / 拍封面给你"), so it is not reopened as undecided
            from ai.daily_life import last_talk, talk_note
            contents = {p["id"]: p["content"] for p in plans}
            for motive in motives:
                if motive.get("type") == "continue" and motive.get("plan_id") in contents:
                    note = talk_note(last_talk(violetdb, contents[motive["plan_id"]], current))
                    if note:
                        motive["last_talk"] = note
            # phase 8.9.9: a plan she already brought up on her own since the user last wrote, with nothing new about it,
            # is not a new reason (ab-v2 step 9: the bookstore at 08:58, "already said" at 12:00 and 13:36, sent again
            # at 14:50 from the same inputs). Missing the user stays a reason of its own.
            raised = self._raised_unanswered(plans)
            motives = [m for m in motives if m.get("plan_id") not in raised]
        except Exception:
            logger.exception("[ProactiveScheduler] motives unavailable")
            return {}
        recent, habit_note = self._recent_proactive(current)
        out = {"relationship_stage": view.stage, "violet_motives": motives, "motive_note": (
            "violet_motives 是 Violet 此刻真有的、想找对方的理由（分享自己的小事、想念对方、想听对方的意见、接着聊之前的事、"
            "上一条很久没回时有点委屈地再找对方一次、接着刚才亲近的那份情绪）。刚聊完、气氛还热的时候，接着那份情绪往往比"
            "换个新话题自然。"
            "要主动联系时，优先从里面挑一个，写进 chat_brief.topic；一个都没有就不发也完全正常，"
            "不用为了发消息而按时间段问候。"), "recent_proactive": recent, "proactive_habit_note": habit_note}
        if raised:
            contents = {p["id"]: p["content"] for p in plans}
            out["already_raised"] = [{"about": contents[pid], "at": at.strftime("%m-%d %H:%M")} for pid, at in raised.items()]
            out["already_raised_note"] = ("already_raised 里的事，你在对方上次说话之后已经主动说过了，对方还没回，也没有新的进展："
                                          "不要换个说法再拿它找对方。")
        return out

    def _afterglow(self, current: datetime) -> dict[str, Any] | None:
        """Phase 8.9.11: the latest conversation was warm or playful on the user's side (ai.relationship WARM_RE /
        PLAYFUL_RE), it ended within 12 hours, and Violet has not written on her own since: {"at", "quote"}."""
        from ai.relationship import PLAYFUL_RE, is_warm
        if self._unanswered_proactive(current):
            return None
        rows = violetdb.fetch_all("SELECT role, content, created_at FROM messages ORDER BY id DESC LIMIT 60")
        talk, previous = [], None
        for row in rows:                                  # newest first, back to the start of that conversation
            at = parse_time(row["created_at"]) if row.get("created_at") else None
            if at is None or (previous is not None and previous - at >= timedelta(hours=3)):
                break
            talk.append({"role": row["role"], "text": row["content"] or "", "at": at})
            previous = at
        users = [m for m in talk if m["role"] == "user"]
        if not users or current - talk[0]["at"] > timedelta(hours=12):
            return None
        warm = next((m for m in users if is_warm(m["text"]) or PLAYFUL_RE.search(m["text"])), None)
        if warm is None:
            return None
        return {"at": warm["at"].strftime("%H:%M"), "quote": warm["text"].strip()[:30]}

    def _unanswered_proactive(self, current: datetime) -> list[dict[str, Any]]:
        """Phase 8.9.10: the messages Violet sent on her own since the user's latest message (reminders aside), oldest
        first: {"at": "HH:MM", "hours": hours ago rounded for wording, "elapsed_hours": the exact hours}. Gates use
        elapsed_hours (phase 9.1.1: 5 h 57 min rounded to 6.0 and passed a 6-hour gate)."""
        last_user = violetdb.get_latest_message_at(role="user")
        last_user_at = parse_time(last_user) if last_user else None
        out = []
        for row in violetdb.fetch_all("SELECT created_at FROM proactive_events WHERE status = 'sent' AND event_type != ? "
                                   "ORDER BY id DESC LIMIT 20", ("commitment_reminder",)):
            at = parse_time(row["created_at"]) if row["created_at"] else None
            if at is None or (last_user_at is not None and at <= last_user_at):
                continue
            elapsed = (current - at).total_seconds() / 3600
            out.append({"at": at.strftime("%H:%M"), "hours": round(elapsed, 1), "elapsed_hours": elapsed})
        return out[::-1]

    def _raised_unanswered(self, plans: list[dict[str, Any]]) -> dict[int, datetime]:
        """Phase 8.9.9: {plan id: when} for plans a sent proactive message (its text, or the decision's reason / topic)
        brought up after the user's latest message -- unless a happened life event about the plan came after it."""
        from ai.daily_life import mentions_life
        from ai.life_store import LifeStore
        last_user = violetdb.get_latest_message_at(role="user")
        last_user_at = parse_time(last_user) if last_user else None
        raised: dict[int, datetime] = {}
        for row in violetdb.fetch_all(
                "SELECT created_at, sent_messages, decision FROM proactive_events WHERE status = 'sent' AND event_type != ? "
                "ORDER BY id DESC LIMIT 20", ("commitment_reminder",)):
            at = parse_time(row["created_at"]) if row["created_at"] else None
            if at is None or (last_user_at is not None and at <= last_user_at):
                continue
            try:
                sent = json.loads(row["sent_messages"] or "[]")
                decision = json.loads(row["decision"] or "{}")
            except (TypeError, ValueError):
                sent, decision = [], {}
            brief = decision.get("chat_brief") if isinstance(decision.get("chat_brief"), dict) else {}
            text = "\n".join([*(str(m.get("content") if isinstance(m, dict) else m) for m in sent if m),
                              str(decision.get("reason") or ""), str(brief.get("topic") or "")])
            for plan in plans:
                if plan["id"] not in raised and mentions_life(text, plan["content"]):
                    raised[plan["id"]] = at
        if raised:
            contents = {p["id"]: p["content"] for p in plans}
            for event in LifeStore(violetdb).recent_happened(days=3):
                when = parse_time(event["occurred_at"])
                for pid, at in list(raised.items()):
                    if when > at and mentions_life(event["content"], contents[pid]):
                        del raised[pid]                  # something did happen about it since: new to tell
        return raised

    def _recent_proactive(self, current: datetime) -> tuple[list[dict[str, Any]], str | None]:
        """Phase 8.9.5: what Violet sent on her own lately (3 days), and -- when she keeps ending on a check-in question
        ("今天过得怎么样？", "吃得怎么样呀？", "准备怎么过呀？") -- a note to do it differently this time."""
        since = current - timedelta(days=3)
        recent = []
        for row in violetdb.fetch_all(
                "SELECT created_at, sent_messages FROM proactive_events WHERE status = 'sent' AND event_type != ? "
                "ORDER BY id DESC LIMIT 20", ("commitment_reminder",))[::-1]:
            if not row["created_at"] or parse_time(row["created_at"]) < since:
                continue
            try:
                sent = json.loads(row["sent_messages"] or "[]")
            except (TypeError, ValueError):
                sent = []
            text = "\n".join(str(m.get("content") if isinstance(m, dict) else m) for m in sent if m)
            if text:
                recent.append({"at": row["created_at"][5:16].replace("T", " "), "text": text,
                               "checkin": bool(CHECKIN_RE.search(text))})
        checkins = [r for r in recent if r["checkin"]]
        if not checkins:
            return recent, None
        example = checkins[-1]["text"].replace("\n", " ")[:40]
        return recent, (f"【你最近主动发的消息】这几天你主动找对方 {len(recent)} 次，有 {len(checkins)} 次是用问近况收尾的"
                        f"（比如“{example}”）。这次换个样子：只说你想说的那件事、一句想念或撒娇就好，不用再问对方怎么样。")

    def _handle_pipeline_result(self, pipeline: dict[str, Any], event_context: dict[str, Any] | None = None,
                                kind: str | None = None) -> None:
        """kind: "scheduled" / "fixed" for Violet-initiated runs of the scheduler (None: a manual force_run)."""
        event_id = pipeline.get("event_id")
        chat_result = pipeline.get("chat")
        from ai import trace
        trace.step("主动联系", 方式=kind or "手动测试", 事件=event_id, 决定=pipeline.get("proactive_decision"),
                   要发送=pipeline.get("should_send", True), 内容=trace.text((chat_result or {}).get("content")),
                   错误=pipeline.get("error"))
        if event_id is None or not chat_result or not pipeline.get("should_send", True):
            return
        event_id = int(event_id)
        content = chat_result.get("content", "")
        from ai.channels.outbound import chat_payload       # phase 10.6: the only way to sendable messages
        payload = chat_payload(content if isinstance(content, str) else "", images=False)
        if not payload.get("should_send", False):
            violetdb.update_proactive_event(event_id, status="skipped", reason="ChatAgent decided not to send")
            return
        if kind is not None and self._schedule() is not None:
            # phase 9.4.4: the rules again right before sending (only with the timetable feature on: off = as before)
            with self._lock:
                blocked = self._final_check(now_china(), kind)
            if blocked:
                logger.info("[ProactiveScheduler] not sent, final check: %s", blocked)
                violetdb.update_proactive_event(event_id, status="skipped", reason=f"final check: {blocked}"[:500])
                return
        messages = payload.get("messages", [])
        try:
            report = self.send_messages(messages, event_id)
        except Exception as exc:
            logger.exception("proactive send crashed for event %s", event_id)
            violetdb.update_proactive_event(event_id, status="failed", reason=f"send error: {exc}"[:500])
            return
        if report is None:
            # Legacy callback without a report: keep the old behaviour.
            violetdb.update_proactive_event(event_id, status="sent", sent_messages=messages)
            return
        sent = report.get("sent_messages") or []
        if sent:
            reason = None
            if report.get("failed_count") or report.get("cancelled_count"):
                reason = f"partially sent {len(sent)}/{len(messages)}: {report.get('error')}"[:500]
            violetdb.update_proactive_event(event_id, status="sent", sent_messages=sent, reason=reason)
            self._after_proactive_sent(pipeline, event_id)
            self._record_schedule_moments(pipeline, event_context, sent, event_id)
        else:
            violetdb.update_proactive_event(event_id, status="failed", sent_messages=[],
                                         reason=f"send failed: {report.get('error')}"[:500])

    # ------------------------------------------------------------------
    # Phase 2: conversation state, backoff, commitments
    # ------------------------------------------------------------------

    def _conversation_state(self) -> dict[str, Any]:
        """What the proactive decision may and may not touch (enforced in ai.chat)."""
        cooling = self.tracker.cooling_topics()
        cooling_memory_ids = {c.get("memory_id") for c in cooling if c.get("memory_id")}
        upcoming = self._mentionable_upcoming_events(cooling_memory_ids)
        upcoming_ids = {m["id"] for m in upcoming}
        restricted = [
            {
                "memory_id": m["id"],
                "content": m["content"],
                "reason": f"{m.get('kind')}: {PROGRESS_LABELS.get(m.get('progress'), m.get('progress')) or m.get('progress')}",
            }
            for m in violetdb.list_recent_event_memories(kinds=["event", "legacy", "background"], limit=20)
            if m["id"] not in upcoming_ids
        ]
        avoid = self.profile.preferences.effective().get("avoid_topic", [])
        restricted += [{"memory_id": None, "content": item["value"], "reason": "user asked not to dwell on this"}
                       for item in avoid]
        allowed = [
            {"id": m["id"], "content": m["content"], "type": "fact"}
            for m in violetdb.list_recent_event_memories(kinds=["fact"], limit=10)
            if m.get("status") == "active" and m["id"] not in cooling_memory_ids
        ]
        allowed += upcoming
        return {
            "cold_streak": self.tracker.cold_streak(),
            "cooling_topics": cooling,
            "restricted_memories": restricted,
            "allowed_memories": allowed,
            "allowed_life_events": self._shareable_life_events(),
            "allowed_web_findings": self._shareable_web_findings(),
            "avoid_topics": [item["value"] for item in avoid],
        }

    # ------------------------------------------------------------------
    # Phase 4: Violet's virtual life
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Phase 5B: things Violet found on the web (explorer findings only)
    # ------------------------------------------------------------------
    def _shareable_web_findings(self) -> list[dict[str, Any]]:
        """At most one candidate, after: freshness, the USER's interests, repeats, avoided topics,
        web-share spacing and cooling. Busy / quiet hours / caps / cold streak are checked in _is_due,
        and the model may still decide not to send (normal)."""
        try:
            from ai.memory_model import mentions_topic, touches_topic
            from ai.web.explorer import explore_config
            from ai.web.findings import FindingsStore
            from ai.web.providers import _bigrams
            cfg = explore_config().get("sharing") or {}
        except Exception:
            logger.exception("[web] sharing config unavailable")
            return []
        if not cfg.get("enabled", True):
            return []
        store = FindingsStore(violetdb)
        now = parse_time(violetdb.now())
        # the user did not engage with Violet's last web shares: stop until they do
        if self.tracker.topic_state(WEB_TOPIC)["unengaged"] >= int(cfg.get("cooling_after_disengaged_shares", 2)):
            return []
        last = violetdb.fetch_one("SELECT MAX(shared_with_user_at) AS t FROM web_findings")
        if last and last["t"] and now - parse_time(last["t"]) < timedelta(days=float(cfg.get("min_days_between_shares", 3))):
            return []
        interests = [f["value"] for f in self.profile.facts.list_facts(["active"], fact_key="interest")
                     if f.get("use_in_context", 1)]
        if not interests:
            return []                                   # nothing known about what the user cares for
        avoid = [item["value"] for item in self.profile.preferences.effective().get("avoid_topic", [])]
        shared = [(f["id"], f["title"]) for f in violetdb.fetch_all("SELECT id, title FROM web_findings WHERE share_count > 0")]
        result = []
        for f in store.list(limit=50, status="active", origin="explorer"):
            text = f"{f['title']} {f['summary'] or ''}"
            if f["share_count"] or not store.is_latest(f, float(cfg.get("freshness_days", 14))):
                continue
            if not any(mentions_topic(text, interest) for interest in interests):
                continue
            if any(touches_topic(text, a) or mentions_topic(text, a) for a in avoid):
                continue
            if self.tracker.topic_state(web_topic_key(f["id"]))["cooling"]:
                continue
            a = _bigrams(f["title"])          # another finding on the same story was already shared
            if any(a and len(a & _bigrams(t)) / len(a | _bigrams(t)) >= 0.5
                   for other_id, t in shared if other_id != f["id"] and _bigrams(t)):
                continue
            result.append({"id": f["id"], "title": f["title"], "domain": f["domain"], "summary": (f["summary"] or "")[:200],
                           "published_at": f["published_at"], "type": "web_finding",
                           "note": "外部网页资料，不是 Violet 的经历；可以说“看到一篇…”，要说明来源网站；不提也完全正常"})
        return result[: int(cfg.get("max_candidates", 1))]

    def _record_web_shares(self, finding_ids: list[int], proactive_event_id: int | None) -> None:
        from ai.web.findings import FindingsStore
        store = FindingsStore(violetdb)
        for finding_id in finding_ids:
            if not store.get(finding_id):
                continue
            store.mark_shared(finding_id)
            violetdb.add_topic_mention(web_topic_key(finding_id), "violet_web", proactive_event_id=proactive_event_id)
        if finding_ids:
            violetdb.add_topic_mention(WEB_TOPIC, "violet_web", proactive_event_id=proactive_event_id)

    def _shareable_life_events(self) -> list[dict[str, Any]]:
        """Recent happened events Violet has not shared yet. Only candidates: not sending is normal."""
        try:
            from ai.life_store import LifeStore
            from ai.persona import life_config
            cfg = life_config().get("sharing") or {}
        except Exception:
            logger.exception("[life] sharing config unavailable")
            return []
        if not cfg.get("enabled", True):
            return []
        # the user did not engage with Violet's last life shares: stop offering them until they do
        if self.tracker.topic_state(LIFE_TOPIC)["unengaged"] >= int(cfg.get("cooling_after_disengaged_shares", 2)):
            return []
        result = []
        for event in LifeStore(violetdb).recent_happened(days=float(cfg.get("recent_days", 3))):
            if event["source"] == "life_daily":
                continue                          # phase 8.9: everyday small things are for chat, not worth a message
            if int(event["share_count"]) >= int(cfg.get("max_shares_per_event", 1)):
                continue
            if self.tracker.topic_state(life_topic_key(event["id"]))["cooling"]:
                continue
            result.append({"id": event["id"], "content": event["content"], "occurred_at": event["occurred_at"],
                           "type": "violet_life_event", "note": "Violet 自己确实发生过的小事；可以随口提一句，也可以不提"})
        return result[-2:]

    def _record_life_shares(self, event_ids: list[int], proactive_event_id: int | None) -> None:
        from ai.life_store import LifeStore
        store = LifeStore(violetdb)
        for event_id in event_ids:
            if not store.event(event_id):
                continue
            store.mark_shared(event_id)
            violetdb.add_topic_mention(life_topic_key(event_id), "violet_life", proactive_event_id=proactive_event_id)
        if event_ids:
            violetdb.add_topic_mention(LIFE_TOPIC, "violet_life", proactive_event_id=proactive_event_id)

    def _mentionable_upcoming_events(self, cooling_memory_ids: set) -> list[dict[str, Any]]:
        """Planned events the user raised recently, happening soon, not yet mentioned by Violet."""
        cfg = self.config.get("event_topics") or {}
        if not cfg.get("enabled", True):
            return []
        now = parse_time(violetdb.now())
        horizon = now + timedelta(hours=float(cfg.get("lookahead_hours", 24)))
        fresh_after = now - timedelta(days=float(cfg.get("max_days_since_user_mention", 2)))
        result = []
        for m in violetdb.list_recent_event_memories(kinds=["event"], include_ended=False, limit=30):
            event_time = event_deadline(m.get("event_time"))     # phase 9.1.1: a date-only time lasts the whole day
            raised_by_user = parse_time(m.get("last_user_mention_at")) or parse_time(m.get("created_at"))
            if m.get("progress") != "planned" or not event_time or not (now < event_time <= horizon):
                continue
            if raised_by_user < fresh_after or m["id"] in cooling_memory_ids or m.get("status") != "active":
                continue
            violet_raised = violetdb.list_topic_mentions(topic_key=memory_topic_key(m["id"]), sources=VIOLET_SOURCES,
                                                   since=raised_by_user.isoformat(), limit=1)
            if violet_raised:
                continue
            result.append({
                "id": m["id"], "content": m["content"], "type": "upcoming_event", "event_time": m["event_time"],
                "note": "可以在事情发生之前自然地提一次（比如祝顺利），不要追问结果，之后也不要再提",
            })
        return result

    # ------------------------------------------------------------------
    # Phase 9.4.4: the timetable (VIOLET_SCHEDULE_ENABLED + VIOLET_SCHEDULE_PROACTIVE; both off = nothing here runs)
    # ------------------------------------------------------------------
    def _schedule(self):
        from ai.schedule import awareness
        if not awareness.enabled():
            return None
        from ai.schedule.store import ScheduleStore
        return awareness.Awareness(ScheduleStore(violetdb))

    def _schedule_gate(self, current: datetime):
        aware = self._schedule()
        if aware is None:
            return None
        gate = aware.gate(current)
        if gate.blocked:
            logger.info("[schedule-gate] %s until %s", gate.code, gate.until.strftime("%H:%M"))
        return gate

    def _postpone_past(self, gate) -> None:
        """The next look comes after the busy window plus the usual random wait: a suppressed message is not sent
        the moment class ends, and every other rule still applies then."""
        later = self._random_next_time(gate.until)
        current_next = self._get_next_proactive_at()
        if current_next is None or current_next < later:
            self._set_next_proactive_at(later)

    def _schedule_context(self, current: datetime, state: dict[str, Any]) -> dict[str, Any]:
        """Optional background for the decision: offered moments go into conversation_state (the decision may only
        name these ids); nothing at all when the feature is off or nothing applies."""
        aware = self._schedule()
        if aware is None:
            return {}
        avoid = [item["value"] for item in self.profile.preferences.effective().get("avoid_topic", [])]
        from ai.context_engine import PLAN_ANSWERED_KINDS, USER_STATED_SOURCES
        stated = any(s.get("source") in USER_STATED_SOURCES for s in self.profile.situations.current(list(PLAN_ANSWERED_KINDS)))
        moments = aware.moments(current, violetdb, avoid_topics=avoid, unanswered=bool(self._unanswered_proactive(current)),
                                user_stated_now=stated)
        if not moments:
            return {}
        state["allowed_schedule_moments"] = moments
        return {"schedule_note": (
            "conversation_state.allowed_schedule_moments 是按对方课表推算出的小背景（明天的考试、今天的课按计划上完了、"
            "这几天课多）。它们不是要发消息的理由，只有你本来就想找对方时才可以自然地用上一个；用了就把它的 id 写进 "
            "mentioned_schedule_ids。课表只是计划，不代表对方真实在哪、在做什么；不要编造课程细节。")}

    def _final_check(self, current: datetime, kind: str) -> str | None:
        """Phase 9.4.4: just before an Violet-initiated message goes out, the rules are looked at again -- the generation
        took time and the user may have become busy, asked for quiet or entered a class meanwhile."""
        if self._in_quiet_hours(current):
            return "quiet hours"
        pref = self.profile.preferences.effective().get("proactive_chat") or {}
        if pref.get("origin") == "explicit" and pref.get("value") == "no":
            return "the user asked Violet not to start conversations"
        busy = self._busy_situation()
        if busy is not None:
            return f"the user said they are busy ({busy['value']})"
        gate = self._schedule_gate(current)
        if gate is not None and gate.blocked:
            self._postpone_past(gate)
            return gate.reason()
        if kind == "scheduled":
            limit = int(self.config.get("daily_message_limit", 3))
            if pref.get("origin") == "explicit" and pref.get("value") == "less":
                limit = min(limit, 1)
            if self._today_sent_count(current) >= limit:
                return "daily proactive limit reached"
        return None

    def _busy_situation(self) -> dict[str, Any] | None:
        for situation in self.profile.situations.current(["availability", "activity"]):
            if BUSY_VALUES_RE.search(situation["value"]):
                return situation
        return None

    def _preference_multiplier(self) -> float:
        pref = self.profile.preferences.effective().get("proactive_chat") or {}
        if pref.get("value") != "less":
            return 1.0
        return 2.0 if pref.get("origin") == "explicit" else 1.5   # implicit: low confidence, gentle effect

    def _backoff_next_time(self, current: datetime) -> datetime:
        base = self._random_next_time(current)
        cold = self.tracker.cold_streak()
        factor = float(self.config.get("unanswered_backoff_factor", 2.0))
        multiplier = min(float(self.config.get("max_backoff_multiplier", 8)), factor ** cold) * self._preference_multiplier()
        return current + (base - current) * multiplier

    def _after_proactive_sent(self, pipeline: dict[str, Any], event_id: int) -> None:
        try:
            self.tracker.record_eve_topic(
                pipeline.get("topic_key"), pipeline.get("mentioned_memory_ids") or [], proactive_event_id=event_id,
            )
            self._record_life_shares(pipeline.get("mentioned_life_event_ids") or [], event_id)
            self._record_web_shares(pipeline.get("mentioned_web_finding_ids") or [], event_id)
            self._set_next_proactive_at(self._backoff_next_time(now_china()))
        except Exception:
            logger.exception("could not record proactive topic for event %s", event_id)

    def _record_schedule_moments(self, pipeline: dict[str, Any], event_context: dict[str, Any] | None,
                                 sent: list[Any], event_id: int) -> None:
        offered = ((event_context or {}).get("conversation_state") or {}).get("allowed_schedule_moments") or []
        if not offered:
            return
        try:
            from ai.schedule.awareness import record_mentions
            text = "\n".join(str(m.get("content") if isinstance(m, dict) else m) for m in sent if m)
            record_mentions(violetdb, offered, pipeline.get("proactive_decision") or {}, text, event_id)
        except Exception:
            logger.exception("could not record schedule moments for event %s", event_id)

    def _quiet_end(self, current: datetime) -> datetime:
        quiet = self.config.get("quiet_hours") or {}
        hour, minute = (quiet.get("end") or "08:00").split(":", 1)
        end = current.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if end <= current:
            end += timedelta(days=1)
        return end

    def _deliver_due_commitments(self, current: datetime) -> int:
        """Send reminders the user explicitly asked for. Not limited by chat caps."""
        cfg = self.config.get("commitments") or {}
        if not cfg.get("enabled", True):
            return 0
        due = violetdb.list_due_commitments(current.isoformat())
        if not due:
            return 0
        if cfg.get("respect_quiet_hours", True) and self._in_quiet_hours(current):
            postponed_to = self._quiet_end(current).isoformat()
            for commitment in due:
                violetdb.update_commitment(commitment["id"], next_attempt_at=postponed_to)
            logger.info("[commitments] %d reminder(s) postponed to the end of quiet hours", len(due))
            return 0

        delivered = 0
        for commitment in due:
            if not violetdb.claim_commitment(commitment["id"]):
                continue   # already being delivered elsewhere: never send twice
            commitment = violetdb.get_commitment(commitment["id"])
            event_id = violetdb.add_proactive_event(
                event_type="commitment_reminder",
                status="planned",
                action_type="chat",
                reason=f"commitment #{commitment['id']} due",
                decision={"commitment_id": commitment["id"], "content": commitment["content"]},
                metadata={"commitment": commitment},
                scheduled_at=commitment["due_at"],
            )
            result = run_commitment_pipeline(commitment, self.chat_agent)
            try:
                report = self.send_messages(result["messages"], event_id)
            except Exception as exc:
                logger.exception("[commitments] send crashed for #%s", commitment["id"])
                report = {"sent_messages": [], "error": f"{exc.__class__.__name__}: {exc}"}
            if report is None:
                report = {"sent_messages": result["messages"]}
            sent = report.get("sent_messages") or []
            if sent:
                violetdb.update_commitment(commitment["id"], status="sent", delivered_at=violetdb.now(),
                                        proactive_event_id=event_id, last_error=result.get("error"))
                violetdb.update_proactive_event(event_id, status="sent", sent_messages=sent,
                                             reason="fallback template used" if result.get("used_fallback") else None)
                self.tracker.record_eve_topic(f"commitment:{commitment['id']}", (), proactive_event_id=event_id,
                                              source="violet_commitment")
                delivered += 1
                continue
            error = str(report.get("error") or "not sent")[:500]
            max_attempts = int(cfg.get("max_attempts", 3))
            if commitment["attempts"] >= max_attempts:
                violetdb.update_commitment(commitment["id"], status="failed", last_error=error, proactive_event_id=event_id)
            else:
                retry_at = current + timedelta(minutes=float(cfg.get("retry_minutes", 5)))
                violetdb.update_commitment(commitment["id"], status="pending", last_error=error,
                                        next_attempt_at=retry_at.isoformat(), proactive_event_id=event_id)
            violetdb.update_proactive_event(event_id, status="failed", reason=f"send failed: {error}")
        return delivered
