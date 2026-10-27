"""Phase 2 proactive policy and background-memory behaviours (one test per behaviour)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests

from tests.fakes import far_quiet_hours, FakeILinkClient, FakeLLM, default_responders, install_fake_llm

PROJECT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def world(monkeypatch, violetdb, tmp_path):
    monkeypatch.setenv("DEBOUNCE_NORMAL", "0.05")
    monkeypatch.setenv("DEBOUNCE_EXTRA", "0.1")
    monkeypatch.setenv("VIOLET_SEND_RETRY_DELAY_SECONDS", "0")
    llm = install_fake_llm(monkeypatch, FakeLLM(default_responders("嗯嗯")))
    from ai.agents import ChatAgent, IntentAgent, MemoryAgent, PerformerAgent, ProactiveAgent
    from ai.proactive_scheduler import ProactiveScheduler
    from violet_runtime import VioletRuntime, MessageSender
    client = FakeILinkClient()
    agents = {"intent": IntentAgent(), "chat": ChatAgent(), "memory": MemoryAgent(), "performer": PerformerAgent()}
    config = tmp_path / "proactive.json"

    def write_config(**overrides):
        base = {"enabled": True, "quiet_hours": far_quiet_hours(), "daily_message_limit": 3, "min_minutes_after_user_message": 90,
                "min_minutes_between_proactive_messages": 180, "allowed_tasks": {},
                "random_delay_minutes": {k: [60, 60] for k in ("morning", "noon", "afternoon", "evening", "night", "other")}}
        base.update(overrides)
        config.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")

    write_config()
    holder = {}
    scheduler = ProactiveScheduler(ProactiveAgent(), agents["chat"], agents["performer"],
                                   send_messages=lambda m, e=None: holder["rt"].send_proactive(m, e), config_path=config)
    rt = VioletRuntime(client, violetdb, agents, proactive_scheduler=scheduler,
                    sender=MessageSender(client, violetdb, simulate_typing=False, sleep=lambda s: None))
    holder["rt"] = rt
    rt.start_worker()
    yield type("W", (), dict(rt=rt, client=client, llm=llm, db=violetdb, scheduler=scheduler, write_config=write_config))
    rt.stop(timeout=2)


def hhmm(dt):
    return dt.strftime("%H:%M")


def now(db):
    return datetime.fromisoformat(db.now())


def due_commitment(db, content="喝水"):
    result = db.add_commitment(content, (now(db) + timedelta(hours=1)).isoformat(), evidence=f"一小时后提醒我{content}")
    past = (now(db) - timedelta(minutes=1)).isoformat()
    return db.update_commitment(result["commitment"]["id"], due_at=past, next_attempt_at=past)


# --- background memory ---------------------------------------------------------------------

def test_reply_is_sent_before_memory_consolidation_runs(world):
    seen = []

    def memory(kw):
        jobs = world.db.list_memory_jobs()
        seen.append((list(world.client.sent_texts), jobs[0]["status"] if jobs else None))
        return json.dumps({"should_save": False, "reason": "t"})
    world.llm.responders["fake-memory"] = memory
    world.rt.ingest_text("今天好累")
    assert world.rt.wait_idle() and world.rt.wait_memory_idle()
    # exactly one consolidation, run by the background job after the reply went out
    assert seen == [(["嗯嗯"], "running")]
    job = world.db.list_memory_jobs()[0]
    assert (job["status"], job["assistant_reply"], job["user_message"]) == ("done", "嗯嗯", "今天好累")


def test_memory_can_be_switched_back_inline(monkeypatch, violetdb):
    monkeypatch.setenv("VIOLET_ASYNC_MEMORY", "0")
    install_fake_llm(monkeypatch, FakeLLM(default_responders()))
    from ai.agents import ChatAgent, IntentAgent, MemoryAgent, PerformerAgent
    from violet_runtime import VioletRuntime
    rt = VioletRuntime(FakeILinkClient(), violetdb, {"intent": IntentAgent(), "chat": ChatAgent(),
                                               "memory": MemoryAgent(), "performer": PerformerAgent()})
    assert rt.memory_worker is None and rt.async_memory is False


# --- defaults --------------------------------------------------------------------------------

def test_proactive_frequency_defaults_are_low():
    from ai.proactive_scheduler import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["daily_message_limit"] <= 3
    assert DEFAULT_CONFIG["min_minutes_after_user_message"] >= 60
    assert DEFAULT_CONFIG["min_minutes_between_proactive_messages"] >= 120
    shipped = json.loads((PROJECT / "proactive_config.json").read_text(encoding="utf-8"))
    assert (shipped["daily_message_limit"], shipped["min_minutes_after_user_message"],
            shipped["min_minutes_between_proactive_messages"]) == (3, 90, 180)


# --- backoff -----------------------------------------------------------------------------------

def test_engagement_decides_between_normal_interval_and_backoff(world):
    db = world.db
    db.add_proactive_event("scheduled_proactive_check", status="sent", action_type="chat")
    world.scheduler.on_user_engagement(False)
    gap = (datetime.fromisoformat(db.get_proactive_setting("next_proactive_at")) - now(db)).total_seconds() / 60
    assert 118 <= gap <= 121                      # cold streak 1 -> x2
    world.scheduler.tracker.evaluate_user_turn("刚下课，今天实验做得超顺利")
    world.scheduler.on_user_engagement(True)
    gap = (datetime.fromisoformat(db.get_proactive_setting("next_proactive_at")) - now(db)).total_seconds() / 60
    assert 58 <= gap <= 61


def test_backoff_is_capped(world):
    for _ in range(10):
        world.db.add_proactive_event("scheduled_proactive_check", status="sent", action_type="chat")
    nxt = world.scheduler._backoff_next_time(now(world.db))
    assert (nxt - now(world.db)).total_seconds() / 60 <= 60 * 8 + 1


# --- fixed-time tasks --------------------------------------------------------------------------

def _fixed_task_config(world, **extra):
    task = {"enabled": True, "description": "天气提醒", "fixed_check_time": hhmm(now(world.db)),
            "daily_limit": 1, "weekly_limit": 7}
    world.write_config(allowed_tasks={"weather_reminder": task}, **extra)
    world.llm.responders["fake-proactive"] = lambda kw: json.dumps({"should_act": False, "action_type": "none", "reason": "t"})


def test_fixed_task_skips_quiet_hours(world):
    current = now(world.db)
    _fixed_task_config(world, quiet_hours={"start": hhmm(current - timedelta(hours=1)), "end": hhmm(current + timedelta(hours=1))})
    world.scheduler.tick()
    assert world.llm.calls_for("fake-proactive") == []


def test_fixed_task_runs_once_and_ignores_chat_budget(world):
    _fixed_task_config(world)
    for _ in range(3):   # chat budget used up earlier today
        eid = world.db.add_proactive_event("scheduled_proactive_check", status="sent", action_type="chat")
        world.db.execute("UPDATE proactive_events SET created_at = ?, updated_at = ? WHERE id = ?",
                         ((now(world.db) - timedelta(hours=2)).isoformat(),) * 2 + (eid,))
    world.scheduler.tick()
    world.scheduler.tick()
    calls = world.llm.calls_for("fake-proactive")
    assert len(calls) == 1
    assert json.loads(calls[0]["messages"][-1]["content"])["event_type"] == "fixed_time_task_check"


def test_fixed_task_not_right_after_another_proactive_message(world):
    _fixed_task_config(world)
    world.db.add_proactive_event("scheduled_proactive_check", status="sent", action_type="chat")
    world.scheduler.tick()
    assert world.llm.calls_for("fake-proactive") == []


# --- commitments -------------------------------------------------------------------------------

def test_reminder_waits_for_quiet_hours_to_end(world):
    current = now(world.db)
    quiet_end = current + timedelta(hours=1)
    world.write_config(quiet_hours={"start": hhmm(current - timedelta(hours=1)), "end": hhmm(quiet_end)})
    commitment = due_commitment(world.db)
    world.scheduler.tick()
    assert world.client.sent_texts == []
    postponed = world.db.get_commitment(commitment["id"])
    assert postponed["status"] == "pending" and postponed["next_attempt_at"][11:16] == hhmm(quiet_end)


def test_reminder_falls_back_to_template_when_generation_fails(world):
    world.llm.responders["fake-chat"] = lambda kw: (_ for _ in ()).throw(RuntimeError("chat down"))
    commitment = due_commitment(world.db, "吃药")
    world.scheduler.tick()
    assert world.client.sent_texts == ["提醒你一下：吃药"]
    delivered = world.db.get_commitment(commitment["id"])
    assert delivered["status"] == "sent" and "chat down" in delivered["last_error"]
    assert world.db.get_proactive_event(delivered["proactive_event_id"])["reason"] == "fallback template used"


def test_reminder_text_must_mention_the_reminded_thing(world):
    world.llm.responders["fake-chat"] = lambda kw: "在干嘛呢"          # model drifted off-topic
    due_commitment(world.db, "吃药")
    world.scheduler.tick()
    assert world.client.sent_texts == ["提醒你一下：吃药"]


def test_reminder_is_claimed_only_once(world):
    commitment = due_commitment(world.db)
    assert world.db.claim_commitment(commitment["id"]) is True
    assert world.db.claim_commitment(commitment["id"]) is False
    world.scheduler.tick()
    assert world.client.sent_texts == []          # it is 'sending' elsewhere; not sent twice


def test_reminder_send_failure_is_retried_then_marked_failed(world):
    world.write_config(commitments={"enabled": True, "respect_quiet_hours": True, "max_attempts": 2, "retry_minutes": 0})
    world.llm.responders["fake-chat"] = lambda kw: "该喝水啦"
    world.client.send_failures = [requests.ReadTimeout("x"), requests.ReadTimeout("y")]
    commitment = due_commitment(world.db)
    world.scheduler.tick()
    first = world.db.get_commitment(commitment["id"])
    assert (first["status"], first["attempts"]) == ("pending", 1) and "x" in first["last_error"]
    world.scheduler.tick()
    second = world.db.get_commitment(commitment["id"])
    assert (second["status"], second["attempts"]) == ("failed", 2)
    assert world.client.sent_texts == []
    statuses = [e["status"] for e in world.db.list_proactive_events(event_type="commitment_reminder")]
    assert statuses == ["failed", "failed"]


# --- policy units ----------------------------------------------------------------------------------

def test_decision_validation_rules():
    from ai.chat import validate_proactive_decision
    state = {"allowed_memories": [{"id": 1, "content": "用户在学吉他"}],
             "cooling_topics": [{"topic_key": "daily_checkin"}]}
    assert validate_proactive_decision({"topic_key": "music", "mentioned_memory_ids": [1]}, state) is None
    assert "may not be raised" in validate_proactive_decision({"topic_key": "x", "mentioned_memory_ids": [2]}, state)
    assert "cooling" in validate_proactive_decision({"topic_key": "Daily_Checkin"}, state)
    assert validate_proactive_decision({"topic_key": "x", "mentioned_memory_ids": [2]}, {}) is None   # no state: legacy


def test_no_reply_token_means_do_not_send():
    from ai.chat_output import normalize_chat_output
    assert normalize_chat_output("[NO_REPLY]") == {"should_send": False, "messages": [], "declined": True}
    assert [m["content"] for m in normalize_chat_output("嗯\n[NO_REPLY]")["messages"]] == ["嗯"]


def test_topic_guard_only_blocks_clear_matches():
    from ai.memory_model import touches_topic
    assert touches_topic("对了你那个服务器修好了没", "用户的服务器出现故障")
    assert not touches_topic("晚饭吃了吗", "用户中午吃了火锅")
    assert not touches_topic("工作忙吗", "用户工作压力大")
