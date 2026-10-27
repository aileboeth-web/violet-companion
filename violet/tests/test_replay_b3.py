"""Phase 7 B.3: offline replay of earlier rounds from a real report, continuation of a kept directory,
and the round-9 / round-10 busy checks. Everything offline (mock model, temp directories)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
PLAN = json.loads((PROJECT / "tests" / "samples" / "acceptance_dialogue_30.json").read_text(encoding="utf-8"))
ANCHOR = datetime.fromisoformat("2026-03-02T10:00:00+08:00")       # far from the host's date on purpose
REPLIES = {
    1: ["好呀，小满，以后就这么叫你了"],
    2: ["天文学诶，小满你这专业听起来就很浪漫", "不过大二应该也开始被数学和物理轮番收拾了吧"],
    3: ["那你周末回家就好好享受一下"],
    4: ["紧张很正常啦", "把观测目的、方法和结论理顺，到时候就不慌了"],
    5: ["我最近在慢慢读《边城》，读到几章的时候总觉得沈从文写得特别安静", "日子过得挺慢的，小满"],
}
EVENT = "用户将于2026-03-11参加天文观测课程答辩，并对此感到有些紧张。"


def _call(agent, tools=(), latency=20000):
    return {"agent": agent, "latency_ms": latency, "tool_calls": [t["tool"] for t in tools], "tool_results": list(tools)}


def make_report(**changes) -> dict:
    """The shape of a phase 7 B.2 real report for rounds 1-5 (fictional content)."""
    writes = {
        1: [{"tool": "remember_profile_fact", "key": "address_name", "ok": True, "action": "created", "fact_id": 1}],
        2: [{"tool": "remember_profile_fact", "key": k, "ok": True, "action": "created", "fact_id": i}
            for i, k in ((2, "study.school"), (3, "study.grade"), (4, "study.major"))],
        3: [{"tool": "remember_profile_fact", "key": "residence.usual", "ok": True, "action": "created", "fact_id": 5}],
        4: [{"tool": "query_memory", "memories_count": 0},
            {"tool": "save_memory", "ok": True, "action": "created", "memory_id": 1}],
        5: [],
    }
    rounds = []
    for r in PLAN["rounds"][:5]:
        n = r["n"]
        details = [_call("IntentAgent"), _call("ChatAgent"),
                   _call("MemoryAgent", writes[n]), _call("MemoryAgent", [{"tool": "memory_done", "ok": True,
                                                                           "action": "done_requested"}])]
        rounds.append({"n": n, "user": r["user"], "planned_user": r["user"], "violet": REPLIES[n],
                       "reply_latency_ms": 30000, "call_details": details})
    report = {
        "real": True, "rounds": rounds,
        "final_state": {
            "facts": [{"fact_key": k, "value": v, "status": "active"} for k, v in (
                ("address_name", "小满"), ("study.school", "星河大学"), ("study.grade", "大二"), ("study.major", "天文"),
                ("residence.usual", "平时住学校宿舍，周末偶尔回家"))],
            "situations": [], "commitments": [],
            "memories": [{"id": 1, "content": EVENT, "kind": "event", "progress": "planned", "status": "active"}],
            "life_events": [{"content": "又往后读了几章《边城》", "share_count": 1,
                             "occurred_at": (ANCHOR - timedelta(hours=20)).isoformat()}],
        },
    }
    for key, value in changes.items():
        report[key] = value
    return report


def write_report(tmp_path, report=None, name="report.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(report or make_report(), ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture()
def clean_env():
    saved = dict(os.environ)
    yield os.environ
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture()
def run_eval(clean_env, violetdb, tmp_path, monkeypatch):
    """dialogue_eval.main on the test database, report written to a temp dir."""
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
# Refusals before anything runs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mutate,message", [
    (lambda r: r["rounds"][2].update(user="平时住家里"), "用户原话与验收剧本不一致"),
    (lambda r: r["rounds"].pop(3), "缺少第 [4] 轮"),
    (lambda r: r["rounds"].pop(4), "缺少第 [5] 轮"),                      # the report ends too early
    (lambda r: r["rounds"][1].pop("call_details"), "缺少调用明细"),
    (lambda r: r["rounds"][0].update(violet=[]), "没有记录 Violet 的回复"),
    (lambda r: r.update(real=False), "真实模型测试的报告"),
    (lambda r: r["rounds"].append({**r["rounds"][4], "n": 6}), "还包含第 [6] 轮"),
    (lambda r: r["final_state"]["facts"][1].update(value="月光大学"), "没有逐字出现在用户原话里"),
    (lambda r: r["final_state"]["life_events"].clear(), "无法确定剧本时钟"),
])
def test_bad_reports_are_refused_before_anything_is_created(run_eval, tmp_path, capsys, mutate, message):
    report = make_report()
    mutate(report)
    workdir = tmp_path / "violet-testchat-refused"
    rc, _ = run_eval("--replay-from", str(write_report(tmp_path, report)), "--rounds", "6", "--workdir", str(workdir))
    assert rc == 2 and message in capsys.readouterr().out
    assert not workdir.exists()


# ---------------------------------------------------------------------------
# A replay
# ---------------------------------------------------------------------------

def test_replay_rebuilds_the_state_through_the_pipeline_on_the_scenario_clock(run_eval, violetdb, tmp_path):
    workdir = tmp_path / "violet-testchat-replay"
    rc, report = run_eval("--replay-from", str(write_report(tmp_path)), "--rounds", "6", "--workdir", str(workdir),
                          "--keep")
    assert rc == 0
    replay = report["replay"]
    assert replay["rounds"] == [1, 2, 3, 4, 5] and replay["billed_calls"] == 0 and replay["billed_tokens"] == 0
    assert sorted(replay["verified_against_report"]) == ["commitments", "facts", "life_events", "memories", "situations"]
    # chat history: plan lines + recorded replies, in order, through the normal message flow
    rows = violetdb.fetch_all("SELECT role, content, created_at FROM messages ORDER BY id")
    expected = []
    for n in range(1, 6):
        expected += [("user", PLAN["rounds"][n - 1]["user"])] + [("assistant", t) for t in REPLIES[n]]
    assert [(r["role"], r["content"]) for r in rows[:len(expected)]] == expected
    # scenario clock, not the host's: the original start + 20 s per recorded call (4 calls per round)
    assert rows[0]["created_at"].startswith("2026-03-02T10:00")
    starts = [s["scenario_time"] for s in replay["scenario_clock"]["round_starts"]]
    assert starts[1].startswith("2026-03-02T10:01:20") and replay["scenario_clock"]["end_of_replay"].startswith("2026-03-02T10:06:40")
    assert report["scenario_clock"]["start"].startswith("2026-03-02T10:00")
    # profile: confirmed, the evidence is the value verbatim in that round's line
    facts = {f["fact_key"]: f for f in violetdb.fetch_all("SELECT * FROM user_profile_facts")}
    assert set(facts) == {"address_name", "study.school", "study.grade", "study.major", "residence.usual"}
    assert all(f["status"] == "active" and f["origin"] == "explicit" and f["evidence"] == f["value"] for f in facts.values())
    assert facts["study.major"]["source_message_id"] == violetdb.fetch_one(
        "SELECT id FROM messages WHERE content = ?", (PLAN["rounds"][1]["user"],))["id"]
    # event: content / kind / progress from the report, the date only because the content contains it
    event = violetdb.fetch_one("SELECT * FROM event_memories")
    assert (event["content"], event["kind"], event["progress"], event["status"]) == (EVENT, "event", "planned", "active")
    assert event["event_time"].startswith("2026-03-11")
    # Violet's life: one event, shared once (by the replayed round-5 reply), not duplicated by preload
    life = violetdb.fetch_all("SELECT content, share_count FROM life_events")
    assert [(e["content"], e["share_count"]) for e in life] == [("又往后读了几章《边城》", 1)]
    # labelled sources and what could not be restored
    assert any("identity.status" in d for d in replay["differences_vs_plan"])
    assert replay["not_restored"] and any("evidence" in x for x in replay["not_restored"])
    state = json.loads((workdir / ".violet-eval-state.json").read_text(encoding="utf-8"))
    assert state["replay"]["status"] == "complete"
    assert [(r["n"], r["mode"]) for r in state["rounds"]] == [(1, "replay"), (2, "replay"), (3, "replay"),
                                                             (4, "replay"), (5, "replay"), (6, "mock")]
    assert state["clock_end"].startswith("2026-03-02T10:0")
    shutil.rmtree(workdir)


def _replay_session(violetdb, workdir, *extra, budget=None):
    from tools.violet_test_chat import build_parser, open_session
    from tools.harness import ScenarioClock
    from tools.replay import load_replay
    args = build_parser().parse_args(["--workdir", str(workdir), *extra])
    session = open_session(args, input_fn=lambda prompt="": "yes", out=lambda s: None, db=violetdb)
    if budget is not None:
        session.recorder.budget = budget
    replay = load_replay(write_report(workdir.parent), PLAN, 6)
    session.use_clock(ScenarioClock(datetime.fromisoformat(replay["anchor"])))
    return session, replay


def test_replay_creates_no_real_client_and_costs_nothing(clean_env, violetdb, tmp_path, monkeypatch):
    import openai
    from tools.dialogue_eval import preload
    from tools.harness import Budget, MockLLM
    from tools.replay import run_replay
    built = []

    class FakeOpenAI(MockLLM):
        def __init__(self, **kwargs):
            built.append(kwargs)
            super().__init__()
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    clean_env.update({"VIOLET_TEST_ALLOW_REAL_API": "1", "VIOLET_TEST_API_KEY": "sk-test-replay-0123456789"})
    session, replay = _replay_session(violetdb, tmp_path / "violet-testchat-net", "--real", "--api-url",
                                      "https://t.example/v1", budget=Budget(max_calls_total=1, max_tokens_total=1))
    try:
        preload(session, PLAN)
        summary = run_replay(session, replay)
        assert built == [] and session.recorder.calls == [] and session.recorder.tokens == 0
        assert summary["offline_answers"] > 0 and session.recorder.stopped is None
        session.recorder.budget = Budget()
        session.say("好看吗", 6)                                        # the first live call creates the client
        assert built and session.recorder.calls
    finally:
        session.close()


def test_a_state_mismatch_stops_before_any_live_round(run_eval, violetdb, tmp_path, capsys):
    report = make_report()
    report["final_state"]["life_events"][0]["share_count"] = 2            # not what the pipeline reproduces
    workdir = tmp_path / "violet-testchat-mismatch"
    rc, out = run_eval("--replay-from", str(write_report(tmp_path, report)), "--rounds", "6", "--workdir",
                       str(workdir), "--keep")
    assert rc == 4 and out is None and "回放后的状态与真实报告不一致" in capsys.readouterr().out
    state = json.loads((workdir / ".violet-eval-state.json").read_text(encoding="utf-8"))
    assert state["replay"]["status"] == "failed" and state["rounds"] == []
    assert not violetdb.fetch_all("SELECT id FROM messages WHERE content = '好看吗'")      # round 6 never ran
    rc, _ = run_eval("--rounds", "6", "--workdir", str(workdir))
    assert rc == 2 and "没有成功完成" in capsys.readouterr().out
    shutil.rmtree(workdir)


def test_an_interrupted_replay_is_never_taken_for_a_finished_one(run_eval, tmp_path, monkeypatch, capsys):
    from tools import dialogue_eval
    monkeypatch.setattr(dialogue_eval, "run_replay", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    workdir = tmp_path / "violet-testchat-interrupted"
    with pytest.raises(KeyboardInterrupt):
        run_eval("--replay-from", str(write_report(tmp_path)), "--rounds", "6", "--workdir", str(workdir), "--keep")
    state = json.loads((workdir / ".violet-eval-state.json").read_text(encoding="utf-8"))
    assert state["replay"]["status"] == "failed"
    rc, _ = run_eval("--rounds", "6", "--workdir", str(workdir))
    assert rc == 2
    shutil.rmtree(workdir)
    # without --keep nothing is left behind at all
    other = tmp_path / "violet-testchat-gone"
    with pytest.raises(KeyboardInterrupt):
        run_eval("--replay-from", str(write_report(tmp_path)), "--rounds", "6", "--workdir", str(other))
    assert not other.exists()


# ---------------------------------------------------------------------------
# No double replay, no overwritten data, continuation
# ---------------------------------------------------------------------------

def test_existing_directories_are_never_replayed_again_or_overwritten(run_eval, violetdb, tmp_path, capsys):
    workdir = tmp_path / "violet-testchat-once"
    report_path = write_report(tmp_path)
    assert run_eval("--replay-from", str(report_path), "--rounds", "6", "--workdir", str(workdir), "--keep")[0] == 0
    state_before = (workdir / ".violet-eval-state.json").read_bytes()
    messages_before = len(violetdb.fetch_all("SELECT id FROM messages"))
    for argv, refusal in ((["--replay-from", str(report_path), "--rounds", "6"], "拒绝再次回放"),
                          (["--rounds", "6"], "已经完成"),                               # overlap
                          (["--rounds", "8"], "必须从第 7 轮开始"),                      # gap
                          (["--rounds", "7", "--real", "--api-url", "https://t.example/v1"],
                           "里有模拟回复的轮次")):                                         # real on top of mock rounds
        capsys.readouterr()
        rc, _ = run_eval(*argv, "--workdir", str(workdir))
        assert rc == 2 and refusal in capsys.readouterr().out, argv
    assert (workdir / ".violet-eval-state.json").read_bytes() == state_before
    assert len(violetdb.fetch_all("SELECT id FROM messages")) == messages_before
    rc, report = run_eval("--rounds", "7", "--workdir", str(workdir))          # the next round continues
    assert rc == 0 and report["continuation"]["mode"] == "continue"
    assert report["continuation"]["completed_rounds_before"] == [1, 2, 3, 4, 5, 6]
    assert report["scenario_clock"]["start"].startswith("2026-03-02T10:0")   # the scenario's time, not the host's
    assert [(e["content"], e["share_count"]) for e in violetdb.fetch_all("SELECT content, share_count FROM life_events")] \
        == [("又往后读了几章《边城》", 1)]
    assert workdir.exists()                                                  # not created by the second run: kept
    shutil.rmtree(workdir)


def test_history_without_a_state_file_is_refused(run_eval, tmp_path, capsys):
    from tools.replay import STATE_FILE
    workdir = tmp_path / "violet-testchat-foreign"
    workdir.mkdir()
    import sqlite3
    conn = sqlite3.connect(workdir / "violet-test.db")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO messages (content) VALUES ('别人的聊天记录')")
    conn.commit()
    conn.close()
    for argv in (["--rounds", "2"], ["--replay-from", str(write_report(tmp_path)), "--rounds", "6"]):
        rc, _ = run_eval(*argv, "--workdir", str(workdir))
        assert rc == 2
    assert not (workdir / STATE_FILE).exists()


# ---------------------------------------------------------------------------
# Rounds 9 and 10
# ---------------------------------------------------------------------------

def test_round9_busy_block_and_round10_release_after_a_replay(run_eval, tmp_path):
    from tools.dialogue_eval import summarize
    rc, report = run_eval("--replay-from", str(write_report(tmp_path)), "--rounds", "6-10", "--scripted-memory",
                          "--quiet-hours", "off")
    assert rc == 0
    by_n = {r["n"]: r for r in report["rounds"]}
    busy = by_n[9]["proactive"][0]
    assert busy["check"] == "blocked_busy" and busy["category"] == "busy" and busy["busy_saved"] is True
    assert busy["ok"] is True and busy["sent"] == [] and busy["busy_situation"]["value"] == "在开会"
    cleared = by_n[10]["proactive"][0]
    assert cleared["check"] == "busy_cleared" and cleared["ok"] is True
    assert by_n[10]["auto"]["context_checks"]["lacks"] == {"在开会": True}
    assert summarize(report)["failed_checks"] == []


def test_round9_fails_when_the_busy_state_was_not_saved(run_eval, tmp_path):
    from tools.dialogue_eval import summarize
    rc, report = run_eval("--replay-from", str(write_report(tmp_path)), "--rounds", "6-10", "--quiet-hours", "off")
    assert rc == 0                                                           # the mock memory model saves nothing
    busy = next(r for r in report["rounds"] if r["n"] == 9)["proactive"][0]
    assert busy["sent"] == [] and busy["ok"] is False and busy["busy_saved"] is False
    assert busy["category"] != "busy" and any("没有保存忙碌状态" in f for f in busy["failures"])
    for r in report["rounds"]:
        r["score"] = 2
    summary = summarize(report)
    assert summary["verdict"] == "fail" and summary["failed_checks"][0]["round"] == 9


def _busy_round(session, until="2h"):
    from tools.dialogue_eval import _proactive
    ids = {r["id"] for r in session.db.fetch_all("SELECT id FROM user_situations")}
    session.memory_script = [{"tool": "set_situation", "args": {"kind": "availability", "value": "在开会",
                                                               "evidence": "在开会", "until": until}}]
    session.say("我在开会，晚点聊", 9)
    session.memory_script = None
    return _proactive(session, "blocked_busy", ids)


def test_quiet_hours_are_not_mistaken_for_the_busy_rule(clean_env, violetdb, tmp_path):
    from tools.violet_test_chat import build_parser, open_session
    from tools.harness import ScenarioClock
    args = build_parser().parse_args(["--workdir", str(tmp_path / "violet-testchat-quiet")])   # quiet hours as configured
    session = open_session(args, out=lambda s: None, db=violetdb)
    try:
        session.use_clock(ScenarioClock(datetime.fromisoformat("2026-03-03T03:00:00+08:00")))   # inside 01:00-08:00
        result = _busy_round(session)
        assert result["busy_saved"] is True and result["category"] == "quiet_hours" and result["ok"] is False
        assert any("静默时段" in f for f in result["failures"])
    finally:
        session.close()


def test_busy_state_still_current_in_round10_fails(clean_env, violetdb, tmp_path):
    from tools.dialogue_eval import _busy_cleared
    from tools.violet_test_chat import build_parser, open_session
    args = build_parser().parse_args(["--workdir", str(tmp_path / "violet-testchat-long"), "--quiet-hours", "off"])
    session = open_session(args, out=lambda s: None, db=violetdb)
    try:
        assert _busy_round(session, until="8h")["ok"] is True
        session.advance(3)
        result = _busy_cleared(session, {"n": 10})
        assert result["ok"] is False and "仍然有效" in result["failures"][0]
    finally:
        session.close()


def test_an_old_busy_state_does_not_count_as_saved_in_round9(clean_env, violetdb, tmp_path):
    from tools.dialogue_eval import _proactive
    from tools.violet_test_chat import build_parser, open_session
    args = build_parser().parse_args(["--workdir", str(tmp_path / "violet-testchat-old-busy"), "--quiet-hours", "off"])
    session = open_session(args, out=lambda s: None, db=violetdb)
    try:
        assert _busy_round(session, until="8h")["ok"] is True                # an earlier busy round
        ids = {r["id"] for r in session.db.fetch_all("SELECT id FROM user_situations")}
        session.say("我在开会，晚点聊", 9)                                     # the memory model saves nothing now
        result = _proactive(session, "blocked_busy", ids)
        assert result["category"] == "busy" and result["busy_saved"] is False and result["ok"] is False
    finally:
        session.close()


def test_quiet_hours_off_is_never_quiet_at_any_scenario_time(clean_env, violetdb, tmp_path):
    """Phase 7 B.4: --quiet-hours off is an empty window, so no advance of the scenario clock can hit it."""
    from tools.violet_test_chat import build_parser, open_session
    from tools.harness import ScenarioClock
    workdir = tmp_path / "violet-testchat-window"
    session = open_session(build_parser().parse_args(["--workdir", str(workdir), "--quiet-hours", "off"]),
                           out=lambda s: None, db=violetdb)
    try:
        session.use_clock(ScenarioClock(datetime.fromisoformat("2026-03-02T10:00:00+08:00")))
        quiet = json.loads((workdir / "proactive.json").read_text(encoding="utf-8"))["quiet_hours"]
        assert quiet == {"start": "00:00", "end": "00:00"}
        session.scheduler.load_config()
        start = session.clock.now()
        for minutes in range(0, 24 * 60, 7):                                    # every time of day
            assert session.scheduler._in_quiet_hours(start + timedelta(minutes=minutes)) is False
    finally:
        session.close()


def test_a_replayed_reply_that_changes_is_refused_even_if_the_state_matches(run_eval, tmp_path, monkeypatch, capsys):
    from tools import harness
    original = harness.VioletTestSession._replay_response

    def altered(self, **kwargs):
        if harness._AGENT.get() == "ChatAgent" and (self.replay_reply or "").startswith("好呀"):
            return harness.MockLLM._completion("别的回复", str(kwargs.get("model")))
        return original(self, **kwargs)
    monkeypatch.setattr(harness.VioletTestSession, "_replay_response", altered)
    rc, _ = run_eval("--replay-from", str(write_report(tmp_path)), "--rounds", "6")
    assert rc == 4 and "第 1 轮回放后发出的消息与记录不一致" in capsys.readouterr().out


@pytest.mark.parametrize("reason,event,sent,error,expected", [
    ("not due yet", None, False, None, "not_triggered"),
    ("quiet hours", None, False, None, "quiet_hours"),
    ("the user said they are busy (在开会) until 10-13T10:06", None, False, None, "busy"),
    ("too soon after user message", None, False, None, "cooldown"),
    ("too soon after proactive message", None, False, None, "cooldown"),
    ("daily proactive limit reached", None, False, None, "policy"),
    ("due", {"status": "skipped"}, False, None, "declined"),
    ("due", {"status": "sent"}, True, None, "sent"),
    ("due", {"status": "failed"}, False, None, "error"),
    (None, None, False, "RuntimeError", "error"),
])
def test_proactive_outcomes_are_classified(reason, event, sent, error, expected):
    from tools.dialogue_eval import PROACTIVE_CATEGORIES, classify_proactive
    category = classify_proactive(reason, event, sent, error)
    assert category == expected and category in PROACTIVE_CATEGORIES


# ---------------------------------------------------------------------------
# Windows: a kept directory survives the process and is continued by another one
# ---------------------------------------------------------------------------

def test_kept_directory_is_continued_by_a_later_process(tmp_path):
    report_path = write_report(tmp_path)
    workdir = tmp_path / "violet-testchat-process"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIOLET_", "LLM_", "MCP_"))}

    def run(*argv):
        proc = subprocess.run([sys.executable, "-m", "tools.dialogue_eval", *argv, "--workdir", str(workdir),
                               "--output-dir", str(tmp_path / f"out-{len(list(tmp_path.iterdir()))}")],
                              cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=300, env=env)
        return proc.returncode, proc.stdout + proc.stderr

    rc, out = run("--replay-from", str(report_path), "--rounds", "6", "--keep")
    assert rc == 0, out[-1500:]
    assert workdir.exists() and "测试数据目录已保留" in out
    rc, out = run("--rounds", "7")
    assert rc == 0, out[-1500:]
    assert workdir.exists() and "不是本次创建的，未删除" in out
    state = json.loads((workdir / ".violet-eval-state.json").read_text(encoding="utf-8"))
    assert [r["n"] for r in state["rounds"]] == [1, 2, 3, 4, 5, 6, 7]
    from tools.replay import count_messages
    assert count_messages(workdir / "violet-test.db") == 2 * 7 + sum(len(v) - 1 for v in REPLIES.values())
    rc, out = run("--rounds", "7")
    assert rc == 2 and "已经完成" in out
    shutil.rmtree(workdir)                                                   # the processes closed the database
    assert not workdir.exists()
