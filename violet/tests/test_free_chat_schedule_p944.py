"""Phase 9.4.4: free_chat --schedule on -- a fictional timetable in the session's own isolated database, the gate and
the proactive outcomes visible with /schedule and in the saved events, a second launch that keeps the setting.

Mock model only (no --real: no key, no network), a scenario clock moved with /advance."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from tools import chat_dialogue as cd
from tools import free_chat as fc

START = "2026-09-25T07:30:00+08:00"                      # a Friday, 07:30: 高数 08:00-09:40, 体育 14:00-15:40


def launch(argv, lines):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIOLET_", "LLM_"))}
    env.update(PYTHONIOENCODING="utf-8", NO_PROXY="127.0.0.1,localhost")
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-m", "tools.free_chat", *argv], cwd=cd.PROJECT, env=env,
                          input="\n".join(lines) + "\n", capture_output=True, text=True, encoding="utf-8", timeout=900)
    return done.returncode, done.stdout + done.stderr


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    path = tmp_path_factory.mktemp("p944") / "sched"
    first = launch(["chat", "--session", str(path), "--schedule", "on", "--start", START],
                   ["/schedule", "我起床啦，一会儿去上课", "/advance 1h", "/schedule", "/schedule cancel",
                    "/schedule cancel 2026-09-25 10:00", "/schedule cancel 2026-09-25 14:00", "yes", "/schedule",
                    "/quit"])
    second = launch(["chat", "--session", str(path)], ["/schedule", "/advance 6h", "/quit"])
    refused = launch(["chat", "--session", str(path), "--schedule", "off"], ["/quit"])
    return path, first, second, refused


def test_runs_and_keeps_the_setting(session):
    path, first, second, refused = session
    assert first[0] == 0 and second[0] == 0, first[1][-3000:] + second[1][-3000:]
    meta = fc.read_meta(path)
    assert meta["mode"] == "mock" and meta["schedule"] == "on" and meta["schedule_context"] == "off"
    assert meta["schedule_fixture"]["fixture"] == "p944.v1" and len(meta["schedule_fixture"]["changes"]) == 2
    assert all(l["schedule"] == {"timetable": "on", "chat_context": "off"} for l in meta["launches"])
    assert "不能换成 off" in refused[1]


def test_schedule_view_shows_the_gate_and_the_plan(session):
    _, first, *_ = session
    out = first[1]
    assert "课表主动闸门 开" in out and "闸门：不拦（clear）" in out                     # 07:30, class from 07:55
    assert "闸门：拦（in_class，到 09:45）" in out                                         # 08:30 after /advance 1h
    assert "高等数学（虚构）" in out and "明天" in out and "高数期中（虚构）" in out
    assert "2026-09-25 可以停的课：08:00 高等数学（虚构）；14:00 体育（虚构）" in out          # bare: a list only
    assert "2026-09-25 10:00：没有在这个时间开始的课，没有停课" in out                    # wrong time: nothing
    assert "即将停课：2026-09-25（周五）14:00-15:40 体育（虚构）" in out                  # shown before the yes
    assert "已停课：2026-09-25 14:00 体育（虚构）" in out
    after = out[out.index("已停课"):]
    assert "体育（虚构）（cancelled）" in after                                          # the next view uses it


def test_skip_reasons_and_calls_are_recorded(session):
    path, *_ = session
    events = fc.read_jsonl(path / fc.EVENTS)
    idle = {}
    for e in events:
        for reason, n in (e.get("idle_checks") or {}).items():
            idle[reason] = idle.get(reason, 0) + n
    assert idle.get("schedule: class by the timetable until 09:45") == 1, idle      # 08:30, the second launch
    checks = [c for e in events for c in e.get("checks") or []]
    assert checks and all(c["due_reason"] == "due" for c in checks)                # later: ordinary checks only
    assert all(c["proactive_sent"] == [] for c in checks)                           # mock decides not to write


def test_every_round_keeps_the_timetable_state_and_the_tool_trace(session):
    """Real-model acceptance prep: each saved round carries the gate / today's plan / offerable and used moments, a
    user round the tool catalog and Intent's plan (violet-schedule listed), and a check its gate and offered moments."""
    path, *_ = session
    events = fc.read_jsonl(path / fc.EVENTS)
    assert events and all(e["schedule_state"]["gate"]["feature_on"] for e in events)
    first = events[0]
    assert first["kind"] == "user" and first["schedule_state"]["date"] == "2026-09-25"
    assert [i["course"] for i in first["schedule_state"]["today"]] == ["高等数学（虚构）", "体育（虚构）"]
    catalog = next(t for t in first["tool_trace"] if t["kind"] == "catalog")
    assert "violet-schedule__get_day_schedule" in catalog["tools"]
    assert any(t["kind"] == "plan" for t in first["tool_trace"])
    blocked = next(e for e in events if e["schedule_state"]["gate"]["blocked"])
    assert blocked["schedule_state"]["gate"]["until"] == "2026-09-25T09:45+08:00"
    checks = [c for e in events for c in e.get("checks") or []]
    assert checks and all({"gate", "offered_moments", "named_moments"} <= set(c["schedule"]) for c in checks)
    assert any(m.startswith("schedule:exam:") for c in checks for m in c["schedule"]["offered_moments"])  # Sat exam
    shown = fc.show_event(blocked)
    assert "课表（2026-09-25" in shown and "拦到 09:45" in shown


def test_the_session_database_is_isolated_and_holds_no_real_data(session):
    path, *_ = session
    db = path / "violet" / "violet-test.db"
    assert db.is_file()
    con = sqlite3.connect(db)
    try:
        names = {r[0] for r in con.execute("SELECT name FROM schedule_courses")}
        assert names == {"高等数学（虚构）", "大学英语（虚构）", "物理实验（虚构）", "体育（虚构）"}
        metadata = " ".join(r[0] or "" for r in con.execute("SELECT metadata FROM proactive_events"))
        for secret in ("虚构老师甲", "虚构老师乙", "虚构楼101", "虚构考场", "虚构座位17", "虚构的私人备注"):
            assert secret not in metadata
    finally:
        con.close()
