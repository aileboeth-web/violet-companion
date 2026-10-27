"""Phase 7 B.4: time passes on the scenario clock (stored history is never rewritten), plan revisions,
the round-15 scenario time, and structured location checks for rounds 11 / 13. Offline only."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.test_replay_b3 import PLAN, make_report, write_report

PROJECT = Path(__file__).resolve().parent.parent
BASE_PLAN = PROJECT / "tests" / "samples" / "acceptance_dialogue_30.json"
BASE_SHA = "bbf5e0a4b1bc6c742e138d5d7c6fb44ec7d7d1d5462c9b6e67fdaaae19e15ec1"


@pytest.fixture()
def clean_env():
    saved = dict(os.environ)
    yield os.environ
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture()
def session(clean_env, violetdb, tmp_path):
    from tools.violet_test_chat import build_parser, open_session
    from tools.harness import ScenarioClock
    s = open_session(build_parser().parse_args(["--workdir", str(tmp_path / "violet-testchat-time"), "--quiet-hours", "off"]),
                     out=lambda x: None, db=violetdb)
    s.use_clock(ScenarioClock(datetime.fromisoformat("2026-10-13T09:46:00+08:00")))
    yield s
    s.close()


TIME_COLUMNS = {
    "messages": ["created_at"], "user_profile_facts": ["stated_at", "confirmed_at", "created_at"],
    "event_memories": ["created_at", "event_time", "valid_until", "progress_updated_at"],
    "life_events": ["occurred_at", "created_at", "share_count"],
    "user_situations": ["observed_at", "expires_at", "created_at"], "memory_jobs": ["created_at"],
    "inbound_messages": ["received_at"],
}


def snapshot(db) -> dict:
    return {t: db.fetch_all(f"SELECT id, {', '.join(c)} FROM {t} ORDER BY id") for t, c in TIME_COLUMNS.items()}


def _say(session, text, n, steps=None):
    session.memory_script = steps
    try:
        return session.say(text, n)
    finally:
        session.memory_script = None


# ---------------------------------------------------------------------------
# The scenario clock
# ---------------------------------------------------------------------------

def test_thirty_hours_pass_on_the_clock_and_history_stays_as_stored(session):
    from tools.dialogue_eval import preload
    preload(session, PLAN)
    _say(session, "下周三有个天文观测课程的答辩，有点紧张", 4,
         [{"tool": "save_memory", "args": {"summary": "用户将于2026-10-21参加天文观测课程答辩", "kind": "event",
                                           "progress": "planned", "event_date": "2026-10-21"}}])
    _say(session, "我在开会，晚点聊", 9,
         [{"tool": "set_situation", "args": {"kind": "availability", "value": "在开会", "evidence": "在开会", "until": "2h"}}])
    before, start = snapshot(session.db), session.clock.now()
    info = session.advance(30)
    assert abs((session.clock.now() - start).total_seconds() - 30 * 3600) < 5
    assert datetime.fromisoformat(session.db.now()).date().isoformat() == "2026-10-14"
    after = snapshot(session.db)
    for table in ("messages", "user_profile_facts", "life_events", "memory_jobs", "inbound_messages"):
        assert after[table] == before[table], table                     # not a single timestamp rewritten
    assert [r["event_time"] for r in after["event_memories"]] == [r["event_time"] for r in before["event_memories"]]
    assert after["event_memories"][0]["event_time"].startswith("2026-10-21")    # the absolute date is stable
    assert info["hours"] == pytest.approx(30, abs=0.01)
    assert [s["value"] for s in info["situations_expired"]] == ["在开会"]


def test_crossing_midnight_and_time_zones(session, monkeypatch):
    from tools.harness import ScenarioClock
    clock = ScenarioClock(datetime.fromisoformat("2026-10-13T15:30:00+00:00"))      # = 23:30 in Shanghai
    assert clock.now().isoformat().startswith("2026-10-13T23:30") and clock.now().utcoffset() == timedelta(hours=8)
    clock.advance(1)
    assert clock.now().isoformat().startswith("2026-10-14T00:30")
    with pytest.raises(ValueError):
        clock.advance(-1)
    with pytest.raises(ValueError):
        clock.advance_to(datetime.fromisoformat("2026-10-01T00:00:00+08:00"))
    with pytest.raises(ValueError):
        clock.advance_to(datetime(2026, 12, 1, 0, 0))                                  # naive: refused
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/Los_Angeles")
    except Exception:
        pytest.skip("no IANA zone data")
    monkeypatch.setenv("VIOLET_TIMEZONE", "America/Los_Angeles")
    from datetime import timezone
    dst = ScenarioClock(datetime.fromisoformat("2026-11-01T00:30:00-07:00"))         # daylight saving ends at 02:00
    start = dst.now()
    dst.advance(2)
    real = dst.now().astimezone(timezone.utc) - start.astimezone(timezone.utc)
    assert real.total_seconds() == pytest.approx(7200, abs=5)                          # two real hours, not three
    assert dst.now().utcoffset() == timedelta(hours=-8) and dst.now().hour == 1         # wall clock 01:30 PST
    monkeypatch.setenv("VIOLET_TIMEZONE", "Asia/Shanghai")
    assert session.clock.now().utcoffset() == timedelta(hours=8)


def test_every_session_runs_on_a_scenario_clock(clean_env, violetdb, tmp_path):
    """Sessions without use_clock (the chat tool, /advance) also move Violet's own time."""
    from tools.violet_test_chat import build_parser, open_session
    plain = open_session(build_parser().parse_args(["--workdir", str(tmp_path / "violet-testchat-plain")]),
                         out=lambda x: None, db=violetdb)
    try:
        before = datetime.fromisoformat(plain.db.now())
        plain.advance(5)
        assert (datetime.fromisoformat(plain.db.now()) - before).total_seconds() == pytest.approx(5 * 3600, abs=10)
    finally:
        plain.close()


