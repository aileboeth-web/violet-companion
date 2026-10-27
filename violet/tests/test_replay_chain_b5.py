"""Phase 7 B.5: replay of several real reports in a row (rounds 1-5, then rounds 6-10), including situations,
the pre-B.4 time mechanism those reports were recorded with, provenance and integrity checks. Offline only."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.test_replay_b3 import ANCHOR, EVENT, PLAN, make_report, write_report

ROUND_S = 80                                              # make_report: 4 calls x 20 s per round
BUSY_ORIGINAL = ANCHOR.replace(hour=12)                   # the busy situation, as written in round 9
DAY_END = ANCHOR.replace(hour=23, minute=59)
REPLIES_2 = {6: ["挺好看的，文字很清淡"], 7: ["不过我觉得小说不一定非得有用"], 8: ["哼，敷衍得还挺理直气壮嘛"],
             9: ["好，先忙你的，晚点见"], 10: ["辛苦啦，先歇会儿吧"]}


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def make_report2(report1: Path, legacy: bool = True, **changes) -> dict:
    """Rounds 6-10 the way the phase 7 B.3 / B.4 tools wrote them, continued from a replay of report1."""
    shift = timedelta(hours=1.6 + 3) if legacy else timedelta(0)
    start = ANCHOR + timedelta(seconds=5 * ROUND_S)
    times = {n: start + timedelta(seconds=(n - 6) * ROUND_S) for n in range(6, 10)}
    times[10] = times[9] + timedelta(seconds=ROUND_S) + (timedelta(0) if legacy else timedelta(hours=4.6))
    busy_now = BUSY_ORIGINAL - (timedelta(hours=1.6) if legacy else timedelta(0))
    rounds = []
    for r in PLAN["rounds"][5:10]:
        n = r["n"]
        mem = []
        if n == 9:
            mem = [{"tool": "set_situation", "ok": True, "action": "created", "situation_id": 1}]
        if n == 10:
            mem = [{"tool": "set_situation", "ok": True, "action": "created", "situation_id": 2}]
        record = {"n": n, "user": r["user"], "planned_user": r["user"], "violet": REPLIES_2[n], "reply_latency_ms": 30000,
                  "call_details": [{"agent": a, "latency_ms": 20000, "tool_calls": [], "tool_results": []}
                                   for a in ("IntentAgent", "ChatAgent")] +
                                  [{"agent": "MemoryAgent", "latency_ms": 20000, "tool_calls": [], "tool_results": mem},
                                   {"agent": "MemoryAgent", "latency_ms": 20000, "tool_calls": [],
                                    "tool_results": [{"tool": "memory_done", "ok": True, "action": "done_requested"}]}],
                  "proactive": []}
        if not legacy:
            record["scenario_time"] = _iso(times[n])
        if n == 9:
            record["proactive"] = [{"check": "blocked_busy", "category": "busy", "sent": [], "ok": True,
                                    "due_reason": f"the user said they are busy (正在开会) until {_iso(busy_now)[5:16]}",
                                    "busy_situation": {"id": 1, "kind": "activity", "value": "正在开会",
                                                       "status": "active", "expires_at": _iso(BUSY_ORIGINAL)}}]
        if n == 10:
            record["proactive"] = [{"check": "busy_cleared", "after_round": 9, "ok": True, "situations": [
                {"id": 1, "kind": "activity", "value": "正在开会", "status": "expired",
                 "expires_at": _iso(BUSY_ORIGINAL - shift)},
                {"id": 2, "kind": "activity", "value": "会议结束了", "status": "active", "expires_at": _iso(DAY_END)}]}]
        rounds.append(record)
    first = make_report()["final_state"]
    report = {
        "real": True, "rounds": rounds,
        "continuation": {"mode": "replay", "workdir": "somewhere", "completed_rounds_before": []},
        "replay": {"sha256": hashlib.sha256(report1.read_bytes()).hexdigest(), "rounds": [1, 2, 3, 4, 5],
                   "scenario_clock": {"end_of_replay": _iso(start)}},
        "scenario_clock": {"start": _iso(ANCHOR), "end": _iso(times[10] + timedelta(seconds=ROUND_S))},
        "final_state": {
            "facts": first["facts"], "memories": first["memories"], "commitments": [],
            "situations": [{"kind": "activity", "value": "正在开会", "expires_at": _iso(BUSY_ORIGINAL - shift)},
                           {"kind": "activity", "value": "会议结束了", "expires_at": _iso(DAY_END)}],
            "life_events": [{"content": "又往后读了几章《边城》", "share_count": 1,
                             "occurred_at": _iso(ANCHOR - timedelta(hours=20) - shift)}],
        },
    }
    for key, value in changes.items():
        report[key] = value
    return report


def reports(tmp_path, legacy=True, edit2=None):
    r1 = write_report(tmp_path, make_report(), "r1.json")
    data2 = make_report2(r1, legacy)
    if edit2:
        edit2(data2)
    r2 = write_report(tmp_path, data2, "r2.json")
    return r1, r2


@pytest.fixture()
def clean_env():
    saved = dict(os.environ)
    yield os.environ
    os.environ.clear()
    os.environ.update(saved)


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


# ---------------------------------------------------------------------------
# The chain as one history
# ---------------------------------------------------------------------------

def test_two_reports_rebuild_rounds_1_to_10_and_round_11_has_the_a1_premise(run_eval, violetdb, tmp_path):
    r1, r2 = reports(tmp_path)
    workdir = tmp_path / "violet-testchat-chain"
    rc, report = run_eval("--replay-from", str(r1), "--replay-from", str(r2), "--rounds", "11", "--scripted-memory",
                          "--quiet-hours", "off", "--workdir", str(workdir), "--keep")
    assert rc == 0
    replay = report["replay"]
    assert replay["rounds"] == list(range(1, 11)) and replay["billed_calls"] == 0
    assert [s["rounds"] for s in replay["segments"]] == [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10]]
    assert [s["messages"] for s in replay["segments"]] == [13, 23]          # cumulative: 5 + 8 replies, then + 10
    assert replay["legacy_time_rounds"] == [9, 10]
    assert replay["proactive_checks_replayed"][0]["matches_record"] is True
    assert [{k: s[k] for k in ("kind", "value", "expires_at")} for s in replay["effective_situations_at_end"]] == [
        {"kind": "activity", "value": "会议结束了", "expires_at": _iso(DAY_END)}]
    assert [p["written_by_memory_job_of"] for p in replay["situation_provenance"]] == ["我在开会，晚点聊", "开完了，好累"]
    assert all(c["drift_s"] < 2 for c in replay["scenario_clock"]["checks"])
    r11 = report["rounds"][0]
    assert r11["n"] == 11 and r11["auto"]["context_checks"]["has"] == {"此刻在哪里、在做什么：不知道": True}
    chat = next(r for r in r11["runs"] if r["agent"] == "ChatAgent")
    shown = violetdb.fetch_all(f"SELECT kind, value FROM user_situations WHERE id IN "
                            f"({', '.join(str(i) for i in chat['context']['situation_ids'])})")
    assert [(s["kind"], s["value"]) for s in shown] == [("activity", "会议结束了")]   # the activity only: no location
    state = json.loads((workdir / ".violet-eval-state.json").read_text(encoding="utf-8"))
    assert [(r["n"], r["mode"]) for r in state["rounds"]] == [(n, "replay") for n in range(1, 11)] + [(11, "mock")]
    assert [s["rounds"] for s in state["replay"]["sources"]] == [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10]]
    shutil.rmtree(workdir)


def _replayed_session(violetdb, tmp_path, r1, r2, *extra):
    from tools.dialogue_eval import preload
    from tools.violet_test_chat import build_parser, open_session
    from tools.harness import ScenarioClock
    from tools.replay import load_plan, load_replay_chain, run_replay
    plan, meta = load_plan(Path(__file__).resolve().parent / "samples" / "acceptance_dialogue_30.json")
    chain = load_replay_chain([r1, r2], plan, 11, meta["changed_rounds"])
    session = open_session(build_parser().parse_args(["--workdir", str(tmp_path / "violet-testchat-s"), *extra]),
                           input_fn=lambda prompt="": "yes", out=lambda s: None, db=violetdb)
    session.use_clock(ScenarioClock(datetime.fromisoformat(chain["anchor"])))
    preload(session, plan)
    return session, chain, run_replay(session, chain, out=lambda s: None)


def test_situations_events_messages_and_times_follow_the_legacy_record(clean_env, violetdb, tmp_path):
    r1, r2 = reports(tmp_path)
    session, chain, summary = _replayed_session(violetdb, tmp_path, r1, r2)
    try:
        rows = violetdb.fetch_all("SELECT * FROM user_situations ORDER BY id")
        assert [(s["kind"], s["value"], s["expires_at"], s["status"]) for s in rows] == [
            ("activity", "正在开会", _iso(BUSY_ORIGINAL - timedelta(hours=4.6)), "expired"),
            ("activity", "会议结束了", _iso(DAY_END), "active")]
        assert [s["evidence"] for s in rows] == ["我在开会，晚点聊", "开完了，好累"]
        writes = summary["restored_writes"]
        assert writes[9][0]["expires_source"].startswith("recorded by the round's busy check")
        assert writes[10][0]["expires_source"].startswith("final_state expires_at + 0")
        messages = violetdb.fetch_all("SELECT role, content, created_at FROM messages ORDER BY id")
        assert len(messages) == 23 and messages[-1]["content"] == REPLIES_2[10][0]
        first = datetime.fromisoformat(messages[0]["created_at"])
        assert abs((first - (ANCHOR - timedelta(hours=4.6))).total_seconds()) < 2      # rounds 1-9: shifted 1.6 h + 3 h
        r10 = next(m for m in messages if m["content"] == "开完了，好累")
        assert abs((datetime.fromisoformat(r10["created_at"]) - (ANCHOR + timedelta(seconds=9 * ROUND_S))).total_seconds()) < 2
        event = violetdb.fetch_one("SELECT * FROM event_memories")
        assert event["content"] == EVENT and event["progress"] == "planned"
        # phase 9.1.1: the recorded save had only a date; it is stored as the date (no 23:59) and the legacy 4.6 h
        # shift keeps it on that day
        assert event["event_time"] == "2026-03-11"
        life = violetdb.fetch_one("SELECT occurred_at, share_count FROM life_events")
        assert life["share_count"] == 1
        assert abs((datetime.fromisoformat(life["occurred_at"]) -
                    (ANCHOR - timedelta(hours=24.6))).total_seconds()) < 2
        assert session.recorder.calls == [] and summary["offline_answers"] > 0
        assert summary["scenario_clock"]["end_of_replay"] == _iso(ANCHOR + timedelta(seconds=10 * ROUND_S))
    finally:
        session.close()


def test_new_style_reports_replay_time_on_the_clock_without_rewriting_history(clean_env, violetdb, tmp_path):
    r1, r2 = reports(tmp_path, legacy=False)
    session, chain, summary = _replayed_session(violetdb, tmp_path, r1, r2)
    try:
        assert summary["legacy_time_rounds"] == []
        first = violetdb.fetch_one("SELECT created_at FROM messages ORDER BY id")["created_at"]
        assert abs((datetime.fromisoformat(first) - ANCHOR).total_seconds()) < 2          # never shifted
        rows = violetdb.fetch_all("SELECT expires_at, status FROM user_situations ORDER BY id")
        assert [(r["expires_at"], r["status"]) for r in rows] == [(_iso(BUSY_ORIGINAL), "expired"), (_iso(DAY_END), "active")]
        assert violetdb.fetch_one("SELECT event_time FROM event_memories")["event_time"] == "2026-03-11"   # phase 9.1.1
    finally:
        session.close()


def test_the_chain_creates_no_real_client_and_spends_no_budget(clean_env, violetdb, tmp_path, monkeypatch):
    import openai
    from tools.harness import Budget, MockLLM
    built = []

    class FakeOpenAI(MockLLM):
        def __init__(self, **kwargs):
            built.append(kwargs)
            super().__init__()
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    clean_env.update({"VIOLET_TEST_ALLOW_REAL_API": "1", "VIOLET_TEST_API_KEY": "sk-test-chain-0123456789"})
    r1, r2 = reports(tmp_path)
    session, chain, summary = _replayed_session(violetdb, tmp_path, r1, r2, "--real", "--api-url", "https://t.example/v1")
    try:
        session.recorder.budget = Budget(max_calls_total=1, max_tokens_total=1)
        assert built == [] and session.recorder.calls == [] and session.recorder.stopped is None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Refusals and failures (nothing paid, nothing half-finished)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("edit,message", [
    (lambda r: r["rounds"].pop(2), "缺少第 [8] 轮"),
    (lambda r: r["rounds"].insert(0, {**r["rounds"][0], "n": 5, "user": PLAN["rounds"][4]["user"],
                                      "planned_user": PLAN["rounds"][4]["user"]}), "重叠或顺序不对"),
    (lambda r: r["replay"].update(sha256="0" * 64), "哈希不一致"),
    (lambda r: r["continuation"].update(mode="continue"), "无法确认报告"),
    (lambda r: r["rounds"][3]["call_details"][2]["tool_results"][0].update(situation_id=7), "情境 7"),
    (lambda r: r["final_state"]["situations"][0].pop("expires_at"), "缺少类型、内容或过期时间"),
    (lambda r: r["rounds"][3]["proactive"][0]["busy_situation"].update(expires_at="2026-03-02T13:00:00+08:00"),
     "推算的不一致"),
    (lambda r: r["rounds"][4].update(user="开完了"), "用户原话与验收剧本不一致"),
    (lambda r: r.update(real=False), "真实模型测试的报告"),
    (lambda r: r["final_state"]["situations"].append({"kind": "location", "value": "家里",
                                                      "expires_at": "2026-03-02T23:59:00+08:00"}), "无法逐条确认"),
])
def test_broken_chains_are_refused_before_anything_runs(run_eval, tmp_path, capsys, edit, message):
    r1, r2 = reports(tmp_path, edit2=edit)
    workdir = tmp_path / "violet-testchat-refused"
    rc, _ = run_eval("--replay-from", str(r1), "--replay-from", str(r2), "--rounds", "11", "--workdir", str(workdir))
    assert rc == 2 and message in capsys.readouterr().out and not workdir.exists()


def test_reports_in_the_wrong_order_or_more_than_two_are_refused(run_eval, tmp_path, capsys):
    r1, r2 = reports(tmp_path)
    assert run_eval("--replay-from", str(r2), "--replay-from", str(r1), "--rounds", "11")[0] == 2
    assert "本身是在回放之上跑的" in capsys.readouterr().out
    assert run_eval("--replay-from", str(r1), "--replay-from", str(r2), "--replay-from", str(r2), "--rounds", "11")[0] == 2
    assert "重叠或顺序不对" in capsys.readouterr().out


def test_a_revision_touching_replayed_rounds_is_refused(tmp_path):
    from tools.replay import ReplaySourceError, load_replay_chain
    r1, r2 = reports(tmp_path)
    with pytest.raises(ReplaySourceError, match="改动了要回放的第 \\[9\\] 轮"):
        load_replay_chain([r1, r2], PLAN, 11, changed_rounds=[9, 15])


@pytest.mark.parametrize("edit,message", [
    (lambda r: r["rounds"][3]["proactive"][0].update(due_reason="quiet hours"), "主动消息检查结果与记录不一致"),
    (lambda r: r["rounds"][4]["proactive"][0]["situations"][1].update(status="expired"), "结束时的情境与记录不一致"),
    (lambda r: r["final_state"]["life_events"][0].update(occurred_at="2026-02-28T09:00:00+08:00"), "生活事件的时间"),
    (lambda r: r["final_state"]["situations"][1].update(value="会议还没结束"), "情境"),
    (lambda r: r.update(scenario_clock={"start": "x", "end": "2026-03-03T10:00:00+08:00"}), "结束时的场景时间"),
    (lambda r: r["replay"]["scenario_clock"].update(end_of_replay="2026-03-02T09:00:00+08:00"), "开头时的场景时间"),
    (lambda r: r["final_state"]["memories"][0].update(progress="done"), "回放后的状态与真实报告不一致"),
])
def test_a_replay_that_does_not_reproduce_the_record_stops_before_any_live_round(run_eval, violetdb, tmp_path, capsys,
                                                                                  edit, message):
    r1, r2 = reports(tmp_path, edit2=edit)
    workdir = tmp_path / "violet-testchat-mismatch"
    rc, report = run_eval("--replay-from", str(r1), "--replay-from", str(r2), "--rounds", "11",
                          "--workdir", str(workdir), "--keep")
    out = capsys.readouterr().out
    assert rc in (2, 4) and report is None and message in out
    if rc == 4:
        state = json.loads((workdir / ".violet-eval-state.json").read_text(encoding="utf-8"))
        assert state["replay"]["status"] == "failed" and state["rounds"] == []
        assert run_eval("--rounds", "11", "--workdir", str(workdir))[0] == 2      # never continued on
    assert not violetdb.fetch_all("SELECT id FROM messages WHERE content = '猜猜我现在在哪'")
    if workdir.exists():
        shutil.rmtree(workdir)


def test_single_report_replay_still_works_as_before(run_eval, tmp_path):
    rc, report = run_eval("--replay-from", str(write_report(tmp_path)), "--rounds", "6")
    assert rc == 0 and report["replay"]["rounds"] == [1, 2, 3, 4, 5] and len(report["replay"]["segments"]) == 1
