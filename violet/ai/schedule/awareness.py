"""Phase 9.4.4: what Violet's proactive side may know from the timetable -- computed in code, never by a model or an MCP.

* busy windows (the gate): every occupying class of the computed day (normal, changed, moved in, extra) from 5 minutes
  before its start to 5 minutes after its end, every exam from 30 minutes before its start to its end; overlapping or
  touching windows are merged. Cancelled / moved-out classes, holidays and changes come from the engine's computed
  day, so a change in the Control Center counts at the next check. Outside an active term (no timetable, not started,
  ended) nothing is busy. An exam without times blocks nothing (its time is unknown).
* moments (optional background for ProactiveAgent, never a reason to send): an exam tomorrow, today's last class over
  (by the plan), a dense week. Each carries only a type, an id and what the wording needs (an exam's name, kind and
  start time; counts; an end time) -- never teacher, place, seat, notes or the timetable itself. Each is offered once
  (topic_mentions, so a restart remembers), not while a proactive message is still unanswered, and exam moments not
  when the user asked Violet to drop the subject (avoid_topic).
* the plan is not where the user is: every result says so, and the chat status line too.

Errors never pretend: a failing store gives state "error" -- the gate does not block (the existing rules apply as
before) and no moment is offered. Logs carry codes and times only.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ai.schedule import engine
from ai.schedule.model import ScheduleData, zone_of

logger = logging.getLogger("violet.schedule")

CLASS_BEFORE = timedelta(minutes=5)
CLASS_AFTER = timedelta(minutes=5)
EXAM_BEFORE = timedelta(minutes=30)
AFTER_CLASS_FRESH = timedelta(hours=3)
DENSE_DAY_CLASSES = 4                 # a day with at least this many classes counts as full
DENSE_WEEK_DAYS = 3                   # ... and this many full days in the next 7 make a dense week
EXAM_WORDS = re.compile(r"(考试|考|测验|期中|期末|exam|quiz)", re.I)
PLAN_ONLY = "这是按课表推算的计划，不代表对方此刻真的在上课 / 下课 / 在哪里；对方自己说的情况优先。"
TOPIC_PREFIX = "schedule:"


def enabled() -> bool:
    from ai.settings import env_bool
    return env_bool("VIOLET_SCHEDULE_ENABLED", False) and env_bool("VIOLET_SCHEDULE_PROACTIVE", False)


def context_enabled() -> bool:
    from ai.settings import env_bool
    return env_bool("VIOLET_SCHEDULE_ENABLED", False) and env_bool("VIOLET_SCHEDULE_CONTEXT", False)


@dataclass
class Gate:
    blocked: bool
    code: str                          # clear | in_class | exam | no_timetable | not_started | ended | between_terms |
    #                                    error
    until: datetime | None = None      # end of the merged busy window (blocked only)
    windows: list[tuple[datetime, datetime, str]] = field(default_factory=list)

    def reason(self) -> str:
        if not self.blocked:
            return f"schedule: {self.code}"
        what = "exam" if self.code == "exam" else "class"
        return f"schedule: {what} by the timetable until {self.until.strftime('%H:%M')}"


def _windows_for(data: ScheduleData, day: date) -> list[tuple[datetime, datetime, str]]:
    result = engine.day(data, day)
    if result.data_state != "active":
        return []
    out = []
    for item in result.items:
        if not item.occupies or item.start is None or item.end is None:
            continue
        if item.kind == "exam":
            out.append((item.start - EXAM_BEFORE, item.end, "exam"))
        else:
            out.append((item.start - CLASS_BEFORE, item.end + CLASS_AFTER, "class"))
    return out


def merge(windows: list[tuple[datetime, datetime, str]]) -> list[tuple[datetime, datetime, str]]:
    """Overlapping or touching windows become one (kind "exam" if any part is an exam)."""
    merged: list[list[Any]] = []
    for start, end, kind in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            if kind == "exam":
                merged[-1][2] = "exam"
        else:
            merged.append([start, end, kind])
    return [tuple(w) for w in merged]


class Awareness:
    def __init__(self, store, default_zone: str | None = None):
        self.store = store
        if default_zone is None:
            from ai.settings import env_str
            default_zone = env_str("VIOLET_TIMEZONE", "Asia/Shanghai") or "Asia/Shanghai"
        self.default_zone = default_zone

    def _load(self) -> ScheduleData:
        return self.store.load()

    def gate(self, moment: datetime) -> Gate:
        try:
            data = self._load()
            today, _zone = engine.local_date(data, moment, self.default_zone)
            state = engine.term_for(data, today)[1]
            windows = merge([w for d in (today - timedelta(days=1), today, today + timedelta(days=1))
                             for w in _windows_for(data, d)])
        except Exception as exc:                            # never pretend to know the plan
            logger.warning("[schedule-gate] timetable unavailable (%s): not used for this check", exc.__class__.__name__)
            return Gate(False, "error")
        for start, end, kind in windows:
            if start <= moment < end:
                return Gate(True, "exam" if kind == "exam" else "in_class", end, windows)
        code = {"active": "clear", "no_data": "no_timetable"}.get(state, state)
        return Gate(False, code, None, windows)

    # -------------------------------------------------------------------------------------------- moments
    def moments(self, moment: datetime, db, *, avoid_topics: list[str] | None = None,
                unanswered: bool = False, user_stated_now: bool = False) -> list[dict[str, Any]]:
        """The optional schedule background for one proactive check (possibly empty). user_stated_now: the user has
        said where they are / what they do / whether they are free (still valid) -- then "today's classes are over by
        the plan" is not offered: their own words outrank the plan (phase 9.4 real run, "下午实验课也请假了")."""
        if unanswered:
            return []                                       # never a reason to write again while unanswered
        try:
            data = self._load()
            today, zone = engine.local_date(data, moment, self.default_zone)
            local = moment.astimezone(zone_of(zone))
            out = []
            avoid = avoid_topics or []
            out += self._exam_tomorrow(data, today, db, avoid)
            if not user_stated_now:
                out += self._classes_over(data, today, local, db)
            out += self._dense_week(data, today, db)
            return out
        except Exception as exc:
            logger.warning("[schedule-moments] timetable unavailable (%s): nothing offered", exc.__class__.__name__)
            return []

    @staticmethod
    def _mentioned(db, key: str) -> bool:
        return bool(db.list_topic_mentions(topic_key=key, limit=1))

    def _exam_tomorrow(self, data: ScheduleData, today: date, db, avoid: list[str]) -> list[dict[str, Any]]:
        if any(EXAM_WORDS.search(a or "") for a in avoid):
            return []                                       # the user asked Violet to leave exams alone
        from ai.schedule.access import mentions_course
        day = engine.day(data, today + timedelta(days=1))
        out = []
        for item in day.items:
            if item.kind != "exam" or item.status != "exam" or item.exam_id is None:
                continue
            if any(mentions_course(a, item.course) or mentions_course(item.course, a) for a in avoid if a):
                continue
            key = f"{TOPIC_PREFIX}exam:{item.exam_id}"
            if self._mentioned(db, key):
                continue                                    # wished once already (any date): never again
            out.append({"id": key, "type": "exam_tomorrow", "exam": item.course,
                        "kind": {"final": "期末", "midterm": "期中", "quiz": "小测"}.get(item.exam_kind or "", "考试"),
                        "date": item.date.isoformat(), "start": item.start.strftime("%H:%M") if item.start else None,
                        "note": "对方明天有这场考试（按课表）：可以在今天自然地祝一次顺利，只一次；不追问准备得怎么样，"
                                "不问考场座位，之后也不追问结果。不提也完全正常。"})
        return out

    def _classes_over(self, data: ScheduleData, today: date, local: datetime, db) -> list[dict[str, Any]]:
        day = engine.day(data, today)
        if day.data_state != "active":
            return []
        classes = [i for i in day.items if i.kind == "class" and i.occupies and i.end is not None]
        if not classes:
            return []
        last_end = max(i.end for i in classes)
        if not (last_end <= local < last_end + AFTER_CLASS_FRESH):
            return []
        if any(i.occupies and i.start is not None and i.start > local for i in day.items):
            return []                                       # something is still planned later today
        key = f"{TOPIC_PREFIX}classes_over:{today.isoformat()}"
        if self._mentioned(db, key):
            return []
        return [{"id": key, "type": "classes_over_today", "classes_today": len(classes),
                 "last_end": last_end.astimezone(local.tzinfo).strftime("%H:%M"),
                 "note": "按课表，对方今天的课应该在这个时间之前上完了。只是计划：对方可能还没下课、可能在别处，"
                         "不要说“你下课了吧”“回宿舍了吧”这种肯定的话；想关心可以轻轻一句，用自己的话，不提也正常。"}]

    def _dense_week(self, data: ScheduleData, today: date, db) -> list[dict[str, Any]]:
        days = engine.day_range(data, today, 7)
        counts = [sum(1 for i in d.items if i.kind == "class" and i.occupies) for d in days if d.data_state == "active"]
        full = sum(1 for n in counts if n >= DENSE_DAY_CLASSES)
        if full < DENSE_WEEK_DAYS:
            return []
        year, week, _ = today.isocalendar()
        key = f"{TOPIC_PREFIX}dense_week:{year}-W{week:02d}"
        if self._mentioned(db, key):
            return []
        return [{"id": key, "type": "dense_week", "full_days": full, "classes_next_7_days": sum(counts),
                 "note": "接下来几天课排得比较满（按课表）。可以体贴一句，不用每次都提，更不要因此多发消息。"}]

    # -------------------------------------------------------------------------------------------- chat
    def status_line(self, moment: datetime) -> str | None:
        """VIOLET_SCHEDULE_CONTEXT: one line for the chat context (only while the plan has a class or an exam now)."""
        gate = self.gate(moment)
        if not gate.blocked:
            return None
        what = "考试" if gate.code == "exam" else "上课"
        return (f"- 按课表，对方这会儿可能在{what}（大约到 {gate.until.strftime('%H:%M')}）。{PLAN_ONLY}"
                "对方来消息就照常回复。")


def record_mentions(db, offered: list[dict[str, Any]], decision: dict[str, Any], sent_text: str,
                    proactive_event_id: int | None) -> list[str]:
    """After a proactive message really went out: the offered moments it used are done for good. Used = listed in
    the decision's mentioned_schedule_ids, or (exams) named in what was sent -- so a wish is never repeated even when
    the model forgot to list it."""
    from ai.schedule.access import mentions_course
    wanted = {str(x) for x in (decision.get("mentioned_schedule_ids") or []) if x}
    used = []
    for m in offered:
        hit = m["id"] in wanted or (m.get("type") == "exam_tomorrow" and mentions_course(sent_text or "", m["exam"]))
        if hit:
            db.add_topic_mention(m["id"], "violet_schedule", proactive_event_id=proactive_event_id)
            used.append(m["id"])
    if used:
        logger.info("[schedule-moments] used by proactive event %s: %s", proactive_event_id,
                    ",".join(u.split(":")[1] for u in used))
    return used
