"""Phase 9.4.2: the four read-only timetable lookups behind the violet-schedule MCP server (plain Python, testable
without MCP). Every result is a JSON-able dict with ok / error_code; nothing here writes.

The current moment is Violet's own clock (ai.settings.now_local -- the scenario clock in tests and free_chat) read in
the zone of the term it falls in; the user never has to say what time it is. Results say plainly when there is no
timetable, the term has not started / is over, the day is off, or there is simply no class -- so Violet never answers
from an old term or fills a gap. Logs carry the tool, the data state and counts only."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable

from ai.schedule import engine
from ai.schedule.access import (WEEKDAY_NAMES, current_turn_message, granted_details, is_abbreviation, project,
                                resolve_date)
from ai.schedule.engine import PLAN_NOTE
from ai.schedule.model import ScheduleData, zone_of

logger = logging.getLogger("violet.schedule")

MAX_RANGE_DAYS = 14
MAX_QUERY_CHARS = 30
MAX_MATCHES = 5
FIND_LOOKAHEAD_DAYS = 28
UPCOMING_PER_COURSE = 3
STATUS_TEXT = {"normal": "照常", "changed": "有变动", "moved_in": "调到这天上", "moved_out": "已调走，这天不上",
               "cancelled": "停课", "extra": "加课", "exam": "考试", "exam_cancelled": "考试已取消",
               "invalid_time": "时间没设置好"}
DAY_KIND_TEXT = {"holiday": "放假", "no_class": "停课日", "makeup": "调休补课日"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": code, "message": message}


class ScheduleTools:
    def __init__(self, store, now: Callable[[], datetime] | None = None, default_zone: Callable[[], str] | None = None):
        self.store = store
        self._now = now or _eve_now
        self._zone = default_zone or _default_zone

    # ------------------------------------------------------------------------------------------- shared
    def _moment(self) -> datetime:
        moment = self._now()
        if moment.tzinfo is None:
            raise ValueError("Violet's clock gave a naive datetime")
        return moment

    def _details(self, requested) -> tuple[list[str], dict[str, Any]]:
        given, refused = granted_details(requested, current_turn_message())
        info: dict[str, Any] = {"details_included": given}
        if refused:
            info["details_note"] = ("地点 / 老师 / 座位号只在对方这句话里明确问到时才提供："
                                    + "、".join(refused) + " 这次没有给。没给的不要猜。")
        return given, info

    def _state_note(self, data: ScheduleData, state: str, on: date, day_kind: str | None = None,
                    day_name: str | None = None, has_classes: bool = False) -> str:
        terms = sorted((t.term for t in data.terms if t.term.status == "active"), key=lambda t: t.start_date)
        if state == "no_data":
            return "还没有录入任何课表：不知道有没有课，不要猜，也不要说成没课。"
        if state == "not_started":
            first = terms[0]
            return f"学期还没开始（{first.name} {first.start_date.isoformat()} 开学），那时之前课表上没有课。"
        if state == "between_terms":
            later = next((t for t in terms if t.start_date > on), None)
            return ("这天不在任何学期里（上个学期已经结束" + (f"，下个学期 {later.start_date.isoformat()} 开始" if later else "")
                    + "），课表上没有这天的课。")
        if state == "ended":
            last = terms[-1]
            return (f"最近录入的学期（{last.name}）已经在 {last.end_date.isoformat()} 结束了；新学期的课表还没有录入。"
                    "不要用旧课表回答现在或以后有没有课。")
        if day_kind in DAY_KIND_TEXT and day_kind != "makeup":
            return f"这天{DAY_KIND_TEXT[day_kind]}（{day_name}），没有常规课。"
        if not has_classes:
            return "这天在学期里，课表上没有课。"
        return "这天在学期里，下面是课表上的安排。"

    def _item(self, raw: dict[str, Any], fields: list[str], names: dict[str, str]) -> dict[str, Any]:
        item = project(raw, fields)
        out = {"course": item["course"], "kind": item["kind"], "status": item["status"],
               "status_text": STATUS_TEXT.get(item["status"], item["status"]), "occupies": item["occupies"],
               "date": item["date"], "weekday": WEEKDAY_NAMES[date.fromisoformat(item["date"]).isoweekday()],
               "start": item["start"][11:16] if item["start"] else None,
               "end": item["end"][11:16] if item["end"] else None,
               "start_at": item["start"], "end_at": item["end"]}
        for key in ("change_note", "moved_to", "moved_from", "exam_kind"):
            if item.get(key):
                out[key] = item[key]
        if item.get("conflict_with"):
            out["conflict_with"] = [names.get(k, k) for k in item["conflict_with"]]
        if item.get("notes"):
            out["notes"] = item["notes"]
        for name in ("location", "teacher", "seat"):
            if name in item:
                out[name] = item[name]
        return out

    def _day_view(self, data: ScheduleData, result: engine.DayResult, fields: list[str]) -> dict[str, Any]:
        raw = result.to_dict(include_personal=True)
        names = {i["key"]: i["course"] for i in raw["items"]}
        items = [self._item(i, fields, names) for i in raw["items"]]
        view = {"date": raw["date"], "weekday": WEEKDAY_NAMES[result.weekday], "data_state": result.data_state,
                "state_note": self._state_note(data, result.data_state, result.date, result.day_kind, result.day_name,
                                               result.has_classes),
                "term": raw["term"], "day_kind": result.day_kind,
                "classes": sum(1 for i in items if i["kind"] == "class" and i["occupies"]),
                "exams": sum(1 for i in items if i["kind"] == "exam" and i["occupies"]), "items": items}
        if result.day_name:
            view["day_name"] = result.day_name
        if result.follows_date:
            view["follows_date"] = result.follows_date.isoformat()
        if result.notes:
            view["notes"] = list(result.notes)
        return view

    def _log(self, tool: str, state: str, count: int, fields: list[str]) -> None:
        logger.info("[schedule] %s: state=%s items=%d details=%s", tool, state, count, ",".join(fields) or "-")

    def _head(self, data: ScheduleData, moment: datetime) -> tuple[date, str, dict[str, Any]]:
        today, zone = engine.local_date(data, moment, self._zone())
        local = moment.astimezone(zone_of(zone))
        return today, zone, {"now": local.isoformat(timespec="minutes"), "today": today.isoformat(),
                             "timezone": zone, "plan_note": PLAN_NOTE}

    # ------------------------------------------------------------------------------------------- tools
    def day(self, date_text: Any = "today", details=None) -> dict[str, Any]:
        moment = self._moment()
        data = self.store.load()
        today, zone, head = self._head(data, moment)
        try:
            on = resolve_date(date_text, today)
        except ValueError as exc:
            return _error("invalid_arguments", str(exc))
        fields, info = self._details(details)
        view = self._day_view(data, engine.day(data, on), fields)
        self._log("get_day_schedule", view["data_state"], len(view["items"]), fields)
        return {"ok": True, **head, **view, **info, "last_updated": data.last_updated}

    def range(self, start_text: Any = "today", days: Any = 7, details=None) -> dict[str, Any]:
        try:
            count = int(days)
        except (TypeError, ValueError):
            return _error("invalid_arguments", "days 要是 1–14 的整数")
        if not 1 <= count <= MAX_RANGE_DAYS:
            return _error("invalid_arguments", f"一次最多查 {MAX_RANGE_DAYS} 天（days 1–{MAX_RANGE_DAYS}）")
        moment = self._moment()
        data = self.store.load()
        today, zone, head = self._head(data, moment)
        try:
            start = resolve_date(start_text, today)
        except ValueError as exc:
            return _error("invalid_arguments", str(exc))
        fields, info = self._details(details)
        views = [self._day_view(data, d, fields) for d in engine.day_range(data, start, count)]
        summary = []
        for v in views:
            busy = sum(_minutes(i) for i in v["items"] if i["occupies"])
            summary.append({"date": v["date"], "weekday": v["weekday"], "data_state": v["data_state"],
                            "day_kind": v["day_kind"], "classes": v["classes"], "exams": v["exams"],
                            "busy_minutes": busy,
                            "first_start": next((i["start"] for i in v["items"] if i["occupies"] and i["start"]), None),
                            "last_end": max((i["end"] for i in v["items"] if i["occupies"] and i["end"]), default=None)})
        active = [s for s in summary if s["data_state"] == "active"]
        lightest = sorted(active, key=lambda s: (s["busy_minutes"], s["date"]))[:3]
        states = sorted({v["data_state"] for v in views})
        self._log("get_schedule_range", "/".join(states), sum(len(v["items"]) for v in views), fields)
        return {"ok": True, **head, "start_date": start.isoformat(), "days": count, "data_states": states,
                "free_days": [s["date"] for s in active if s["classes"] == 0 and s["exams"] == 0],
                "lightest_days": [s["date"] for s in lightest], "summary": summary, "schedule": views, **info,
                "last_updated": data.last_updated,
                **({"state_note": self._state_note(data, views[0]["data_state"], start)}
                   if all(v["data_state"] != "active" for v in views) else {})}

    def status(self, details=None) -> dict[str, Any]:
        moment = self._moment()
        data = self.store.load()
        today, zone, head = self._head(data, moment)
        fields, info = self._details(details)
        raw = engine.status_at(data, moment, default_zone=self._zone()).to_dict(include_personal=True)
        current = []
        for item in raw["current"]:
            view = self._item(item, fields, {})
            view["ends_in_minutes"] = max(0, int((datetime.fromisoformat(item["end"]) - moment).total_seconds() // 60))
            current.append(view)
        nxt = None
        if raw["next"]:
            nxt = self._item(raw["next"], fields, {})
            nxt["starts_in_minutes"] = int((datetime.fromisoformat(raw["next"]["start"]) - moment).total_seconds() // 60)
        state = raw["data_state"]
        note = (self._state_note(data, state, today) if state != "active" else
                ("按课表，现在是上课时间。" if current else "按课表，现在没有在上的课。"))
        self._log("get_current_status", state, len(current) + (1 if nxt else 0), fields)
        return {"ok": True, **head, "data_state": state, "state_note": note, "term": raw["term"],
                "in_class_by_plan": bool(current), "current": current, "next": nxt,
                "remaining_today": raw["remaining_today"], "basis": "timetable_plan",
                "not_known": "对方此刻真实在哪里、在做什么、是不是真的在上课，课表都不知道；对方自己说的情况优先。",
                **info, "last_updated": data.last_updated}

    def find(self, query: Any, details=None) -> dict[str, Any]:
        text = str(query or "").strip()
        if not text or len(text) > MAX_QUERY_CHARS or _CONTROL.search(text):
            return _error("invalid_arguments", f"query 要是 1–{MAX_QUERY_CHARS} 个字的课名或老师名")
        moment = self._moment()
        data = self.store.load()
        today, zone, head = self._head(data, moment)
        fields, info = self._details(details)
        td, state = engine.term_for(data, today)
        if td is None:
            self._log("find_course", state, 0, fields)
            return {"ok": True, **head, "query": text, "data_state": state, "matches": [],
                    "state_note": self._state_note(data, state, today), **info}
        matches = []
        for course in sorted(td.courses.values(), key=lambda c: c.id):
            if course.status != "active":
                continue
            reason = _match(text, course)
            if reason:
                matches.append((reason, course))
        order = {"name": 0, "code": 1, "abbreviation": 2, "teacher": 3}
        matches.sort(key=lambda m: (order[m[0]], m[1].id))
        found = []
        upcoming = [i for d in engine.day_range(data, today, FIND_LOOKAHEAD_DAYS)
                    for i in d.to_dict(include_personal=True)["items"]
                    if not (i["end"] and datetime.fromisoformat(i["end"]) <= moment)]      # already over: not "next"
        names = {i["key"]: i["course"] for i in upcoming}
        for reason, course in matches[:MAX_MATCHES]:
            items = [self._item(i, fields, names) for i in upcoming
                     if i["kind"] == "class" and i["course_id"] == course.id and i["status"] != "invalid_time"]
            exams = [self._item(i, fields, names) for i in upcoming if i["kind"] == "exam" and i["course_id"] == course.id]
            entry = {"course": course.name, "type": course.kind, "matched_by": reason,
                     "upcoming": items[:UPCOMING_PER_COURSE], "upcoming_exams": exams[:UPCOMING_PER_COURSE],
                     "lookahead_days": FIND_LOOKAHEAD_DAYS}
            if "teacher" in fields and course.teacher:
                entry["teacher"] = course.teacher
            found.append(entry)
        self._log("find_course", state, len(found), fields)
        out = {"ok": True, **head, "query": text, "data_state": state, "term": {"name": td.term.name,
               "week": td.term.week_of(today)}, "matches": found, "more_matches": len(matches) > MAX_MATCHES, **info}
        if not found:
            out["note"] = "课表里没有和这个名字对得上的课；不要编造课程，可以问对方是不是别的叫法。"
        return out


def _match(query: str, course) -> str | None:
    q = query.lower()
    name = course.name.lower()
    if q in name:
        return "name"
    if course.code and q in course.code.lower():
        return "code"
    # an abbreviation in order with the same first character ("高数" -> "高等数学", "毛概" -> "毛泽东思想和…概论")
    if is_abbreviation(q, name):
        return "abbreviation"
    if len(q) >= 2 and course.teacher and q in course.teacher.lower():
        return "teacher"
    return None


def _minutes(item: dict[str, Any]) -> int:
    if not item.get("start_at") or not item.get("end_at"):
        return 0
    return int((datetime.fromisoformat(item["end_at"]) - datetime.fromisoformat(item["start_at"])).total_seconds() // 60)


def _eve_now() -> datetime:
    from ai import settings
    return settings.now_local()


def _default_zone() -> str:
    from ai.settings import env_str
    return env_str("VIOLET_TIMEZONE", "Asia/Shanghai") or "Asia/Shanghai"