def test_events_change_state_on_the_right_side_of_their_date(session):
    _say(session, "下周三有个天文观测课程的答辩，有点紧张", 4,
         [{"tool": "save_memory", "args": {"summary": "用户将于2026-10-21参加天文观测课程答辩", "kind": "event",
                                           "progress": "planned", "event_date": "2026-10-21"}}])
    event = session.db.fetch_one("SELECT * FROM event_memories")
    # phase 9.1.1: a day without a clock is stored as the day (it used to be 23:59 of it); it still passes at the
    # end of that day
    from ai.memory_model import event_deadline
    assert event["event_time"] == "2026-10-21"
    event_time = event_deadline(event["event_time"])
    session.advance_to(event_time - timedelta(hours=1))
    session.db.refresh_event_states()
    assert session.db.fetch_one("SELECT progress FROM event_memories")["progress"] == "planned"
    info = session.advance_to(event_time + timedelta(hours=1))
    assert info["events_changed"] == [{"id": event["id"], "before": ["planned", "active"], "after": ["unknown", "active"]}]
    assert session.db.fetch_one("SELECT event_time FROM event_memories")["event_time"] == event["event_time"]
    session.advance_to(datetime.fromisoformat(event["valid_until"]) + timedelta(minutes=1))
    session.db.refresh_event_states()
    assert session.db.fetch_one("SELECT status FROM event_memories")["status"] == "expired"


