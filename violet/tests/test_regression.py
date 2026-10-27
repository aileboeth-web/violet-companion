"""Regression: existing core behaviour still works end to end (all mocked)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tests.fakes import (
    far_quiet_hours,
    FakeILinkClient, FakeLLM, completion, default_responders, image_message, install_fake_llm, intent_json,
    text_message,
)


@pytest.fixture()
def env(monkeypatch, violetdb, tmp_path):
    monkeypatch.setenv("DEBOUNCE_NORMAL", "0.05")
    monkeypatch.setenv("DEBOUNCE_EXTRA", "0.1")
    llm = install_fake_llm(monkeypatch, FakeLLM(default_responders("哼\n终于想起我了")))
    from ai.agents import ChatAgent, IntentAgent, MemoryAgent, PerformerAgent, ProactiveAgent
    from violet_runtime import VioletRuntime, MessageSender
    client = FakeILinkClient()
    agents = {"intent": IntentAgent(), "chat": ChatAgent(), "memory": MemoryAgent(), "performer": PerformerAgent()}
    sender = MessageSender(client, violetdb, simulate_typing=False, sleep=lambda s: None)
    vision_calls = []

    def vision(path):
        vision_calls.append(path)
        return "桌上有一杯奶茶"
    rt = VioletRuntime(client, violetdb, agents, vision=vision, sender=sender)
    rt.start_worker()
    yield {"rt": rt, "client": client, "llm": llm, "db": violetdb, "agents": agents,
           "proactive_agent": ProactiveAgent(), "vision_calls": vision_calls, "tmp": tmp_path}
    rt.stop(timeout=2)


def _turn(env, text):
    env["rt"].ingest_text(text)
    assert env["rt"].wait_idle()
    assert env["rt"].wait_memory_idle()     # memory now runs after the reply, in the background


def test_normal_chat_turn(env):
    _turn(env, "我回来啦")
    assert env["client"].sent_texts == ["哼", "终于想起我了"]           # one WeChat message per line
    rows = env["db"].get_recent_messages(10)
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "我回来啦"), ("assistant", "哼"), ("assistant", "终于想起我了")]
    # Phase 2: memory runs in the background after the reply and now also sees
    # what Violet actually sent (the old prompt claimed this but the code never passed it).
    assert env["rt"].wait_memory_idle()
    memory_call = env["llm"].calls_for("fake-memory")[0]
    assert memory_call["messages"][-1]["content"] == "用户说：我回来啦\nViolet 实际回复：哼\n终于想起我了"


def test_silent_route_sends_nothing_but_keeps_history(env):
    env["llm"].responders["fake-intent"] = lambda kw: intent_json("silent")
    _turn(env, "晚安啦")
    assert env["client"].sent_texts == []
    assert [r["content"] for r in env["db"].get_recent_messages(5)] == ["晚安啦"]
    assert env["llm"].calls_for("fake-chat") == []


def test_task_route_creates_task_and_runs_performer(env, monkeypatch):
    from tests.fake_mcp import campus_server, use
    use(monkeypatch, campus_server(agents=("performer",)))   # phase 9.2: a task needs a tool that could carry it out
    env["llm"].responders["fake-intent"] = lambda kw: intent_json("task")
    env["llm"].responders["fake-performer"] = lambda kw: json.dumps({"task_status": "pending_info", "task_id": 1})
    _turn(env, "帮我点杯奶茶")
    tasks = env["db"].list_task_sessions()
    assert len(tasks) == 1 and tasks[0]["task_name"] == "测试任务"
    performer_input = json.loads(env["llm"].calls_for("fake-performer")[0]["messages"][-1]["content"])
    assert performer_input["active_task_id"] == tasks[0]["id"]
    chat_call = env["llm"].calls_for("fake-chat")[0]
    assert any("任务执行结果" in (m.get("content") or "") for m in chat_call["messages"])


def test_memory_agent_tools_still_write_memories(env):
    script = iter([
        completion(tool_calls=[{"name": "save_memory", "arguments": {
            "summary": "用户周五有考试", "category": "event", "importance": 3, "emotional_weight": 2}}]),
        completion(tool_calls=[{"name": "upsert_core_memory", "arguments": {
            "memory_key": "reply_style", "category": "reply_style", "content": "喜欢短句"}}]),
        completion(content=json.dumps({"should_save": True, "reason": "ok"})),
    ])
    env["llm"].responders["fake-memory"] = lambda kw: next(script)
    _turn(env, "周五要考试了，你回我短一点")
    assert [m["content"] for m in env["db"].list_event_memories()] == ["用户周五有考试"]
    assert env["db"].get_core_memory_by_key("reply_style")["content"] == "喜欢短句"


def test_intent_recall_is_injected_into_chat(env):
    env["db"].add_event_memory("用户家的狗叫豆豆", category="event")
    script = iter([
        # The legacy character-overlap similarity is unchanged in phase 1, so the
        # query must share characters with the memory (a later phase replaces it).
        completion(tool_calls=[{"name": "query_memory", "arguments": {"query": "狗 豆豆"}}]),
        completion(content=intent_json("chat")),
    ])
    env["llm"].responders["fake-intent"] = lambda kw: next(script)
    _turn(env, "我家狗今天不吃饭")
    chat_call = env["llm"].calls_for("fake-chat")[0]
    recalled = [m["content"] for m in chat_call["messages"] if "相关记忆" in (m.get("content") or "")]
    assert recalled and "豆豆" in recalled[0]


def test_image_is_described_and_answered(env):
    env["client"].queue_messages([image_message(5)])
    env["rt"].poll_once()
    assert env["rt"].wait_idle()
    assert env["vision_calls"] == ["fake-image.jpg"]
    user = env["db"].get_recent_messages(5)[0]["content"]
    assert user == "[用户发来一张图片]\n图片内容：桌上有一杯奶茶"
    assert env["client"].sent_texts == ["哼", "终于想起我了"]


def test_debounce_waits_longer_for_unfinished_sentences():
    from violet_runtime import TurnBuffer, is_incomplete
    delays = []

    class FakeTimer:
        def __init__(self, delay, fn):
            delays.append(delay)
            self.daemon = True

        def start(self):
            pass

        def cancel(self):
            pass

    buf = TurnBuffer(lambda turn: None, normal_delay=5, extra_delay=10, timer_factory=FakeTimer)
    buf.add_text(1, "我跟你说")
    buf.add_text(2, "今天好开心")
    buf.add_text(3, "然后")
    assert delays == [10, 5, 10]
    assert is_incomplete(["就是"]) and not is_incomplete(["吃饭了吗"])


def test_scheduled_proactive_message_is_sent_and_recorded(env):
    from ai.proactive_scheduler import ProactiveScheduler
    env["llm"].responders["fake-proactive"] = lambda kw: json.dumps(
        {"should_act": True, "action_type": "chat", "reason": "好久没说话", "chat_brief": {"topic": "问候"}})
    env["llm"].responders["fake-chat"] = lambda kw: "在干嘛呢"
    config = env["tmp"] / "proactive.json"
    config.write_text(json.dumps({"enabled": True, "quiet_hours": far_quiet_hours(), "min_minutes_after_user_message": 30,
                                  "min_minutes_between_proactive_messages": 60, "daily_message_limit": 2,
                                  "allowed_tasks": {}}), encoding="utf-8")
    rt = env["rt"]
    scheduler = ProactiveScheduler(env["proactive_agent"], env["agents"]["chat"], env["agents"]["performer"],
                                   send_messages=lambda m, e=None: rt.send_proactive(m, e), config_path=config,
                                   is_busy=rt.is_busy, pipeline_lock=rt.pipeline_lock)
    db = env["db"]
    db.add_user_message("text", "早", created_at=(datetime.fromisoformat(db.now()) - timedelta(hours=2)).isoformat())
    db.set_proactive_setting("next_proactive_at", (datetime.fromisoformat(db.now()) - timedelta(minutes=1)).isoformat())
    scheduler.tick()
    assert env["client"].sent_texts == ["在干嘛呢"]
    event = db.list_proactive_events()[0]
    assert event["status"] == "sent" and event["sent_messages"][0]["content"] == "在干嘛呢"
    assert db.get_recent_messages(5)[-1]["content"] == "在干嘛呢"
    # dry-run test endpoint path still works without sending
    result = scheduler.force_run(send=False)
    assert result["proactive_decision"]["should_act"] is True
    assert env["client"].sent_texts == ["在干嘛呢"]


def test_reload_keeps_agents_working(env):
    for agent in env["agents"].values():
        agent.reload()
    _turn(env, "还在吗")
    assert env["client"].sent_texts == ["哼", "终于想起我了"]
