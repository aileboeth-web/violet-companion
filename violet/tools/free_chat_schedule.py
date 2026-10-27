"""Phase 9.4.4: a fictional timetable for free_chat sessions (--schedule on) and the /schedule view.

Everything here works on the session's own isolated database (tools.harness puts it in the session directory); it
never opens Violet's real database or a real timetable. The fixture is built relative to the session's start time, so a
session started on any day has classes, an exam tomorrow, a moved class and a cancelled one nearby. Names, teachers,
rooms and the seat are fictional (they let a tester check that none of them reaches the proactive decision).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

FIXTURE_VERSION = "p944.v1"
PERIODS = [(1, "08:00", "08:45"), (2, "08:55", "09:40"), (3, "10:00", "10:45"), (4, "10:55", "11:40"),
           (5, "14:00", "14:45"), (6, "14:55", "15:40")]
WEEKDAY = ("", "周一", "周二", "周三", "周四", "周五", "周六", "周日")


def fill_fixture(db, start: datetime) -> dict[str, Any]:
    """Create the fictional term around `start` (local date): returns ids and what was changed."""
    from ai.schedule import engine
    from ai.schedule.store import ScheduleStore
    store = ScheduleStore(db)
    today = start.date()
    monday = today - timedelta(days=today.weekday()) - timedelta(days=7)          # the session starts in week 2
    from ai.settings import env_str                                          # the zone Violet's clock runs in
    zone = getattr(start.tzinfo, "key", None) or env_str("VIOLET_TIMEZONE", "Asia/Shanghai") or "Asia/Shanghai"
    term = store.create_term(name="测试学期（虚构）", start_date=monday.isoformat(),
                             end_date=(monday + timedelta(days=7 * 20 - 3)).isoformat(), timezone=zone,
                             actor="free_chat")
    store.set_periods(term, PERIODS, actor="free_chat")
    courses = {
        "math": store.add_course(term, name="高等数学（虚构）", teacher="虚构老师甲", notes="虚构的私人备注", actor="free_chat"),
        "eng": store.add_course(term, name="大学英语（虚构）", teacher="虚构老师乙", actor="free_chat"),
        "lab": store.add_course(term, name="物理实验（虚构）", kind="lab", actor="free_chat"),
        "pe": store.add_course(term, name="体育（虚构）", kind="pe", actor="free_chat"),
    }
    for weekday in (1, 3, 5):
        store.add_slot(courses["math"], weekday=weekday, weeks="1-20", period_start=1, period_end=2,
                       location="虚构楼101", actor="free_chat")
    for weekday in (2, 4):
        store.add_slot(courses["eng"], weekday=weekday, weeks="1-20", period_start=3, period_end=4,
                       location="虚构楼205", actor="free_chat")
    store.add_slot(courses["lab"], weekday=4, weeks="2-20双", start="14:00", end="15:40", location="虚构实验楼",
                   actor="free_chat")
    store.add_slot(courses["pe"], weekday=5, weeks="1-20", period_start=5, period_end=6, location="虚构操场",
                   actor="free_chat")
    tomorrow = today + timedelta(days=1)
    exam = store.add_exam(term, name="高数期中（虚构）", exam_date=tomorrow.isoformat(), kind="midterm",
                          course_id=courses["math"], start="10:00", end="11:40", location="虚构考场",
                          seat="虚构座位17", actor="free_chat")
    changes = []
    data = store.load()
    for offset, kind in ((2, "time"), (3, "cancel")):                     # a moved and a cancelled class nearby
        for extra in range(0, 7):
            day = today + timedelta(days=offset + extra)
            occ = next((i for i in engine.day(data, day).items if i.kind == "class" and i.status == "normal"), None)
            if occ is None or any(c["date"] == day.isoformat() for c in changes):
                continue
            fields = {"new_start": "16:00", "new_end": "17:40"} if kind == "time" else {}
            store.add_exception(occ.course_id, kind=kind, on_date=day.isoformat(), slot_id=occ.slot_id,
                                reason="虚构的调课原因", actor="free_chat", **fields)
            changes.append({"kind": kind, "date": day.isoformat(), "course": occ.course})
            data = store.load()
            break
    return {"fixture": FIXTURE_VERSION, "term_id": term, "exam_id": exam, "changes": changes}


def schedule_view(db, now: datetime, recent: int = 6) -> list[str]:
    """What /schedule prints: the gate now, today's plan, the moments a check would offer, the latest proactive
    outcomes with their reasons."""
    from ai.schedule import awareness, engine
    from ai.schedule.store import ScheduleStore
    store = ScheduleStore(db)
    aware = awareness.Awareness(store)
    lines = [f"模拟时间 {now.strftime('%m-%d %H:%M')}（{WEEKDAY[now.isoweekday()]}）；"
             f"课表主动闸门 {'开' if awareness.enabled() else '关'}，聊天课表提示 {'开' if awareness.context_enabled() else '关'}"]
    gate = aware.gate(now)
    lines.append("闸门：" + (f"拦（{gate.code}，到 {gate.until.strftime('%H:%M')}）" if gate.blocked else f"不拦（{gate.code}）"))
    data = store.load()
    today, _ = engine.local_date(data, now)
    for label, day in (("今天", today), ("明天", today + timedelta(days=1))):
        result = engine.day(data, day)
        items = [f"{i.start.strftime('%H:%M') if i.start else '?'}-{i.end.strftime('%H:%M') if i.end else '?'} "
                 f"{i.course}（{i.status}）" for i in result.items]
        lines.append(f"{label} {day.isoformat()}：" + ("；".join(items) if items else f"没有安排（{result.data_state}）"))
    offered = aware.moments(now, db)
    lines.append("这次检查可以提供的课表背景：" + ("；".join(f"{m['type']}" + (f" {m.get('exam')}" if m.get("exam") else "")
                                                for m in offered) or "无"))
    used = db.fetch_all("SELECT topic_key, created_at FROM topic_mentions WHERE topic_key LIKE 'schedule:%' ORDER BY id")
    if used:
        lines.append("已经用过（不会再提供）：" + "；".join(f"{u['topic_key']} @ {u['created_at'][5:16]}" for u in used))
    rows = db.fetch_all("SELECT id, status, reason, created_at, decision FROM proactive_events "
                        "WHERE event_type != 'commitment_reminder' ORDER BY id DESC LIMIT ?", (int(recent),))
    for r in rows[::-1]:
        try:
            ids = (json.loads(r["decision"] or "{}") or {}).get("mentioned_schedule_ids") or []
        except (TypeError, ValueError):
            ids = []
        lines.append(f"主动 #{r['id']} {r['created_at'][5:16]} {r['status']}：{(r['reason'] or '')[:80]}"
                     + (f"（用了 {', '.join(map(str, ids))}）" if ids else ""))
    return lines


def gate_summary(db, now: datetime) -> dict[str, Any]:
    """The gate at a moment, as the scheduler's code computes it."""
    from ai.schedule import awareness
    from ai.schedule.store import ScheduleStore
    gate = awareness.Awareness(ScheduleStore(db)).gate(now)
    return {"at": now.isoformat(timespec="minutes"), "blocked": gate.blocked, "code": gate.code,
            "until": gate.until.isoformat(timespec="minutes") if gate.until else None,
            "feature_on": awareness.enabled()}