def test_the_defense_is_still_found_and_updated_after_the_jump(session, violetdb):
    from ai.agents import IntentAgent, MemoryAgent
    from ai.agents.base_agent import _CALL_CONTEXT
    _say(session, "下周三有个天文观测课程的答辩，有点紧张", 4,
         [{"tool": "save_memory", "args": {"summary": "用户将于2026-10-21参加天文观测课程答辩", "kind": "event",
                                           "progress": "planned", "event_date": "2026-10-21"}}])
    session.advance_to(datetime.fromisoformat("2026-10-22T10:00:00+08:00"))
    found = IntentAgent()._tool_query_memory({"query": "答辩 天文观测"})["memories"]
    assert [m["content"] for m in found] == ["用户将于2026-10-21参加天文观测课程答辩"]
    memory = MemoryAgent()
    token = _CALL_CONTEXT.set({"user_message": "答辩结束了，挺顺利的", "turn_id": "t15"})
    try:
        assert "未结束的事件" in memory._format_open_events() and "2026-10-21" in memory._format_open_events()
        result = json.loads(memory._tool_update_event_progress({"memory_id": found[0]["id"], "progress": "done",
                                                                "summary": "用户的天文观测课程答辩已经结束，很顺利"}))
    finally:
        _CALL_CONTEXT.reset(token)
    assert result["ok"] is True and result["progress"] == "done"


def test_busy_expires_with_time_and_an_early_end_of_the_same_kind_clears_it(session):
    from tools.dialogue_eval import _busy_cleared
    _say(session, "我在开会，晚点聊", 9,
         [{"tool": "set_situation", "args": {"kind": "activity", "value": "正在开会", "evidence": "在开会", "until": "2h"}}])
    early = session.advance(1.9)
    assert session.scheduler._busy_situation() is not None and early["situations_expired"] == []
    info = session.advance(0.2)
    assert session.scheduler._busy_situation() is None and [s["value"] for s in info["situations_expired"]] == ["正在开会"]
    # ended early, reported as the same kind of situation: the busy one is superseded at once
    _say(session, "我在开会，晚点聊", 11,
         [{"tool": "set_situation", "args": {"kind": "activity", "value": "正在开会", "evidence": "在开会", "until": "2h"}}])
    session.advance(0.5)
    _say(session, "开完了，好累", 12,
         [{"tool": "set_situation", "args": {"kind": "activity", "value": "会议结束了，有点累", "evidence": "开完了", "until": "3h"}}])
    assert _busy_cleared(session, {"n": 12})["ok"] is True


def test_an_early_end_of_another_kind_is_reported_honestly(session):
    """Product behaviour (not changed here): a new situation only replaces one of the same kind."""
    from tools.dialogue_eval import _busy_cleared
    _say(session, "我在开会，晚点聊", 9,
         [{"tool": "set_situation", "args": {"kind": "availability", "value": "在开会", "evidence": "在开会", "until": "2h"}}])
    session.advance(0.5)
    _say(session, "开完了，好累", 10,
         [{"tool": "set_situation", "args": {"kind": "activity", "value": "会议结束了", "evidence": "开完了", "until": "3h"}}])
    result = _busy_cleared(session, {"n": 10})
    assert result["ok"] is False and "仍然有效" in result["failures"][0]


def test_life_events_keep_their_time_and_share_count(session):
    from tools.dialogue_eval import preload
    preload(session, PLAN)
    before = session.db.fetch_all("SELECT id, occurred_at, share_count, created_at FROM life_events")
    session.advance(30)
    preload(session, PLAN)                                     # a continued run calls preload again
    assert session.db.fetch_all("SELECT id, occurred_at, share_count, created_at FROM life_events") == before


def test_time_passing_sends_nothing_and_calls_no_model(session):
    session.db.add_commitment("提醒小满交实验报告", (session.clock.now() + timedelta(hours=1)).isoformat(),
                              kind="reminder", evidence="提醒我交实验报告")
    sent, calls = len(session.wechat.sent), len(session.recorder.calls)
    session.advance(48)
    assert len(session.wechat.sent) == sent and len(session.recorder.calls) == calls
    assert session.db.fetch_one("SELECT status FROM commitments")["status"] == "pending"   # only a tick delivers


