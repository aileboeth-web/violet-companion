"""Phase 7 B.6.2: rebuild round 14 offline on a COPY of the directory that ended after round 13, from the
real report that continued exactly that directory, so only round 15 has to be run with the real model.
All offline: fictional data shaped like the kept test directory and report 20261013-051915."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
DEFENSE = "用户将于2026-10-21参加天文观测课程答辩，并对此感到有些紧张。"
INTERVIEW = "用户将于2026年10月14日参加实习面试，面试前紧张到睡不着。"
DONE_TEXT = "用户的天文观测课程答辩已结束，过程挺顺利。"
USER_14 = "我明天面试实习，现在紧张得睡不着，怎么办啊"
USER_15 = "答辩结束了，挺顺利的"
VIOLET_14 = ["小满，明天要面试，紧张很正常", "先别逼自己立刻睡着，慢慢呼气", "把明天要带的东西写下来", "先把手机放下，好不好"]
CLOCK_END_13 = "2026-10-13T09:50:31.031251+08:00"
START_15 = "2026-10-13T09:52:34.374251+08:00"


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

    def run(*argv, input_fn=input):
        counter["n"] += 1
        out = tmp_path / f"out{counter['n']}"
        rc = dialogue_eval.main([*argv, "--output-dir", str(out)], input_fn=input_fn)
        report = json.loads((out / "report.json").read_text(encoding="utf-8")) if (out / "report.json").exists() else None
        return rc, report
    return run


def _plan_meta():
    from tools.replay import load_plan
    return load_plan(PROJECT / "tests" / "samples" / "acceptance_dialogue_30.json")[1]


def _snapshot(violetdb, target: Path) -> None:
    src = sqlite3.connect(violetdb.db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


@pytest.fixture()
def seed(violetdb, tmp_path):
    """A kept directory after round 13 (rounds 1-10 replayed, 11-13 real) and the matching database."""
    for n in range(1, 14):
        violetdb.add_message("user", "incoming", "text", f"第 {n} 轮的话", created_at=f"2026-10-13T09:{30 + n:02d}:00+08:00")
        violetdb.add_message("assistant", "outgoing", "text", f"第 {n} 轮的回复", created_at=f"2026-10-13T09:{30 + n:02d}:01+08:00")
    defense = violetdb.add_event_memory(DEFENSE, kind="event", progress="planned", event_time="2026-10-21T19:23:00+08:00",
                                     valid_until="2026-11-04T19:23:00+08:00")
    seq = violetdb.fetch_one("SELECT seq FROM sqlite_sequence WHERE name = 'event_memories'")["seq"]
    meta = _plan_meta()
    state = {
        "version": 1, "plan": str(PROJECT / "tests" / "samples" / "acceptance_dialogue_30.json"),
        "plan_sha256": meta["base_sha256"],
        "plan_revisions": {"2": {"file": meta["revision_file"], "sha256": meta["revision_sha256"], "changed_rounds": [15]}},
        "replay": {"source": ["r1.json", "r2.json"], "sha256": ["a", "b"], "rounds": list(range(1, 11)), "status": "complete"},
        "rounds": [{"n": n, "mode": "replay" if n <= 10 else "real", "scenario_time": f"2026-10-13T09:{30 + n:02d}:00+08:00"}
                   for n in range(1, 14)],
        "clock_end": CLOCK_END_13,
    }
    state["rounds"][3]["scenario_time"] = "2026-10-13T09:38:02.325251+08:00"     # round 4: the defense was "下周三"
    d = tmp_path / "violet-testchat-before14"
    d.mkdir()
    (d / ".violet-eval-state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    _snapshot(violetdb, d / "violet-test.db")
    return {"dir": d, "defense": defense, "next_id": seq + 1, "state": state}


def make_report(seed, **edits) -> dict:
    defense, new_id = seed["defense"], seed["next_id"]
    meta = _plan_meta()

    def call(agent, results=(), latency=10000):
        return {"agent": agent, "model": "gpt-6-luna", "latency_ms": latency, "tool_calls": [r["tool"] for r in results],
                "tool_results": list(results), "ok": True}
    r14 = [call("IntentAgent", [{"tool": "query_memory", "memories_count": 0}]), call("IntentAgent"), call("ChatAgent"),
           call("MemoryAgent", [{"tool": "query_memory", "memories_count": 0}]),
           call("MemoryAgent", [{"tool": "save_memory", "ok": False, "action": "needs_decision", "candidates_count": 1,
                                 "candidates_ids": [defense]}]),
           call("MemoryAgent", [{"tool": "save_memory", "ok": True, "action": "created", "memory_id": new_id}]),
           call("MemoryAgent", [{"tool": "memory_done", "ok": True, "action": "done_requested"}])]
    r15 = [call("IntentAgent", [{"tool": "query_memory", "memories_count": 0}]), call("IntentAgent"), call("ChatAgent"),
           call("MemoryAgent", [{"tool": "query_memory", "memories_count": 0}]),
           call("MemoryAgent", [{"tool": "save_memory", "ok": True, "action": "created", "memory_id": new_id + 1}]),
           call("MemoryAgent", [{"tool": "memory_done", "ok": True, "action": "done_requested"}])]
    report = {
        "real": True, "models": {"memory": "gpt-6-luna"},
        "rounds": [
            {"n": 14, "user": USER_14, "planned_user": USER_14, "violet": list(VIOLET_14), "calls": len(r14), "call_details": r14,
             "proactive": [], "scenario_time": CLOCK_END_13, "time_advance": None, "reply_latency_ms": 57203},
            {"n": 15, "user": USER_15, "planned_user": USER_15, "violet": ["太好了小满"], "calls": len(r15), "call_details": r15,
             "proactive": [], "scenario_time": "2026-10-22T10:00:00+08:00",
             "time_advance": {"from": START_15, "to": "2026-10-22T10:00:00+08:00", "hours": 216.124,
                              "events_changed": [{"id": defense, "before": ["planned", "active"], "after": ["unknown", "active"]},
                                                 {"id": new_id, "before": ["planned", "active"], "after": ["unknown", "active"]}]}},
        ],
        "final_state": {"memories": [
            {"id": defense, "content": DEFENSE, "kind": "event", "progress": "unknown", "status": "active"},
            {"id": new_id, "content": INTERVIEW, "kind": "event", "progress": "unknown", "status": "active"},
            {"id": new_id + 1, "content": DONE_TEXT, "kind": "event", "progress": "done", "status": "active"}]},
        "continuation": {"mode": "continue", "workdir": "elsewhere", "completed_rounds_before": list(range(1, 14))},
        "replay": None,
        "plan": {k: meta[k] for k in ("plan", "base_sha256", "revision", "revision_file", "revision_sha256", "changed_rounds")},
    }
    for edit in edits.values():
        edit(report)
    return report


def write(tmp_path, report, name="report15.json"):
    path = tmp_path / name
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def replay_args(seed, report_path, sha, workdir, *extra):
    return ["--seed-from", str(seed["dir"]), "--continue-replay", str(report_path), "--continue-replay-sha256", sha,
            "--rounds", "15", "--workdir", str(workdir), *extra]


def _files_sha(d: Path) -> dict:
    return {n: hashlib.sha256((d / n).read_bytes()).hexdigest() for n in ("violet-test.db", ".violet-eval-state.json")}


def _times(violetdb):
    return [(r["id"], r["created_at"]) for r in violetdb.fetch_all("SELECT id, created_at FROM messages ORDER BY id")]


# ---------------------------------------------------------------------------
# The replay itself
# ---------------------------------------------------------------------------

def test_round14_is_rebuilt_and_only_round15_is_left(run_eval, violetdb, seed, tmp_path):
    report_path, sha = write(tmp_path, make_report(seed))
    seed_files, history = _files_sha(seed["dir"]), _times(violetdb)
    counts = {t: violetdb.fetch_one(f"SELECT COUNT(*) AS c FROM {t}")["c"] for t in
              ("user_profile_facts", "user_situations", "commitments", "life_events", "profile_questions")}
    base = tmp_path / "violet-testchat-b62-base"
    rc, report = run_eval(*replay_args(seed, report_path, sha, base, "--replay-only", "--keep"))
    assert rc == 0 and report["rounds"] == []
    summary = report["replay"]
    assert summary["mode"] == "continuation_replay" and summary["rounds"] == [14]
    assert summary["billed_calls"] == 0 and summary["offline_answers"] > 0
    assert summary["checked"][0]["memory_results"] == [
        {"action": "needs_decision", "candidates_ids": [seed["defense"]]}, {"action": "created", "memory_id": seed["next_id"]}]
    assert summary["scenario_clock"]["end"] == START_15 and summary["not_restored"]
    # chat and memory
    rows = violetdb.fetch_all("SELECT role, content FROM messages ORDER BY id")
    assert [(r["role"], r["content"]) for r in rows[-5:]] == [("user", USER_14)] + [("assistant", t) for t in VIOLET_14]
    assert len(rows) == 26 + 5 and _times(violetdb)[:26] == history                  # history untouched
    interview = violetdb.get_event_memory(seed["next_id"])
    assert (interview["content"], interview["progress"], interview["status"]) == (INTERVIEW, "planned", "active")
    assert interview["event_time"].startswith("2026-10-14")
    defense = violetdb.get_event_memory(seed["defense"])
    assert (defense["content"], defense["progress"], defense["event_time"]) == (DEFENSE, "planned", "2026-10-21T19:23:00+08:00")
    assert violetdb.fetch_one("SELECT COUNT(*) AS c FROM event_memories")["c"] == 2
    assert counts == {t: violetdb.fetch_one(f"SELECT COUNT(*) AS c FROM {t}")["c"] for t in counts}
    assert not violetdb.fetch_all("SELECT id FROM memory_jobs WHERE status != 'done'")
    # state: round 14 is a replay, the directory continues at round 15
    state = json.loads((base / ".violet-eval-state.json").read_text(encoding="utf-8"))
    assert [(r["n"], r["mode"]) for r in state["rounds"]][-2:] == [(13, "real"), (14, "replay")]
    assert state["clock_end"] == START_15
    assert state["continuation_replays"][0]["status"] == "complete"
    assert state["continuation_replays"][0]["seeded_from"]["sha256"] == seed_files
    assert _files_sha(seed["dir"]) == seed_files                                 # the source was only read
    # the next run copies the prepared directory and runs round 15 at the revised scenario time
    rc, live = run_eval("--seed-from", str(base), "--rounds", "15", "--workdir", str(tmp_path / "violet-testchat-r15"))
    assert rc == 0 and [r["n"] for r in live["rounds"]] == [15]
    r15 = live["rounds"][0]
    # the clock runs on with real time after the jump (the original run recorded 10:00:00.016)
    assert abs((datetime.fromisoformat(r15["scenario_time"]) -
                datetime.fromisoformat("2026-10-22T10:00:00+08:00")).total_seconds()) < 1
    assert abs((datetime.fromisoformat(r15["time_advance"]["from"]) - datetime.fromisoformat(START_15)).total_seconds()) < 5
    assert {c["id"]: c["after"][0] for c in r15["time_advance"]["events_changed"]} == {
        seed["defense"]: "unknown", seed["next_id"]: "unknown"}
    assert live["continuation"]["completed_rounds_before"] == list(range(1, 15))
    assert not (tmp_path / "violet-testchat-r15").exists() and (base / ".violet-eval-state.json").exists()


def test_the_replay_never_creates_a_real_client(run_eval, clean_env, violetdb, seed, tmp_path, monkeypatch):
    import openai
    from tools.harness import MockLLM
    built = []

    class FakeOpenAI(MockLLM):
        def __init__(self, **kwargs):
            built.append(kwargs)
            super().__init__()
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    clean_env.update({"VIOLET_TEST_ALLOW_REAL_API": "1", "VIOLET_TEST_API_KEY": "sk-test-b62-0123456789"})
    report_path, sha = write(tmp_path, make_report(seed))
    rc, report = run_eval(*replay_args(seed, report_path, sha, tmp_path / "violet-testchat-real", "--replay-only", "--real",
                                       "--api-url", "https://t.example/v1", "--yes", "--max-tokens", "1"))
    assert rc == 0 and built == [] and report["replay"]["billed_calls"] == 0 and report["call_summary"]["calls"] == 0


# ---------------------------------------------------------------------------
# Refusals before anything is created
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("edit,message", [
    (lambda r: r["rounds"][0].update(user="我明天面试，紧张"), "用户原话与验收剧本不一致"),
    (lambda r: r["continuation"].update(completed_rounds_before=list(range(1, 13))), "不是接在这个目录"),
    (lambda r: r["rounds"][0].update(scenario_time="2026-10-13T09:50:40+08:00"), "不是同一条时间线"),
    (lambda r: r["plan"].update(revision_sha256="0" * 64), "剧本修正版与当前的不同"),
    (lambda r: r["plan"].update(base_sha256="0" * 64), "剧本哈希不一致"),
    (lambda r: r.update(real=False), "真实模型测试的报告"),
    (lambda r: r["rounds"][0]["call_details"][4]["tool_results"].__setitem__(0, {
        "tool": "remember_profile_fact", "ok": True, "action": "created", "key": "x"}), "不支持的写入"),
    (lambda r: r["rounds"][0]["call_details"].pop(5), "调用明细不完整"),
    (lambda r: r["rounds"][1]["call_details"][4]["tool_results"].__setitem__(0, {
        "tool": "update_event_progress", "ok": True, "action": "progress_updated", "memory_id": r["rounds"][0][
            "call_details"][5]["tool_results"][0]["memory_id"]}), "之后的轮次里被改动过"),
    (lambda r: r["rounds"].pop(0), "没有第 [14] 轮"),
])
def test_incompatible_reports_are_refused(run_eval, seed, tmp_path, capsys, edit, message):
    report_path, sha = write(tmp_path, make_report(seed, e=edit))          # a consistent hash: only the content is wrong
    target = tmp_path / "violet-testchat-refused"
    rc, _ = run_eval(*replay_args(seed, report_path, sha, target))
    assert rc == 2 and message in capsys.readouterr().out and not target.exists()


def test_a_modified_report_or_a_missing_hash_is_refused(run_eval, seed, tmp_path, capsys):
    report_path, sha = write(tmp_path, make_report(seed))
    text = report_path.read_text(encoding="utf-8").replace("先把手机放下", "先把手机收起来")
    report_path.write_text(text, encoding="utf-8")
    target = tmp_path / "violet-testchat-modified"
    assert run_eval(*replay_args(seed, report_path, sha, target))[0] == 2
    assert "哈希与给定的不一致" in capsys.readouterr().out and not target.exists()
    args = replay_args(seed, report_path, sha, target)
    args[args.index("--continue-replay-sha256"):args.index("--continue-replay-sha256") + 2] = []
    assert run_eval(*args)[0] == 2 and "必须给出来源报告的 SHA-256" in capsys.readouterr().out


def test_a_directory_that_already_has_round14_is_not_replayed_again(run_eval, seed, tmp_path, capsys):
    state = dict(seed["state"])
    state["rounds"] = state["rounds"] + [{"n": 14, "mode": "real", "scenario_time": CLOCK_END_13}]
    (seed["dir"] / ".violet-eval-state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    report_path, sha = write(tmp_path, make_report(seed))
    assert run_eval(*replay_args(seed, report_path, sha, tmp_path / "violet-testchat-again"))[0] == 2
    assert "没有需要回放的轮次" in capsys.readouterr().out


@pytest.mark.parametrize("argv,message", [
    (lambda s, rep, sha, t: ["--continue-replay", str(rep), "--continue-replay-sha256", sha, "--rounds", "15",
                             "--workdir", str(s["dir"])], "只在 --seed-from 复制出的新目录里进行"),
    (lambda s, rep, sha, t: replay_args(s, rep, sha, s["dir"]), "还不存在的 --workdir"),
    (lambda s, rep, sha, t: ["--seed-from", str(s["dir"]), "--rounds", "14", "--replay-only", "--workdir", str(t)],
     "--replay-only 需要"),
    (lambda s, rep, sha, t: replay_args(s, rep, sha, t, "--replay-from", str(rep)), "不能和 --seed-from"),
])
def test_unsafe_combinations_are_refused(run_eval, seed, tmp_path, capsys, argv, message):
    report_path, sha = write(tmp_path, make_report(seed))
    before = _files_sha(seed["dir"])
    target = tmp_path / "violet-testchat-combo"
    assert run_eval(*argv(seed, report_path, sha, target))[0] == 2
    assert message in capsys.readouterr().out and not target.exists() and _files_sha(seed["dir"]) == before


# ---------------------------------------------------------------------------
# Failures during the replay: nothing billed, the directory can never be continued
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("break_db,message", [
    (lambda db, s: db.execute("UPDATE event_memories SET content = ? WHERE id = ?", ("用户下周三有答辩", s["defense"])),
     "与报告记录的起点不一致"),
    (lambda db, s: db.add_event_memory("用户周末要搬家", kind="event", progress="planned"), "下一条事件记忆的 id"),
    (lambda db, s: db.execute("UPDATE event_memories SET progress = 'unknown' WHERE id = ?", (s["defense"],)),
     "与报告记录的起点不一致"),
    (lambda db, s: db.execute("INSERT INTO memory_jobs (turn_id, user_message, status, attempts, created_at, updated_at) "
                              "VALUES ('t-13', '第 13 轮的话', 'failed', 3, ?, ?)", (CLOCK_END_13, CLOCK_END_13)),
     "还有没完成的记忆任务"),
])
def test_a_database_that_does_not_match_the_report_stops_the_replay(run_eval, violetdb, seed, tmp_path, capsys,
                                                                    break_db, message):
    break_db(violetdb, seed)
    report_path, sha = write(tmp_path, make_report(seed))
    target = tmp_path / "violet-testchat-broken"
    rc, report = run_eval(*replay_args(seed, report_path, sha, target, "--keep"))
    assert rc == 4 and report is None and message in capsys.readouterr().out
    state = json.loads((target / ".violet-eval-state.json").read_text(encoding="utf-8"))
    assert state["continuation_replays"][0]["status"] == "failed" and [r["n"] for r in state["rounds"]][-1] == 13
    assert not violetdb.fetch_all("SELECT id FROM messages WHERE content = ?", (USER_15,))
    rc, _ = run_eval("--seed-from", str(target), "--rounds", "14", "--workdir", str(tmp_path / "violet-testchat-next"))
    assert rc == 2 and "续跑回放没有成功完成" in capsys.readouterr().out


def test_a_replayed_write_that_differs_from_the_record_stops_the_replay(run_eval, violetdb, seed, tmp_path, capsys):
    """The report says the save first asked for a decision; if the current code would not ask, the
    replay does not match the original round and must not pretend it does."""
    report = make_report(seed)
    report["rounds"][0]["call_details"][4]["tool_results"][0]["candidates_ids"] = [seed["defense"], 999]
    report_path, sha = write(tmp_path, report)
    rc, _ = run_eval(*replay_args(seed, report_path, sha, tmp_path / "violet-testchat-diff"))
    assert rc in (2, 4) and ("相似候选 999" in capsys.readouterr().out)


def test_an_unexpected_record_written_during_the_replay_stops_it(run_eval, violetdb, seed, tmp_path, capsys, monkeypatch):
    """Nothing may be invented: a record the report does not account for (here a commitment written by
    the pipeline during the replayed round) fails the replay."""
    from tools.harness import VioletTestSession
    original = VioletTestSession.say

    def say_and_add(self, text, round_no=None, timeout=600):
        result = original(self, text, round_no, timeout)
        self.db.add_commitment("明天提醒我面试", "2026-10-14T08:00:00+08:00", kind="reminder", evidence="提醒我")
        return result
    monkeypatch.setattr(VioletTestSession, "say", say_and_add)
    report_path, sha = write(tmp_path, make_report(seed))
    rc, _ = run_eval(*replay_args(seed, report_path, sha, tmp_path / "violet-testchat-extra"))
    assert rc == 4 and "记录数与预期不一致" in capsys.readouterr().out


def test_the_current_code_must_reproduce_the_recorded_memory_results(run_eval, violetdb, seed, tmp_path, capsys):
    """Without the recorded needs_decision step the replayed save is asked for a decision (B.6.1 guard)
    and therefore does not reproduce the record: stop, never fake the round."""
    report = make_report(seed)
    report["rounds"][0]["call_details"].pop(4)
    report["rounds"][0]["calls"] -= 1
    report_path, sha = write(tmp_path, report)
    target = tmp_path / "violet-testchat-mismatch"
    rc, _ = run_eval(*replay_args(seed, report_path, sha, target, "--keep"))
    assert rc == 4 and "记忆写入与记录不一致" in capsys.readouterr().out
    assert json.loads((target / ".violet-eval-state.json").read_text(encoding="utf-8"))["continuation_replays"][0]["status"] == "failed"
    assert violetdb.fetch_one("SELECT COUNT(*) AS c FROM event_memories")["c"] == 1


# ---------------------------------------------------------------------------
# Round 15 on the rebuilt state: both tool choices of the model (B.6.1 guard), offline
# ---------------------------------------------------------------------------

@pytest.fixture()
def rebuilt(run_eval, violetdb, seed, tmp_path):
    report_path, sha = write(tmp_path, make_report(seed))
    assert run_eval(*replay_args(seed, report_path, sha, tmp_path / "violet-testchat-rebuilt", "--replay-only"))[0] == 0
    from tools.harness import ScenarioClock, install_clock, uninstall_clock
    saved = install_clock(ScenarioClock(datetime.fromisoformat("2026-10-22T10:00:32+08:00")))
    violetdb.refresh_event_states()                                    # round 15's time jump
    yield seed
    uninstall_clock(saved)


def _memory_tools():
    from ai.agents import MemoryAgent
    from ai.agents.base_agent import _CALL_CONTEXT
    return MemoryAgent(), _CALL_CONTEXT


@pytest.mark.parametrize("path", ["A", "B"])
def test_round15_both_tool_choices_end_with_one_done_defense(rebuilt, violetdb, monkeypatch, path):
    from tests.fakes import FakeLLM, default_responders, install_fake_llm
    install_fake_llm(monkeypatch, FakeLLM(default_responders()))
    agent, ctx = _memory_tools()
    token = ctx.set({"turn_id": f"t15-{path}", "user_message": USER_15})
    try:
        if path == "B":
            wrong = json.loads(agent._tool_save_memory({"summary": DONE_TEXT, "category": "event", "kind": "event",
                                                        "progress": "done", "new_event": True}))
            assert wrong["action"] == "needs_decision"
            assert [c["id"] for c in wrong["candidates"]] == [rebuilt["defense"]]       # the interview is no candidate
        done = json.loads(agent._tool_update_event_progress({"memory_id": rebuilt["defense"], "progress": "done",
                                                             "summary": DONE_TEXT, "reason": USER_15}))
    finally:
        ctx.reset(token)
    assert done["action"] == "progress_updated"
    rows = violetdb.fetch_all("SELECT id, content, progress, event_time FROM event_memories ORDER BY id")
    assert [(r["id"], r["progress"]) for r in rows] == [(rebuilt["defense"], "done"), (rebuilt["next_id"], "unknown")]
    assert rows[0]["event_time"] == "2026-10-21T19:23:00+08:00" and rows[1]["content"] == INTERVIEW
    revisions = violetdb.list_memory_revisions("event_memories", rebuilt["defense"])
    assert (revisions[-1]["old_progress"], revisions[-1]["new_progress"], revisions[-1]["source"]) == ("unknown", "done", "user")
    assert violetdb.ended_event_contradictions() == []


# ---------------------------------------------------------------------------
# Seeding a directory
# ---------------------------------------------------------------------------

def test_inspecting_a_kept_directory_creates_no_files_in_it(tmp_path):
    """Found in the drill: a read-only sqlite open of the seed's WAL database created empty -wal / -shm
    files in the kept directory. Inspection must leave the directory exactly as it was."""
    from tools.replay import count_messages
    db = tmp_path / "violet-test.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO messages (content) VALUES ('a'), ('b')")
    conn.commit()
    conn.close()
    before = sorted(p.name for p in tmp_path.iterdir())
    assert before == ["violet-test.db"] and count_messages(db) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_the_seed_directory_gets_no_new_files(run_eval, seed, tmp_path):
    report_path, sha = write(tmp_path, make_report(seed))
    before = sorted(p.name for p in seed["dir"].iterdir())
    assert run_eval(*replay_args(seed, report_path, sha, tmp_path / "violet-testchat-nofiles", "--replay-only"))[0] == 0
    assert sorted(p.name for p in seed["dir"].iterdir()) == before


def test_seed_copies_only_database_and_state_and_leaves_the_source_alone(tmp_path):
    from tools.harness import IsolationError, seed_workdir
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir(), dst.mkdir()
    (src / "violet-test.db").write_bytes(b"db")
    (src / ".violet-eval-state.json").write_text("{}", encoding="utf-8")
    (src / "violet-test.db-shm").write_bytes(b"x")
    (src / "violet-test.db-wal").write_bytes(b"")
    (src / "other.txt").write_text("not copied", encoding="utf-8")
    result = seed_workdir(src, dst)
    assert sorted(p.name for p in dst.iterdir()) == [".violet-eval-state.json", "violet-test.db"]
    assert result["empty_wal_files_not_copied"] == ["violet-test.db-wal", "violet-test.db-shm"]
    assert (src / "violet-test.db").read_bytes() == b"db"
    with pytest.raises(IsolationError, match="拒绝覆盖"):
        seed_workdir(src, dst)
    (src / "violet-test.db-wal").write_bytes(b"uncommitted")
    with pytest.raises(IsolationError, match="未合并的写入日志"):
        seed_workdir(src, tmp_path / "dst2")
    (src / ".violet-eval-state.json").unlink()
    with pytest.raises(IsolationError, match="缺少"):
        seed_workdir(src, tmp_path / "dst3")
    with pytest.raises(IsolationError, match="same"):
        seed_workdir(dst, dst)