def schedule_state(db, now: datetime) -> dict[str, Any]:
    """Saved with every free_chat round (phase 9.4.4 evidence): the gate, today's computed plan (course, times,
    status), what a check would offer now and which moments are already used. Fictional data only."""
    from ai.schedule import awareness, engine
    from ai.schedule.store import ScheduleStore
    store = ScheduleStore(db)
    data = store.load()
    today, _ = engine.local_date(data, now)
    day = engine.day(data, today)
    return {"gate": gate_summary(db, now), "date": today.isoformat(), "data_state": day.data_state,
            "week": day.week, "day_kind": day.day_kind,
            "today": [{"course": i.course, "kind": i.kind, "status": i.status,
                       "start": i.start.strftime("%H:%M") if i.start else None,
                       "end": i.end.strftime("%H:%M") if i.end else None} for i in day.items],
            "offerable_moments": [m["id"] for m in awareness.Awareness(store).moments(now, db)],
            "used_moments": [r["topic_key"] for r in db.fetch_all(
                "SELECT topic_key FROM topic_mentions WHERE topic_key LIKE 'schedule:%' ORDER BY id")]}


def check_moments(db, events_mark: int) -> dict[str, Any]:
    """For the proactive events one check created: the moments the decision was offered and the ids it named."""
    offered, named = [], []
    for row in db.fetch_all("SELECT metadata, decision FROM proactive_events WHERE id > ? ORDER BY id", (events_mark,)):
        try:
            meta = json.loads(row["metadata"] or "{}") or {}
            decision = json.loads(row["decision"] or "{}") or {}
        except (TypeError, ValueError):
            continue
        offered += [m.get("id") for m in (meta.get("conversation_state") or {}).get("allowed_schedule_moments") or []]
        named += [str(x) for x in decision.get("mentioned_schedule_ids") or []]
    return {"offered_moments": offered, "named_moments": named}