def test_legacy_advance_time_is_no_longer_used_by_the_tools():
    """Live rounds and the clock never rewrite history. Since phase 7 B.5 the only callers are the replay of
    rounds that were RECORDED under the pre-B.4 mechanism, each one behind an explicit legacy guard."""
    for name in ("harness.py", "dialogue_eval.py", "violet_test_chat.py"):
        code = (PROJECT / "tools" / name).read_text(encoding="utf-8")
        calls = [line for line in code.splitlines() if "advance_time(" in line and "def advance_time" not in line]
        assert calls == [], name
    lines = (PROJECT / "tools" / "replay.py").read_text(encoding="utf-8").splitlines()
    calls = [i for i, line in enumerate(lines) if "advance_time(" in line and "import" not in line]
    assert len(calls) == 2
    for i in calls:
        guard = lines[i - 1].strip()
        assert guard in ('if r.get("advance_hours") and seg["legacy_time"]:', "if legacy:"), guard


# ---------------------------------------------------------------------------
# Plan revisions
# ---------------------------------------------------------------------------

def test_the_base_plan_is_unchanged_and_revision_2_only_moves_round_15():
    from tools.replay import load_plan
    assert hashlib.sha256(BASE_PLAN.read_bytes()).hexdigest() == BASE_SHA
    plan, meta = load_plan(BASE_PLAN)
    assert meta["revision"] == 2 and meta["changed_rounds"] == [15] and meta["base_sha256"] == BASE_SHA
    r15 = next(r for r in plan["rounds"] if r["n"] == 15)
    assert "advance_hours" not in r15 and r15["time"]["base_round"] == 4
    assert r15["user"] == "答辩结束了，挺顺利的" and r15["memory"][0]["tool"] == "update_event_progress"
    base, base_meta = load_plan(BASE_PLAN, "none")
    assert base_meta["revision"] == 1 and next(r for r in base["rounds"] if r["n"] == 15)["advance_hours"] == 30
    others = [r for r in plan["rounds"] if r["n"] != 15]
    assert others == [r for r in base["rounds"] if r["n"] != 15]


@pytest.mark.parametrize("edit,message", [
    (lambda rev: rev.update(base_sha256="0" * 64), "哈希不一致"),
    (lambda rev: rev["rounds"]["15"]["set"].update(user="改过的话"), "不允许改动"),
    (lambda rev: rev["rounds"].update({"99": {"set": {"advance_hours": 1}}}), "不存在"),
])
def test_bad_revisions_are_refused(tmp_path, edit, message):
    from tools.replay import ReplaySourceError, load_plan
    plan_copy = tmp_path / BASE_PLAN.name
    shutil.copy(BASE_PLAN, plan_copy)
    rev = json.loads((BASE_PLAN.parent / "acceptance_dialogue_30.rev2.json").read_text(encoding="utf-8"))
    edit(rev)
    (tmp_path / "acceptance_dialogue_30.rev2.json").write_text(json.dumps(rev, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ReplaySourceError, match=message):
        load_plan(plan_copy)


def test_round15_time_follows_the_defense_of_round4():
    from zoneinfo import ZoneInfo
    from tools.replay import scenario_target
    spec = {"base_round": 4, "weeks_ahead": 1, "weekday": 3, "plus_days": 1, "at": "10:00"}
    tz = ZoneInfo("Asia/Shanghai")
    # round 4 on Tuesday 2026-10-13 ("下周三" = 2026-10-21) -> round 15 on Thursday 2026-10-22 10:00
    for base in ("2026-10-13T09:38:02+08:00", "2026-10-13T05:02:02+08:00", "2026-10-12T21:00:00-07:00"):
        assert scenario_target(spec, datetime.fromisoformat(base), tz).isoformat() == "2026-10-22T10:00:00+08:00"
    # a Sunday counts to the week it ends: next week's Wednesday is three days later
    assert scenario_target(spec, datetime.fromisoformat("2026-10-18T22:00:00+08:00"), tz).date().isoformat() == "2026-10-22"


# ---------------------------------------------------------------------------
# Existing directories: read-only compatibility and continuation
# ---------------------------------------------------------------------------

def _old_style_workdir(tmp_path, done=10) -> Path:
    """A kept directory as phase 7 B.3 wrote it: no scenario times, no plan revisions."""
    import sqlite3
    workdir = tmp_path / "violet-testchat-old"
    workdir.mkdir()
    state = {"version": 1, "plan": str(BASE_PLAN), "plan_sha256": BASE_SHA,
             "replay": {"source": "report.json", "sha256": "x", "rounds": [1, 2, 3, 4, 5], "status": "complete"},
             "rounds": [{"n": n, "mode": "replay"} for n in range(1, 6)] +
                       [{"n": n, "mode": "real", "scripted_memory": False} for n in range(6, done + 1)],
             "clock_end": "2026-10-13T09:46:45.461251+08:00"}
    (workdir / ".violet-eval-state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    conn = sqlite3.connect(workdir / "violet-test.db")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT, created_at TEXT)")
    conn.execute("INSERT INTO messages (role, content, created_at) VALUES ('user', ?, '2026-10-13T05:02:02+08:00')",
                 (PLAN["rounds"][3]["user"],))
    conn.commit()
    conn.close()
    return workdir


def _digest(path: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.iterdir()) if p.is_file()}


