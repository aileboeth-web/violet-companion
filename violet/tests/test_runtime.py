"""Message runtime: inbox, poll loop, turn worker, outbox, recovery, proactive coordination."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta

import pytest
import requests

from tests.fakes import (
    far_quiet_hours,
    FakeILinkClient, FakeLLM, default_responders, image_message, install_fake_llm, intent_json,
    text_message, wait_until,
)


@pytest.fixture()
def env(monkeypatch, violetdb, tmp_path):
    monkeypatch.setenv("DEBOUNCE_NORMAL", "0.05")
    monkeypatch.setenv("DEBOUNCE_EXTRA", "0.1")
    monkeypatch.setenv("VIOLET_POLL_INTERVAL_SECONDS", "0.02")
    monkeypatch.setenv("VIOLET_SEND_RETRY_DELAY_SECONDS", "0")
    fake_llm = install_fake_llm(monkeypatch, FakeLLM(default_responders("好呀\n我在听")))
    from ai.agents import ChatAgent, IntentAgent, MemoryAgent, PerformerAgent
    from violet_runtime import VioletRuntime, MessageSender

    client = FakeILinkClient()
    agents = {"intent": IntentAgent(), "chat": ChatAgent(), "memory": MemoryAgent(), "performer": PerformerAgent()}
    holder = {}

    def build(**kwargs):
        stats: dict[str, int] = {}
        sender = MessageSender(client, violetdb, simulate_typing=False, sleep=lambda s: None, stats=stats)
        rt = VioletRuntime(client, violetdb, agents, vision=kwargs.pop("vision", lambda path: "一只橘猫趴在键盘上"),
                        sender=sender, **kwargs)
        rt.stats = stats
        holder["rt"] = rt
        return rt

    yield {"client": client, "llm": fake_llm, "agents": agents, "db": violetdb, "build": build}
    if "rt" in holder:
        holder["rt"].stop(timeout=2)


def _user_messages(db):
    return [r["content"] for r in db.fetch_all("SELECT content FROM messages WHERE role='user' ORDER BY id")]


def _assistant_messages(db):
    return [r["content"] for r in db.fetch_all("SELECT content FROM messages WHERE role='assistant' ORDER BY id")]


# --- poll loop resilience ---------------------------------------------------------

def test_poll_loop_survives_media_download_failure(env):
    client, db = env["client"], env["db"]
    rt = env["build"]()
    client.queue_messages([image_message(1, fail_download=True), text_message("你看这个", message_id=2)])
    rt.start()
    assert wait_until(lambda: client.sent_texts == ["好呀", "我在听"])
    # loop is still alive and keeps polling new messages
    client.queue_messages([text_message("还在吗", message_id=3)])
    assert wait_until(lambda: len(client.sent_texts) == 4)
    assert rt.health()["poll_thread_alive"]
    image_row = db.fetch_one("SELECT * FROM inbound_messages WHERE wx_message_id='1'")
    assert "download_failed" in image_row["error"] and image_row["status"] == "done"
    first_turn = _user_messages(db)[0]
    assert "图片没能加载出来" in first_turn and "你看这个" in first_turn


def test_poll_loop_survives_fetch_errors_and_typing_failures(env):
    client = env["client"]
    rt = env["build"]()
    client.fetch_error = requests.ConnectionError("network down")
    client.fail_typing = True
    client.queue_messages([text_message("在吗", message_id=10)])
    rt.start()
    assert wait_until(lambda: client.sent_texts == ["好呀", "我在听"], timeout=15)
    assert rt.stats.get("typing_failures", 0) > 0
    assert rt.health()["poll_thread_alive"]


def test_poll_supervisor_restarts_after_unexpected_crash(env, monkeypatch):
    rt = env["build"]()
    calls = {"n": 0}
    original = rt._poll_loop

    def flaky_loop():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("bug outside the per-iteration guard")
        return original()
    monkeypatch.setattr(rt, "_poll_loop", flaky_loop)
    monkeypatch.setattr(rt._stop, "wait", lambda t=None: time.sleep(0.01) or rt._stop.is_set())
    rt.start_polling()
    assert wait_until(lambda: calls["n"] >= 2)
    assert rt.poll_restarts == 1 and rt.health()["poll_thread_alive"]


def test_one_failing_item_does_not_block_the_rest_of_the_batch(env, monkeypatch):
    client, db = env["client"], env["db"]
    rt = env["build"]()
    original = rt.buffer.add_text

    def flaky_add(inbound_id, text, received_at=None):
        if text == "坏消息":
            raise RuntimeError("buffer exploded")
        return original(inbound_id, text, received_at)
    monkeypatch.setattr(rt.buffer, "add_text", flaky_add)
    client.queue_messages([text_message("坏消息", message_id=1), text_message("好消息", message_id=2)])
    assert rt.poll_once() == 2
    rows = {r["content"]: r for r in db.fetch_all("SELECT * FROM inbound_messages")}
    assert rows["坏消息"]["status"] == "failed" and "buffer exploded" in rows["坏消息"]["error"]
    assert rows["好消息"]["status"] == "received"
    assert client.committed_bufs == 1, "all items were persisted, so the cursor must advance"


# --- no silent drops / duplicates -------------------------------------------------

def test_more_than_ten_messages_in_one_poll_are_all_processed(env):
    client, db, llm = env["client"], env["db"], env["llm"]
    rt = env["build"]()
    rt.start_worker()
    client.queue_messages([text_message(f"第{i}句", message_id=100 + i) for i in range(15)])
    assert rt.poll_once() == 15
    assert rt.wait_idle()
    combined = _user_messages(db)[-1]
    assert all(f"第{i}句" in combined for i in range(15))
    assert db.count_inbound_by_status() == {"done": 15}
    assert llm.calls_for("fake-chat")[-1]["messages"][-1]["content"] == combined


def test_redelivered_messages_are_not_processed_twice(env):
    client, db = env["client"], env["db"]
    rt = env["build"]()
    rt.start_worker()
    batch = [text_message("只说一次", message_id=7)]
    client.queue_messages(batch)
    client.queue_messages(batch)                     # e.g. cursor replay after a crash
    assert rt.poll_once() == 1
    assert rt.poll_once() == 0
    assert rt.wait_idle()
    assert _user_messages(db) == ["只说一次"]
    assert client.sent_texts == ["好呀", "我在听"]
    assert rt.stats["duplicates_dropped"] == 1


def test_messages_without_ids_are_never_deduplicated_by_guess(env):
    client, db = env["client"], env["db"]
    rt = env["build"]()
    client.queue_messages([text_message("哈哈"), text_message("哈哈")])
    assert rt.poll_once() == 2


def test_cursor_not_committed_if_inbox_write_fails(env, monkeypatch):
    client, db = env["client"], env["db"]
    rt = env["build"]()
    original = db.add_inbound_message

    def failing(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(db, "add_inbound_message", failing)
    client.queue_messages([text_message("hello", message_id=1)])
    rt.poll_once()
    assert client.committed_bufs == 0
    monkeypatch.setattr(db, "add_inbound_message", original)
    client.queue_messages([text_message("hello", message_id=1)])
    rt.poll_once()
    assert client.committed_bufs == 1


# --- turn failures --------------------------------------------------------------------

def test_user_message_stored_and_fallback_sent_when_pipeline_crashes(env):
    client, db = env["client"], env["db"]

    def crashing_pipeline(**kwargs):
        raise RuntimeError("unexpected bug")
    rt = env["build"](pipeline_runner=crashing_pipeline)
    rt.start_worker()
    rt.ingest_text("你好")
    assert rt.wait_idle()
    assert _user_messages(db) == ["你好"]
    from violet_runtime import DEFAULT_FALLBACK_REPLY
    assert client.sent_texts == [DEFAULT_FALLBACK_REPLY]
    row = db.fetch_one("SELECT * FROM inbound_messages")
    assert row["status"] == "failed" and "unexpected bug" in row["error"] and row["stored_message_id"]
    assert db.count_outbound_by_status() == {"sent": 1}


def test_chat_model_down_sends_configurable_fallback(env, monkeypatch):
    client, db, llm = env["client"], env["db"], env["llm"]
    llm.responders["fake-chat"] = lambda kw: (_ for _ in ()).throw(RuntimeError("relay down"))
    monkeypatch.setenv("VIOLET_FALLBACK_REPLY", "")
    rt = env["build"]()
    rt.start_worker()
    rt.ingest_text("在吗")
    assert rt.wait_idle()
    assert client.sent_texts == []                         # fallback disabled -> silent, but recorded
    assert _user_messages(db) == ["在吗"]
    assert db.fetch_one("SELECT status FROM inbound_messages")["status"] == "failed"


# --- outbox / send status ---------------------------------------------------------------

def test_send_failure_marks_failed_and_cancels_rest(env):
    client, db = env["client"], env["db"]
    client.send_failures = [requests.ReadTimeout("no answer")]   # ambiguous -> must not retry
    rt = env["build"]()
    rt.start_worker()
    rt.ingest_text("hi")
    assert rt.wait_idle()
    assert client.sent_texts == []
    rows = db.fetch_all("SELECT content, status, attempts FROM outbound_messages ORDER BY id")
    assert [(r["content"], r["status"], r["attempts"]) for r in rows] == [("好呀", "failed", 1), ("我在听", "cancelled", 0)]
    assert _assistant_messages(db) == []
    assert db.fetch_one("SELECT status FROM inbound_messages")["status"] == "failed"


def test_connection_error_is_retried_with_same_client_id(env):
    client, db = env["client"], env["db"]
    client.send_failures = [requests.ConnectionError("refused")]
    rt = env["build"]()
    rt.start_worker()
    rt.ingest_text("hi")
    assert rt.wait_idle()
    assert client.sent_texts == ["好呀", "我在听"]
    first = db.fetch_one("SELECT * FROM outbound_messages ORDER BY id LIMIT 1")
    assert first["status"] == "sent" and first["attempts"] == 2
    assert client.sent[0]["client_id"] == first["client_id"]


def test_api_rejection_is_a_failure(env):
    client, db = env["client"], env["db"]
    client.send_failures = [{"ret": -14, "errmsg": "session timeout"}]
    rt = env["build"]()
    rt.start_worker()
    rt.ingest_text("hi")
    assert rt.wait_idle()
    row = db.fetch_one("SELECT * FROM outbound_messages ORDER BY id LIMIT 1")
    assert row["status"] == "failed" and "SendRejected" in row["error"]


# --- concurrency -----------------------------------------------------------------------

def test_turns_are_serialized_and_see_previous_turn(env):
    client, db, llm = env["client"], env["db"], env["llm"]
    active, peak = {"n": 0}, {"n": 0}
    lock = threading.Lock()

    def slow_chat(kw):
        with lock:
            active["n"] += 1
            peak["n"] = max(peak["n"], active["n"])
        time.sleep(0.3)
        with lock:
            active["n"] -= 1
        return "收到"
    llm.responders["fake-chat"] = slow_chat
    rt = env["build"]()
    rt.start_worker()
    rt.ingest_text("第一条消息")
    assert wait_until(lambda: len(llm.calls_for("fake-chat")) == 1)
    rt.ingest_text("第二条消息")                      # arrives while turn 1 is running
    assert rt.wait_idle(timeout=15)
    assert peak["n"] == 1
    second_call = llm.calls_for("fake-chat")[1]
    # Phase 3: the previous turn arrives as real dialogue turns (not a system transcript),
    # in order and with the right roles, right before the new message.
    dialogue = [(m["role"], m["content"]) for m in second_call["messages"] if m["role"] in ("user", "assistant")]
    assert dialogue[-3:] == [("user", "第一条消息"), ("assistant", "收到"), ("user", "第二条消息")]
    assert not any("第一条消息" in (m.get("content") or "") for m in second_call["messages"] if m["role"] == "system")


def test_proactive_tick_waits_while_user_turn_is_pending(env, tmp_path):
    client, db, llm = env["client"], env["db"], env["llm"]
    from ai.agents import ProactiveAgent
    from ai.proactive_scheduler import ProactiveScheduler
    config = tmp_path / "proactive.json"
    config.write_text(json.dumps({"enabled": True, "quiet_hours": far_quiet_hours(), "min_minutes_after_user_message": 0,
                                  "min_minutes_between_proactive_messages": 0, "daily_message_limit": 5,
                                  "allowed_tasks": {}}), encoding="utf-8")
    rt_holder = {}
    scheduler = ProactiveScheduler(ProactiveAgent(), env["agents"]["chat"], env["agents"]["performer"],
                                   send_messages=lambda m, e=None: rt_holder["rt"].send_proactive(m, e),
                                   config_path=config)
    rt = env["build"](proactive_scheduler=scheduler)
    rt_holder["rt"] = rt
    db.add_user_message("text", "很早之前的消息", created_at=(datetime.fromisoformat(db.now()) - timedelta(hours=3)).isoformat())
    db.set_proactive_setting("next_proactive_at", (datetime.fromisoformat(db.now()) - timedelta(minutes=1)).isoformat())

    rt.ingest_text("我来了")            # buffered, not yet processed (worker not started)
    # on_user_message() pushed next_proactive_at into the future; re-arm it so the
    # tick would fire if it ignored the pending user turn.
    db.set_proactive_setting("next_proactive_at", (datetime.fromisoformat(db.now()) - timedelta(minutes=1)).isoformat())
    scheduler.tick()
    assert llm.calls_for("fake-proactive") == []
    rt.start_worker()
    assert rt.wait_idle()
    db.set_proactive_setting("next_proactive_at", (datetime.fromisoformat(db.now()) - timedelta(minutes=1)).isoformat())
    scheduler.tick()
    assert len(llm.calls_for("fake-proactive")) == 1


def test_proactive_send_failure_is_recorded(env, tmp_path):
    client, db, llm = env["client"], env["db"], env["llm"]
    from ai.agents import ProactiveAgent
    from ai.proactive_scheduler import ProactiveScheduler
    llm.responders["fake-proactive"] = lambda kw: json.dumps({"should_act": True, "action_type": "chat", "reason": "t"})
    llm.responders["fake-chat"] = lambda kw: "想你了"
    client.send_failures = [requests.ReadTimeout("x")]
    rt_holder = {}
    scheduler = ProactiveScheduler(ProactiveAgent(), env["agents"]["chat"], env["agents"]["performer"],
                                   send_messages=lambda m, e=None: rt_holder["rt"].send_proactive(m, e),
                                   config_path=tmp_path / "p.json")
    rt = env["build"](proactive_scheduler=scheduler)
    rt_holder["rt"] = rt
    result = scheduler.force_run(send=True)
    event = db.get_proactive_event(result["event_id"])
    assert event["status"] == "failed" and "send failed" in event["reason"]
    assert db.fetch_one("SELECT status, origin FROM outbound_messages") == {"status": "failed", "origin": "proactive"}


# --- recovery ------------------------------------------------------------------------------

def test_recovery_requeues_fresh_and_expires_stale(env):
    client, db = env["client"], env["db"]
    now = datetime.fromisoformat(db.now())
    fresh = db.add_inbound_message("mid:1:0", "1", 0, 1, "刚才没回我", received_at=(now - timedelta(seconds=30)).isoformat())
    stale = db.add_inbound_message("mid:2:0", "2", 0, 1, "很久以前的话", received_at=(now - timedelta(hours=2)).isoformat())
    half_done = db.add_inbound_message("mid:3:0", "3", 0, 1, "回复到一半", received_at=(now - timedelta(seconds=20)).isoformat())
    stored = db.add_user_message("text", "回复到一半")
    db.update_inbound_messages([half_done], status="processing", stored_message_id=stored)

    rt = env["build"]()
    assert rt.recover_pending() == 1
    rt.start_worker()
    assert rt.wait_idle()
    statuses = {r["id"]: r["status"] for r in db.fetch_all("SELECT id, status FROM inbound_messages")}
    assert statuses == {fresh: "done", stale: "expired", half_done: "expired"}
    users = _user_messages(db)
    assert users.count("回复到一半") == 1 and "很久以前的话" in users and "刚才没回我" in users
    assert client.sent_texts == ["好呀", "我在听"]       # only the fresh one is answered
