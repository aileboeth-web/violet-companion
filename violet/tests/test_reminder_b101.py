"""Phase 7 B.10.1: an explicit "提醒我…" request is settled before the reply (the reply tells the truth about it),
memory jobs that did not finish are reported, the token budget keeps a reserve for the next call, and the
round-28 scheduler check tells a due reminder apart from ordinary proactive chat. Offline, fictional data."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tests.fakes import FakeLLM, completion, default_responders, install_fake_llm, intent_json
from tests.test_checks_b91 import clean_env, run_eval  # noqa: F401  (fixtures)
from tests.test_phase2_scenarios import make_world, say, script, tool  # noqa: F401  (fixtures / helpers)
from tests.test_test_tools import session_factory, workdir  # noqa: F401  (fixtures)

USER_26 = "提醒我明天早上八点交作业"
NOW = (2026, 10, 22, 16, 15)


def _now():
    from ai.settings import local_timezone
    return datetime(*NOW, tzinfo=local_timezone())


# ---------------------------------------------------------------------------
# Reading the request (pure)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,content,due", [
    (USER_26, "交作业", "2026-10-23T08:00:00+08:00"),
    ("明天早上八点要交作业，记得提醒我", "交作业", "2026-10-23T08:00:00+08:00"),
    ("能提醒我明天早上八点交作业吗？", "交作业", "2026-10-23T08:00:00+08:00"),
    ("明晚7点半提醒我给妈妈打电话", "给妈妈打电话", "2026-10-23T19:30:00+08:00"),
    ("提醒我明天下午三点一刻去取快递", "取快递", "2026-10-23T15:15:00+08:00"),
    ("提醒我明天8:30开会", "开会", "2026-10-23T08:30:00+08:00"),
    ("提醒我明天十点半交报告", "交报告", "2026-10-23T10:30:00+08:00"),
    ("今晚提醒我吃药", "吃药", "2026-10-22T20:00:00+08:00"),
    ("提醒我后天交作业", "交作业", "2026-10-24T09:00:00+08:00"),          # no clock: the product's default time
    ("提醒我明天中午一点吃饭", "吃饭", "2026-10-23T13:00:00+08:00"),
])
def test_explicit_requests_are_read_exactly(violetdb, text, content, due):
    from ai.memory_model import parse_reminder_request
    r = parse_reminder_request(text, _now())
    assert (r["requested"], r["ok"], r["content"], r["due_at"]) == (True, True, content, due)
    assert r["evidence"] and r["evidence"] in text


@pytest.mark.parametrize("text,reason", [
    ("提醒我八点交作业", "no_date"),
    ("提醒我周五早上八点交作业", "no_date"),        # a day the parser cannot read: never claimed as "not said"
    ("提醒我明天", "no_content"),
    ("今天早上八点提醒我交作业", "time_passed"),
    ("提醒我明天晚上十二点睡觉", "ambiguous_time"),
    ("提醒我明天早上八点或者后天九点交作业", "ambiguous_time"),
    ("提醒我明天早上八点交作业，再提醒我后天开会", "several_requests"),
    ("提醒我明天早上八点把那份关于天文观测课程的期末报告和实验数据一起交给老师", "too_long"),
])
def test_unclear_requests_are_owned_but_never_set(violetdb, text, reason):
    from ai.memory_model import parse_reminder_request
    r = parse_reminder_request(text, _now())
    assert (r["requested"], r["ok"], r["reason"]) == (True, False, reason)


@pytest.mark.parametrize("text", [
    "不用提醒我了", "别提醒我明天交作业", "你昨天提醒我了吗", "你怎么没提醒我", "我明天早上八点交作业",
    "明天早上八点催我交作业",                # other explicit words stay with MemoryAgent, as before
])
def test_other_messages_are_not_settled_before_the_reply(violetdb, text):
    from ai.memory_model import parse_reminder_request
    assert parse_reminder_request(text, _now())["requested"] is False


# ---------------------------------------------------------------------------
# The pipeline: saved before the reply, the reply is told the real result
# ---------------------------------------------------------------------------

@pytest.fixture()
def agents(monkeypatch, violetdb):
    fake = install_fake_llm(monkeypatch, FakeLLM(default_responders("好，明早八点叫你交作业")))
    fake.responders["fake-intent"] = lambda kw: intent_json("task", task={"exists": True, "name": "提醒交作业",
                                                                         "description": "提醒用户明天早上八点交作业"})
    from ai.agents import ChatAgent, IntentAgent, MemoryAgent, PerformerAgent
    return fake, {"intent": IntentAgent(), "chat": ChatAgent(), "memory": MemoryAgent(), "performer": PerformerAgent()}


def _run(a, text=USER_26, turn_id="turn-26"):
    from ai.chat import run_pipeline
    return run_pipeline(text, a["intent"], a["chat"], a["performer"], a["memory"], defer_memory=True, turn_id=turn_id)


def _chat_system(fake):
    call = [c for c in fake.calls if c.get("model") == "fake-chat"][-1]
    return "\n".join(m.get("content") or "" for m in call["messages"] if m["role"] == "system")


def _performer_calls(fake):
    return [c for c in fake.calls if c.get("model") == "fake-performer"]


def test_round26_reminder_is_saved_before_the_reply_and_the_reply_is_told(agents, violetdb):
    fake, a = agents
    result = _run(a)
    rows = violetdb.fetch_all("SELECT * FROM commitments")
    assert len(rows) == 1
    row = rows[0]
    assert (row["content"], row["status"], row["evidence"], row["source_turn_id"]) == ("交作业", "pending", USER_26, "turn-26")
    assert row["due_at"][11:16] == "08:00"
    assert result["reminder"]["status"] == "set" and result["reminder"]["commitment_id"] == row["id"]
    system = _chat_system(fake)
    assert "提醒已经保存好了" in system and "“交作业”" in system and "08:00" in system
    assert "没有设置成功" not in system
    # the Performer has no reminder tool: it is not asked, and no failed task is left behind
    assert _performer_calls(fake) == [] and violetdb.fetch_all("SELECT * FROM task_sessions") == []


def test_an_unclear_request_is_not_set_and_the_reply_must_not_claim_it(agents, violetdb):
    fake, a = agents
    result = _run(a, "提醒我八点交作业")
    assert violetdb.fetch_all("SELECT * FROM commitments") == []
    assert result["reminder"]["status"] == "not_set" and result["reminder"]["reason"] == "no_date"
    system = _chat_system(fake)
    assert "没有设置成功" in system and "没能确定是哪一天" in system and "保存好了" not in system


def test_a_storage_failure_is_reported_as_not_set(agents, violetdb, monkeypatch):
    fake, a = agents
    from ai.chat import violetdb as pipeline_db

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(pipeline_db, "add_commitment", broken)
    result = _run(a)
    assert result["reminder"]["status"] == "not_set" and result["reminder"]["reason"] == "storage_error"
    assert violetdb.fetch_all("SELECT * FROM commitments") == []
    system = _chat_system(fake)
    assert "没有设置成功" in system and "保存的时候出错了" in system and "保存好了" not in system


def test_the_same_request_never_makes_two_reminders(agents, violetdb):
    fake, a = agents
    first = _run(a)
    again = _run(a)                                    # the same turn processed twice (crash recovery)
    later = _run(a, turn_id="turn-27")                 # the same words in a later turn
    assert len(violetdb.fetch_all("SELECT * FROM commitments")) == 1
    assert (first["reminder"]["reason"], again["reminder"]["reason"]) == ("created", "already_saved")
    assert later["reminder"]["status"] == "already_pending"
    assert later["reminder"]["commitment_id"] == first["reminder"]["commitment_id"]
    assert "之前已经设好了" in _chat_system(fake)


def test_other_messages_keep_their_route_and_get_no_note(agents, violetdb, monkeypatch):
    from tests.fake_mcp import campus_server, use
    use(monkeypatch, campus_server(agents=("performer",)))   # phase 9.2: a task needs a tool that could carry it out
    fake, a = agents
    result = _run(a, "我明天早上八点交作业")
    assert result["reminder"] is None and violetdb.fetch_all("SELECT * FROM commitments") == []
    assert _performer_calls(fake) and "【提醒】" not in _chat_system(fake)


def test_a_continued_task_still_goes_to_the_performer(agents, violetdb):
    fake, a = agents
    task = violetdb.create_task_session(task_name="订闹钟", task_description="订闹钟", task_info={}, status="pending_info")
    fake.responders["fake-intent"] = lambda kw: intent_json("task", active_task_id=task)
    result = _run(a)
    assert result["reminder"]["status"] == "set" and _performer_calls(fake)


def test_a_cancelled_reminder_of_this_turn_is_not_reported_as_set(agents, violetdb):
    fake, a = agents
    first = _run(a)
    violetdb.update_commitment(first["reminder"]["commitment_id"], status="cancelled")
    again = _run(a)                                    # the same turn again: the stored row is not pending
    assert again["reminder"]["status"] == "not_set" and "保存好了" not in _chat_system(fake)


def test_switched_off_the_old_path_is_unchanged(agents, violetdb, monkeypatch):
    from tests.fake_mcp import campus_server, use
    use(monkeypatch, campus_server(agents=("performer",)))   # phase 9.2: a task needs a tool that could carry it out
    fake, a = agents
    monkeypatch.setenv("VIOLET_REMINDER_BEFORE_REPLY", "0")
    result = _run(a)
    assert result["reminder"] is None and violetdb.fetch_all("SELECT * FROM commitments") == []
    assert _performer_calls(fake)                      # routed to the Performer as before
    assert "【提醒】" not in _chat_system(fake)


def _memory_tool(memory, name, args, user_text, turn_id):
    from ai.agents.base_agent import _CALL_CONTEXT
    token = _CALL_CONTEXT.set({"turn_id": turn_id, "user_message": user_text, "background_job": True})
    try:
        return json.loads(memory._handlers[name](args))
    finally:
        _CALL_CONTEXT.reset(token)


def test_memory_agent_does_not_write_the_settled_reminder_again(agents, violetdb):
    fake, a = agents
    first = _run(a)
    got = _memory_tool(a["memory"], "create_commitment",
                       {"content": "提醒用户交作业", "evidence": USER_26, "due_date": "明天", "due_clock": "08:00"},
                       USER_26, "turn-26")
    assert got["action"] == "already_saved" and got["commitment_id"] == first["reminder"]["commitment_id"]
    assert len(violetdb.fetch_all("SELECT * FROM commitments")) == 1


def test_memory_agent_does_not_set_a_reminder_the_user_was_told_was_not_set(agents, violetdb):
    fake, a = agents
    _run(a, "提醒我八点交作业")
    got = _memory_tool(a["memory"], "create_commitment",
                       {"content": "交作业", "evidence": "提醒我八点交作业", "due_date": "明天", "due_clock": "08:00"},
                       "提醒我八点交作业", "turn-26")
    assert (got["ok"], got["reason"]) == (False, "settled_before_reply")
    assert violetdb.fetch_all("SELECT * FROM commitments") == []


def test_an_already_pending_reminder_is_not_duplicated_by_memory_agent(agents, violetdb):
    fake, a = agents
    first = _run(a)
    later = _run(a, turn_id="turn-27")                 # "already_pending": nothing saved for turn-27
    got = _memory_tool(a["memory"], "create_commitment",
                       {"content": "提醒交作业", "evidence": USER_26, "due_date": "明天", "due_clock": "08:00"},
                       USER_26, "turn-27")
    assert later["reminder"]["status"] == "already_pending"
    assert got["action"] == "already_saved" and got["commitment_id"] == first["reminder"]["commitment_id"]
    assert len(violetdb.fetch_all("SELECT * FROM commitments")) == 1


def test_without_the_step_before_the_reply_memory_agent_still_creates(agents, violetdb):
    """e.g. the pipeline crashed before the step ran: a readable request is not lost."""
    fake, a = agents
    got = _memory_tool(a["memory"], "create_commitment",
                       {"content": "交作业", "evidence": USER_26, "due_date": "明天", "due_clock": "08:00"},
                       USER_26, "turn-crashed")
    assert got["action"] == "created" and len(violetdb.fetch_all("SELECT * FROM commitments")) == 1


def test_other_request_words_are_still_created_by_memory_agent(agents, violetdb):
    fake, a = agents
    text = "明天早上八点催我交作业"
    got = _memory_tool(a["memory"], "create_commitment",
                       {"content": "交作业", "evidence": text, "due_date": "明天", "due_clock": "08:00"}, text, "turn-x")
    assert got["action"] == "created" and len(violetdb.fetch_all("SELECT * FROM commitments")) == 1


def test_a_failing_memory_job_keeps_the_saved_reminder(make_world):
    w = make_world()
    w.llm.responders["fake-intent"] = lambda kw: intent_json("task", task={"exists": True, "name": "提醒交作业",
                                                                          "description": "提醒交作业"})
    w.llm.responders["fake-chat"] = lambda kw: "好，明早八点叫你交作业"

    def broken(kw):
        raise RuntimeError("memory model down")
    w.llm.responders["fake-memory"] = broken
    say(w, USER_26)
    assert w.client.sent_texts[-1] == "好，明早八点叫你交作业"
    job = w.db.fetch_all("SELECT * FROM memory_jobs")[-1]
    assert job["status"] == "failed" and job["attempts"] == 3          # the product's own retries, unchanged
    rows = w.db.fetch_all("SELECT * FROM commitments")
    assert [(r["content"], r["status"]) for r in rows] == [("交作业", "pending")]
    assert w.llm.calls_for("fake-performer") == []


def test_memory_agent_after_the_reply_adds_nothing(make_world):
    w = make_world()
    w.llm.responders["fake-chat"] = lambda kw: "好，明早八点叫你交作业"
    w.llm.responders["fake-memory"] = script(
        tool("create_commitment", content="交作业", evidence=USER_26, due_date="tomorrow", due_clock="08:00"))
    say(w, USER_26)
    rows = w.db.fetch_all("SELECT * FROM commitments")
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    assert w.db.fetch_all("SELECT status FROM memory_jobs")[-1]["status"] == "done"


# ---------------------------------------------------------------------------
# Budget: a reserve for the next call; reported usage is never changed
# ---------------------------------------------------------------------------

def _rec(budget, history):
    from tools.harness import CallRecord, CallRecorder
    rec = CallRecorder(budget)
    for agent, tokens in history:
        rec.record(CallRecord(agent=agent, model="m", started_at="t", latency_ms=1, ok=True, total_tokens=tokens,
                              usage_present=True))
    return rec


# the 27 calls before call 28 of the real round 26 (report 20261013-221347): 229,634 tokens
ROUND_26_HISTORY = [("IntentAgent", 7651), ("IntentAgent", 7810), ("PerformerAgent", 6590), ("PerformerAgent", 6806),
                    ("PerformerAgent", 6969), ("ChatAgent", 6647), ("MemoryAgent", 11160), ("MemoryAgent", 11266),
                    ("IntentAgent", 7682), ("IntentAgent", 7799), ("ChatAgent", 6409), ("MemoryAgent", 11159),
                    ("MemoryAgent", 11297), ("MemoryAgent", 11374), ("IntentAgent", 7667), ("ChatAgent", 6470),
                    ("MemoryAgent", 11169), ("MemoryAgent", 11430), ("MemoryAgent", 11483), ("IntentAgent", 7674),
                    ("IntentAgent", 7796), ("PerformerAgent", 6577), ("PerformerAgent", 6845), ("PerformerAgent", 6900),
                    ("PerformerAgent", 7091), ("ChatAgent", 6695), ("MemoryAgent", 11218)]


def test_round26_call28_is_refused_before_it_is_sent():
    from tools.harness import Budget, BudgetedClient, BudgetExceeded
    rec = _rec(Budget(max_calls_per_turn=10, max_calls_total=28, max_tokens_total=240_000), ROUND_26_HISTORY)
    assert rec.tokens == 229_634 and len(rec.calls) == 27

    class Inner:
        n = 0

        def __init__(self):
            self.chat = type("C", (), {})()
            self.chat.completions = type("X", (), {"create": self.create})()

        def create(self, **kw):
            Inner.n += 1
            raise AssertionError("must not be sent")
    from tools.harness import _AGENT
    token = _AGENT.set("MemoryAgent")
    try:
        client = BudgetedClient(Inner(), rec)
        with pytest.raises(BudgetExceeded, match="229634 used \\+ 12632 reserved"):
            client.chat.completions.create(model="m", messages=[])
        for _ in range(2):                                         # what the memory worker's retries would do
            with pytest.raises(BudgetExceeded):
                client.chat.completions.create(model="m", messages=[])
    finally:
        _AGENT.reset(token)
    assert Inner.n == 0 and len(rec.calls) == 27 and rec.tokens == 229_634       # nothing sent, usage untouched
    state = rec.budget_state()
    assert state["used_tokens"] == 229_634 and state["remaining_tokens"] == 10_366
    assert state["stop_detail"]["reserved_tokens"] == 12_632 and state["stop_detail"]["agent"] == "MemoryAgent"
    assert "largest earlier MemoryAgent call" in state["stop_detail"]["reserve_basis"]
    assert len(state["refused_calls"]) == 3 and "per-request" in state["limitation"]


def test_reserve_rules():
    from tools.harness import Budget
    budget = Budget(first_call_reserve=16_000, reserve_factor=1.1)
    rec = _rec(budget, [])
    assert rec.reserve_for("IntentAgent") == (16_000, "no earlier call: first_call_reserve")
    rec = _rec(budget, [("IntentAgent", 7_000), ("MemoryAgent", 11_000)])
    assert rec.reserve_for("MemoryAgent")[0] == 12_100                     # its own largest call
    assert rec.reserve_for("ChatAgent")[0] == 12_100                       # none yet: the largest of any agent
    assert rec.reserve_for("IntentAgent")[0] == 7_700                      # its own, not the largest of all
    rec = _rec(Budget(max_tokens_total=20_000), [])
    rec.check("IntentAgent")                                               # 0 + 16,000 <= 20,000
    rec = _rec(Budget(max_tokens_total=15_000), [])
    with pytest.raises(Exception, match="token cap 15000"):
        rec.check("IntentAgent")                                           # 0 + 16,000 > 15,000: refused at once


def test_a_budget_refused_memory_job_fails_once_and_is_reported(session_factory):
    from tools.harness import Budget
    session = session_factory(budget=Budget(max_calls_per_turn=2, max_calls_total=50))
    result = session.say("你好呀", 1)
    jobs = result["memory_jobs"]
    assert len(jobs) == 1 and jobs[0]["status"] == "failed" and jobs[0]["attempts"] == 1
    assert jobs[0]["error_type"] == "BudgetExceeded" and jobs[0]["budget_not_retried"]
    assert result["background_complete"] is False and result["errors"] == []      # no API error, still not complete
    kinds = [e["kind"] for e in result["execution_errors"]]
    assert "memory_job" in kinds and "budget_refused" in kinds
    assert len(session.recorder.calls) == 2                                        # refusals are never sent


def test_a_refused_reply_is_a_failed_turn(session_factory):
    from tools.harness import Budget
    session = session_factory(budget=Budget(max_calls_per_turn=1, max_calls_total=50))
    result = session.say("你好呀", 1)
    assert "turn" in [e["kind"] for e in result["execution_errors"]]


def test_state_and_continuation_through_the_command_line(run_eval, tmp_path):
    wd = tmp_path / "kept-run"
    rc, report = run_eval("--rounds", "1", "--max-calls-per-round", "2", "--workdir", str(wd), "--keep")
    assert rc == 0
    state = json.loads((wd / ".violet-eval-state.json").read_text(encoding="utf-8"))
    assert state["rounds"][0]["background_complete"] is False
    assert state["rounds"][0]["memory_jobs"][0]["error_type"] == "BudgetExceeded"
    rc, report = run_eval("--rounds", "2", "--workdir", str(wd))
    assert rc == 2 and report is None                                       # refused before anything ran
    rc, report = run_eval("--rounds", "2", "--workdir", str(wd), "--accept-incomplete-memory", "--keep")
    assert rc == 0 and report["continuation"]["accepted_rounds_with_unfinished_background_work"] == [1]


def test_execution_errors_never_give_a_pass(run_eval):
    from tools.dialogue_eval import summarize
    rc, report = run_eval("--rounds", "1", "--max-calls-per-round", "2")
    assert rc == 0
    r1 = report["rounds"][0]
    assert r1["background_complete"] is False and r1["execution_errors"]
    assert report["budget"]["refused_calls"] and report["budget"]["used_tokens"] == sum(
        c["total_tokens"] or 0 for c in r1["call_details"])
    r1["score"] = 2
    summary = summarize(report)
    assert summary["verdict"] == "fail_execution_incomplete"
    assert summary["rounds_with_unfinished_background_work"] == [1]


def test_continuing_on_unfinished_memory_work_needs_an_explicit_yes(tmp_path, violetdb):
    from database import VioletDatabase
    from tools.replay import STATE_VERSION, ReplaySourceError, check_workdir, write_state
    wd = tmp_path / "kept"
    wd.mkdir()
    db = VioletDatabase(wd / "violet-test.db")
    db.add_user_message("text", "你好")
    job = db.add_memory_job("t1", "你好", 1, status="pending")
    db.update_memory_job(job, status="failed", attempts=3, last_error="BudgetExceeded: token cap 240000 reached")
    db.close()
    write_state(wd, {"version": STATE_VERSION, "plan": "p", "plan_sha256": "x", "plan_revisions": {}, "replay": None,
                     "rounds": [{"n": 1, "mode": "real", "background_complete": False}], "clock_end": None})
    with pytest.raises(ReplaySourceError, match="--accept-incomplete-memory"):
        check_workdir(wd, False, {2}, True)
    result = check_workdir(wd, False, {2}, True, accept_incomplete_memory=True)
    assert result["mode"] == "continue" and result["rounds_with_unfinished_background_work"] == [1]
    assert [(j["job_id"], j["status"], j["error_type"]) for j in result["unfinished_memory_jobs"]] == [
        (job, "failed", "BudgetExceeded")]


# ---------------------------------------------------------------------------
# Round 28: a due reminder vs. ordinary proactive chat
# ---------------------------------------------------------------------------

def _round28(session_factory, due="2026-10-23T08:00:00+08:00", hours=20.0):
    from ai.settings import local_timezone
    from tools.harness import ScenarioClock
    session = session_factory()
    session.use_clock(ScenarioClock(datetime(2026, 10, 22, 16, 18, tzinfo=local_timezone())))
    session.db.add_user_message("text", USER_26)
    cid = session.db.add_commitment("交作业", due, evidence=USER_26, source_turn_id="turn-26")["commitment"]["id"]
    return session, cid, session.advance(hours)


def test_round28_reminder_sent_late_because_of_the_time_jump(session_factory):
    from tools.dialogue_eval import TIME_JUMP_DELAY, _proactive
    session, cid, jump = _round28(session_factory)
    entry = _proactive(session, "allowed_or_skipped_without_bypass", time_advance=jump)
    assert entry["category"] == "commitment_reminder" and entry["ok"] is True, entry
    [r] = entry["reminders"]
    assert (r["commitment_id"], r["delivery"], r["status_after"], r["event_status"]) == (cid, "sent", "sent", "sent")
    assert r["proactive_event_id"] and r["outbox"] and all(o["status"] == "sent" for o in r["outbox"])
    assert r["late_minutes"] == 4 * 60 + 18 and r["late_cause"].startswith(TIME_JUMP_DELAY)
    assert entry["chat_proactive"]["sent"] == []                 # the reminder is not counted as proactive chat
    assert session.wechat.sent and all("交作业" in m["text"] for m in session.wechat.sent)


def test_round28_reminder_not_sent_is_never_a_success(session_factory, monkeypatch):
    from tools.dialogue_eval import _proactive
    session, cid, jump = _round28(session_factory)

    def failing(messages, event_id=None):
        raise ConnectionError("send failed")
    monkeypatch.setattr(session.scheduler, "send_messages", failing)
    entry = _proactive(session, "allowed_or_skipped_without_bypass", time_advance=jump)
    [r] = entry["reminders"]
    assert r["delivery"] == "not_sent" and r["status_after"] != "sent"
    assert entry["ok"] is False and any(f"#{cid}" in f for f in entry["failures"])


def test_round28_an_ordinary_proactive_message_is_not_a_delivered_reminder(session_factory, monkeypatch):
    from tools.dialogue_eval import _proactive
    session, cid, jump = _round28(session_factory)

    def chat_instead():
        # a normal proactive message that even names the thing -- but the reminder itself is not delivered
        event = session.db.add_proactive_event("scheduled_proactive_check", status="sent", action_type="chat")
        session.db.execute("INSERT INTO outbound_messages (client_id, origin, proactive_event_id, message_type, content,"
                           " status, created_at, updated_at) VALUES ('x1', 'proactive', ?, 'text', '记得交作业', 'sent',"
                           " ?, ?)", (event, session.db.now(), session.db.now()))
        return {"due_reason": "due", "event": {"status": "sent", "reason": None}, "sent": ["记得交作业"]}
    monkeypatch.setattr(session, "proactive_tick", chat_instead)
    entry = _proactive(session, "allowed_or_skipped_without_bypass", time_advance=jump)
    [r] = entry["reminders"]
    assert r["delivery"] == "not_sent" and r["proactive_event_id"] is None
    assert entry["category"] != "commitment_reminder" and entry["chat_proactive"]["sent"] == ["记得交作业"]
    assert entry["ok"] is False


def test_round28_a_reminder_marked_sent_without_a_message_is_not_a_success(session_factory, monkeypatch):
    from tools.dialogue_eval import _proactive
    session, cid, jump = _round28(session_factory)

    def marked_only():
        # the rows say "sent", but no message about it ever went out
        event = session.db.add_proactive_event("commitment_reminder", status="sent", action_type="chat",
                                               decision={"commitment_id": cid, "content": "交作业"})
        session.db.update_commitment(cid, status="sent", delivered_at=session.db.now(), proactive_event_id=event)
        return {"due_reason": "due", "event": {"status": "sent", "reason": None}, "sent": []}
    monkeypatch.setattr(session, "proactive_tick", marked_only)
    entry = _proactive(session, "allowed_or_skipped_without_bypass", time_advance=jump)
    [r] = entry["reminders"]
    assert r["status_after"] == "sent" and r["outbox"] == []
    assert r["delivery"] == "sent_without_matching_message" and entry["ok"] is False


def test_round28_ordinary_chat_past_the_gate_fails(session_factory, monkeypatch):
    from tools.dialogue_eval import _proactive
    session, cid, jump = _round28(session_factory, due="2026-10-30T08:00:00+08:00")     # nothing due
    monkeypatch.setattr(session, "proactive_tick", lambda: {"due_reason": "quiet hours", "event": None, "sent": ["在吗"]})
    entry = _proactive(session, "allowed_or_skipped_without_bypass", time_advance=jump)
    assert entry["reminders"] == [] and entry["ok"] is False and "闸门" in entry["failures"][0]


def test_round28_a_reminder_sent_in_quiet_hours_fails(session_factory):
    from tools.dialogue_eval import _proactive
    session, cid, jump = _round28(session_factory, due="2026-10-23T02:00:00+08:00", hours=10.7)   # 03:00, quiet
    path = session.workdir / "proactive.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["commitments"] = {**cfg.get("commitments", {}), "respect_quiet_hours": False}
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    entry = _proactive(session, "allowed_or_skipped_without_bypass", time_advance=jump)
    assert entry["reminders"][0]["delivery"] == "sent"
    assert entry["ok"] is False and "静默时段" in "".join(entry["failures"])


def test_run_plan_gives_the_round28_check_its_time_jump(run_eval, monkeypatch):
    from tools import dialogue_eval
    seen = []
    real = dialogue_eval._proactive

    def spy(session, what, situation_ids_before=None, time_advance=None):
        seen.append((what, time_advance))
        return real(session, what, situation_ids_before, time_advance=time_advance)
    monkeypatch.setattr(dialogue_eval, "_proactive", spy)
    rc, report = run_eval("--rounds", "28")
    assert rc == 0
    assert seen and seen[0][0] == "allowed_or_skipped_without_bypass" and seen[0][1]["hours"] == 20.0


def test_round28_in_quiet_hours_the_reminder_waits(session_factory):
    from tools.dialogue_eval import _proactive
    session, cid, jump = _round28(session_factory, due="2026-10-23T02:00:00+08:00", hours=10.7)   # 03:00, quiet
    entry = _proactive(session, "allowed_or_skipped_without_bypass", time_advance=jump)
    [r] = entry["reminders"]
    assert r["delivery"] == "postponed_quiet_hours" and r["status_after"] == "pending"
    assert entry["ok"] is True and entry["quiet_hours"] is True and session.wechat.sent == []