def test_an_old_style_directory_is_read_only_compatible(tmp_path):
    from tools.replay import ReplaySourceError, check_workdir, load_plan
    workdir = _old_style_workdir(tmp_path)
    before = _digest(workdir)
    plan, meta = load_plan(BASE_PLAN)
    assert check_workdir(workdir, False, set(range(11, 16)), True, meta)["mode"] == "continue"
    for rounds, message in ((set(range(10, 16)), "已经完成"), ({12, 13}, "必须从第 11 轮开始"),
                            (set(range(1, 16)), "已经完成")):
        with pytest.raises(ReplaySourceError, match=message):
            check_workdir(workdir, False, rounds, True, meta)
    with pytest.raises(ReplaySourceError, match="拒绝再次回放"):
        check_workdir(workdir, True, set(range(11, 16)), True, meta)
    with pytest.raises(ReplaySourceError, match="剧本哈希不一致"):
        check_workdir(workdir, False, set(range(11, 16)), True, {**meta, "base_sha256": "0" * 64})
    assert _digest(workdir) == before                                    # nothing was written


def test_a_revision_may_not_touch_finished_rounds(tmp_path):
    from tools.replay import ReplaySourceError, check_workdir, load_plan
    workdir = _old_style_workdir(tmp_path, done=15)
    _, meta = load_plan(BASE_PLAN)
    with pytest.raises(ReplaySourceError, match="已经完成的第 \\[15\\] 轮"):
        check_workdir(workdir, False, {16}, True, meta)
    _, base_meta = load_plan(BASE_PLAN, "none")
    assert check_workdir(workdir, False, {16}, True, base_meta)["mode"] == "continue"


def test_round4_date_of_an_old_directory_comes_from_its_stored_message(violetdb):
    from tools.replay import round_start
    violetdb.add_user_message("text", PLAN["rounds"][3]["user"], created_at="2026-10-13T05:02:02+08:00")
    assert round_start(violetdb, PLAN, 4, {}).isoformat() == "2026-10-13T05:02:02+08:00"
    assert round_start(violetdb, PLAN, 4, {4: "2026-03-02T10:01:20+08:00"}).isoformat().startswith("2026-03-02")


@pytest.fixture()
def run_eval(clean_env, violetdb, tmp_path, monkeypatch):
    from tools import dialogue_eval, violet_test_chat
    real_open = violet_test_chat.open_session
    monkeypatch.setattr(violet_test_chat, "open_session",
                        lambda args, input_fn=input, **kw: real_open(args, input_fn=input_fn, out=lambda s: None,
                                                                     db=violetdb, **kw))
    counter = {"n": 0}

    def run(*argv):
        counter["n"] += 1
        out = tmp_path / f"out{counter['n']}"
        rc = dialogue_eval.main([*argv, "--output-dir", str(out)])
        report = json.loads((out / "report.json").read_text(encoding="utf-8")) if (out / "report.json").exists() else None
        return rc, report
    return run