CANCEL_USAGE = "用法：/schedule cancel 2026-10-30 08:00（日期也可以写 today / tomorrow）；先显示要停的课，输入 yes 才停"


def _target_day(db, now: datetime, text: str):
    from ai.schedule import engine
    from ai.schedule.model import parse_date
    from ai.schedule.store import ScheduleStore
    today, _ = engine.local_date(ScheduleStore(db).load(), now)
    word = text.strip().lower()
    if word in ("today", "今天"):
        return today
    if word in ("tomorrow", "明天"):
        return today + timedelta(days=1)
    return parse_date(text)                                  # ValueError when it is not YYYY-MM-DD


def classes_on(db, day) -> list[Any]:
    """The classes the plan has on that day that can still be cancelled (exams and extra classes aside)."""
    from ai.schedule import engine
    from ai.schedule.store import ScheduleStore
    return [i for i in engine.day(ScheduleStore(db).load(), day).items
            if i.kind == "class" and i.slot_id is not None and i.status in ("normal", "changed", "moved_in")]


def cancel_choices(db, now: datetime, day_text: str = "today") -> str:
    """/schedule cancel without a target: what could be cancelled -- nothing is changed."""
    try:
        day = _target_day(db, now, day_text)
    except ValueError:
        return CANCEL_USAGE
    items = classes_on(db, day)
    listed = "；".join(f"{i.start.strftime('%H:%M') if i.start else '?'} {i.course}" for i in items) or "没有可以停的课"
    return f"{day.isoformat()} 可以停的课：{listed}\n{CANCEL_USAGE}"


def find_class(db, now: datetime, target: str) -> tuple[Any | None, str]:
    """"2026-10-30 08:00" / "tomorrow 08:00" -> (the ONE class that starts then, a line saying exactly what will be
    cancelled) or (None, why not + that day's classes). Never picks another class on its own (phase 9.4 real run:
    the old "next class after now" rule cancelled 14:00 体育 when 08:00 数学 was meant, because 08:00 had begun)."""
    parts = target.split()
    if len(parts) != 2:
        return None, CANCEL_USAGE
    try:
        day = _target_day(db, now, parts[0])
    except ValueError:
        return None, CANCEL_USAGE
    hhmm = parts[1].strip()
    items = classes_on(db, day)
    hits = [i for i in items if i.start is not None and i.start.strftime("%H:%M") == hhmm]
    if len(hits) != 1:
        listed = "；".join(f"{i.start.strftime('%H:%M') if i.start else '?'} {i.course}" for i in items) or "没有课"
        why = "没有在这个时间开始的课" if not hits else "这个时间开始的课不止一门"
        return None, f"{day.isoformat()} {hhmm}：{why}，没有停课。那天可以停的课：{listed}"
    occ = hits[0]
    return occ, (f"即将停课：{day.isoformat()}（{WEEKDAY[day.isoweekday()]}）{occ.start.strftime('%H:%M')}-"
                 f"{occ.end.strftime('%H:%M')} {occ.course}")


def cancel_occurrence(db, occ) -> str:
    from ai.schedule.store import ScheduleStore
    nominal = occ.nominal_date or occ.date
    ScheduleStore(db).add_exception(occ.course_id, kind="cancel", on_date=nominal.isoformat(), slot_id=occ.slot_id,
                                    actor="free_chat")
    return (f"已停课：{nominal.isoformat()} {occ.start.strftime('%H:%M')} {occ.course}"
            "（只改这个测试会话的虚构课表；下一次检查就按新课表算）")