def test_rounds_11_to_15_continue_without_repeating_history(run_eval, violetdb, tmp_path, capsys):
    workdir = tmp_path / "violet-testchat-11to15"
    report_path = write_report(tmp_path)
    assert run_eval("--replay-from", str(report_path), "--rounds", "6-10", "--scripted-memory", "--quiet-hours", "off",
                    "--workdir", str(workdir), "--keep")[0] == 0
    history = violetdb.fetch_all("SELECT id, role, content, created_at FROM messages ORDER BY id")
    rc, report = run_eval("--rounds", "11-15", "--scripted-memory", "--workdir", str(workdir))
    assert rc == 0 and report["continuation"]["completed_rounds_before"] == list(range(1, 11))
    assert report["plan"]["revision"] == 2
    assert violetdb.fetch_all("SELECT id, role, content, created_at FROM messages ORDER BY id")[:len(history)] == history
    assert len(violetdb.fetch_all("SELECT id FROM messages WHERE role = 'user'")) == 15          # nothing repeated
    by_n = {r["n"]: r for r in report["rounds"]}
    # round 15: the morning after the defense day (fixture: round 4 on Monday 2026-03-02, defense 03-11)
    assert by_n[15]["scenario_time"].startswith("2026-03-12T10:00")
    advance = by_n[15]["time_advance"]
    assert advance["to"].startswith("2026-03-12T10:00") and advance["hours"] > 200
    assert "家里" in [s["value"] for s in advance["situations_expired"]]                       # today's location expired
    assert by_n[11]["auto"]["context_checks"]["has"] == {"此刻在哪里、在做什么：不知道": True}
    assert "scripted memory in round 12" in by_n[13]["auto"]["context_checks"]["matched_by"]["此刻所在：家里"]
    assert violetdb.fetch_one("SELECT event_time FROM event_memories")["event_time"].startswith("2026-03-11")
    state = json.loads((workdir / ".violet-eval-state.json").read_text(encoding="utf-8"))
    assert [r["n"] for r in state["rounds"]] == list(range(1, 16))
    assert state["rounds"][14]["scenario_time"].startswith("2026-03-12") and state["rounds"][14]["plan_revision"] == 2
    rc, _ = run_eval("--rounds", "11-15", "--workdir", str(workdir))
    assert rc == 2 and "已经完成" in capsys.readouterr().out
    shutil.rmtree(workdir)


def test_a_plan_time_in_the_scenario_past_is_refused_before_any_call(run_eval, tmp_path, capsys):
    workdir = tmp_path / "violet-testchat-late"
    assert run_eval("--replay-from", str(write_report(tmp_path)), "--rounds", "6-14", "--scripted-memory",
                    "--quiet-hours", "off", "--workdir", str(workdir), "--keep")[0] == 0
    state = json.loads((workdir / ".violet-eval-state.json").read_text(encoding="utf-8"))
    state["clock_end"] = "2026-04-01T09:00:00+08:00"                     # the scenario is already past 03-12
    (workdir / ".violet-eval-state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    rc, report = run_eval("--rounds", "15", "--workdir", str(workdir))
    assert rc == 2 and report is None and "早于当前场景时间" in capsys.readouterr().out
    shutil.rmtree(workdir)


# ---------------------------------------------------------------------------
# Rounds 11 / 13: structured location checks
# ---------------------------------------------------------------------------

def _situation(violetdb, kind, value, turn_id=None, user_message=None):
    from ai.profile_store import Profile
    result = Profile(violetdb).situations.observe(kind, value, "user_stated", evidence=value[-2:],
                                               user_message=user_message or value, until="today", turn_id=turn_id)
    return result["situation"]["id"]


def _memory_job(violetdb, turn_id, text):
    violetdb.add_memory_job(turn_id, text, None, status="pending")


def _check(violetdb, text, situation_ids, modes, real=True, system=""):
    from tools.dialogue_eval import context_checks
    return context_checks(violetdb, {"context_has": [text]}, system, {"situation_ids": situation_ids},
                          {"plan": PLAN, "round": 13, "modes": modes, "real": real})


@pytest.mark.parametrize("value", ["家里", "在家", "家中", "在家里", "回家了"])
def test_round13_accepts_equivalent_home_values_written_by_the_real_memory_agent(violetdb, value):
    _memory_job(violetdb, "turn-12", PLAN["rounds"][11]["user"])
    sid = _situation(violetdb, "location", value, "turn-12", f"今天回家了，{value}")
    result = _check(violetdb, "此刻所在：家里", [sid], {12: {"mode": "real", "scripted_memory": False}})
    assert result["has"]["此刻所在：家里"] is True
    assert "MemoryAgent (real model) in round 12" in result["matched_by"]["此刻所在：家里"]


@pytest.mark.parametrize("setup,reason", [
    ("none", "位置记忆未写入"),
    ("friend", "不是家"),
    ("no_job", "找不到"),
    ("scripted", "不是真实 MemoryAgent 写入的"),
])
def test_round13_fails_honestly(violetdb, setup, reason):
    ids = []
    modes = {12: {"mode": "real", "scripted_memory": False}}
    if setup == "friend":
        _memory_job(violetdb, "turn-12", PLAN["rounds"][11]["user"])
        ids = [_situation(violetdb, "location", "朋友家", "turn-12", "在朋友家")]
    elif setup == "no_job":
        ids = [_situation(violetdb, "location", "家里", None, "家里")]
    elif setup == "scripted":
        _memory_job(violetdb, "turn-12", PLAN["rounds"][11]["user"])
        ids = [_situation(violetdb, "location", "家里", "turn-12", "家里")]
        modes = {12: {"mode": "mock", "scripted_memory": True}}
    # the chat history saying 家 is irrelevant: only the round's context situations count
    result = _check(violetdb, "此刻所在：家里", ids, modes, system="- 此刻所在：家里\n用户说：今天回家了，在家呢")
    assert result["has"]["此刻所在：家里"] is False and reason in result["failures"]["此刻所在：家里"]


def test_only_the_situations_of_this_rounds_context_count(violetdb):
    """A location stored somewhere in the database but not carried by this round's context does not count."""
    _memory_job(violetdb, "turn-12", PLAN["rounds"][11]["user"])
    home = _situation(violetdb, "location", "家里", "turn-12", "今天回家了，在家里呢")
    activity = _situation(violetdb, "activity", "会议已结束，用户感到疲惫")
    assert _check(violetdb, "此刻在哪里、在做什么：不知道", [activity], {})["has"]["此刻在哪里、在做什么：不知道"] is True
    result = _check(violetdb, "此刻所在：家里", [activity], {12: {"mode": "real", "scripted_memory": False}})
    assert result["has"]["此刻所在：家里"] is False and "位置记忆未写入" in result["failures"]["此刻所在：家里"]
    assert home


def test_round11_unknown_location_is_judged_on_structure(violetdb):
    activity = _situation(violetdb, "activity", "会议已结束，用户感到疲惫")
    ok = _check(violetdb, "此刻在哪里、在做什么：不知道", [activity], {})
    assert ok["has"]["此刻在哪里、在做什么：不知道"] is True                 # no literal line, still no location
    home = _situation(violetdb, "location", "家里")
    bad = _check(violetdb, "此刻在哪里、在做什么：不知道", [activity, home], {},
                 system="- 此刻在哪里、在做什么：不知道")                         # the literal line alone is not enough
    assert bad["has"]["此刻在哪里、在做什么：不知道"] is False and "家里" in bad["failures"]["此刻在哪里、在做什么：不知道"]
