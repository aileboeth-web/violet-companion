"""LLM call layer and pipelines: degradation instead of crashes."""

from __future__ import annotations

import json
import threading
import time

import pytest

from tests.fakes import (
    FakeLLM, api_connection_error, completion, default_responders, install_fake_llm, intent_json,
)


@pytest.fixture()
def agents(monkeypatch, violetdb):
    fake = install_fake_llm(monkeypatch, FakeLLM(default_responders()))
    from ai.agents import ChatAgent, IntentAgent, MemoryAgent, PerformerAgent, ProactiveAgent
    return fake, {
        "intent": IntentAgent(), "chat": ChatAgent(), "memory": MemoryAgent(),
        "performer": PerformerAgent(), "proactive": ProactiveAgent(),
    }


def _run(agents_map, text="今天好累"):
    from ai.chat import run_pipeline
    return run_pipeline(
        user_message=text,
        intent_agent=agents_map["intent"],
        chat_agent=agents_map["chat"],
        performer_agent=agents_map["performer"],
        memory_agent=agents_map["memory"],
    )


# --- JSON robustness ---------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "not json at all",
    "```json\n{\"route\": \"chat\", \"should_reply\": true\n```",   # truncated
    "",
    "[1, 2, 3]",
    json.dumps({"route": "weird"}),
    json.dumps({"route": "task", "task": {"name": None}}),       # task without name
    json.dumps({"route": "task", "active_task_id": 999, "task": {}}),  # unknown task id
])
def test_bad_intent_output_degrades_to_chat(agents, raw):
    fake, a = agents
    fake.responders["fake-intent"] = lambda kw: raw
    result = _run(a)
    assert result["chat"]["content"] == "好呀\n我在听"
    assert not result["chat_failed"]
    assert "intent" in result["errors"]


def test_intent_json_wrapped_in_prose_is_recovered(agents):
    fake, a = agents
    fake.responders["fake-intent"] = lambda kw: "好的，结果如下：\n```json\n" + intent_json("silent") + "\n```"
    result = _run(a)
    assert result.get("should_reply") is False
    assert not fake.calls_for("fake-chat")


def test_intent_agent_exception_still_replies(agents):
    fake, a = agents

    def boom(kw):
        raise api_connection_error()
    fake.responders["fake-intent"] = boom
    result = _run(a)
    assert result["chat"]["content"]
    assert "intent" in result["errors"]


def test_chat_failure_is_reported_not_raised(agents):
    fake, a = agents

    def boom(kw):
        raise RuntimeError("upstream exploded")
    fake.responders["fake-chat"] = boom
    result = _run(a)
    assert result["chat_failed"] is True
    assert result["chat"]["content"] == ""


def test_performer_failure_marks_task_failed_and_chat_continues(agents, violetdb, monkeypatch):
    from tests.fake_mcp import campus_server, use
    use(monkeypatch, campus_server(agents=("performer",)))   # phase 9.2: a task needs a tool that could carry it out
    fake, a = agents
    fake.responders["fake-intent"] = lambda kw: intent_json("task")

    def boom(kw):
        raise RuntimeError("performer crashed")
    fake.responders["fake-performer"] = boom
    result = _run(a, "帮我点个外卖")
    assert result["chat"]["content"]
    tasks = violetdb.list_task_sessions()
    assert len(tasks) == 1 and tasks[0]["status"] == "failed"
    chat_call = fake.calls_for("fake-chat")[0]
    assert any("任务执行结果" in (m.get("content") or "") for m in chat_call["messages"])


def test_memory_failure_is_isolated(agents):
    fake, a = agents
    fake.responders["fake-memory"] = lambda kw: (_ for _ in ()).throw(RuntimeError("memory down"))
    result = _run(a)
    assert result["chat"]["content"] and "memory" in result["errors"]


# --- local tool robustness ---------------------------------------------------

def test_local_tool_exception_becomes_tool_error(agents):
    fake, a = agents
    script = iter([
        completion(tool_calls=[{"name": "get_task", "arguments": {"task_id": "not-a-number"}}]),
        completion(content=json.dumps({"task_status": "failed"})),
    ])
    fake.responders["fake-performer"] = lambda kw: next(script)
    result = a["performer"].run(json.dumps({"active_task_id": None}))
    tool_messages = [m for m in result["messages"] if m.get("role") == "tool"]
    assert tool_messages and "LOCAL_TOOL_ERROR" in tool_messages[0]["content"]


# --- shared agent state --------------------------------------------------------

def test_call_context_is_isolated_between_threads(monkeypatch):
    from ai.agents.base_agent import BaseAgent

    barrier = threading.Barrier(2, timeout=5)
    seen: dict[str, str] = {}
    fake = FakeLLM(default=lambda kw: kw["messages"][-1]["content"])
    install_fake_llm(monkeypatch, fake)

    class ProbeAgent(BaseAgent):
        model_env_key = "CHAT_LLM_MODEL"

        def build_context_messages(self, user_message):
            barrier.wait()   # both threads have set their call_context by now
            return [{"role": "user", "content": self._call_context.get("tag", "<none>")}]

    agent = ProbeAgent()

    def worker(tag):
        seen[tag] = agent.run("x", call_context={"tag": tag})["content"]

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {"A": "A", "B": "B"}
    assert agent._call_context == {}


# --- timeouts / retries / fallback -------------------------------------------

def test_real_client_has_explicit_timeout_and_no_sdk_retries(monkeypatch):
    from ai.agents.base_agent import BaseAgent
    from ai.agents import ChatAgent
    monkeypatch.setattr(BaseAgent, "client_factory", None)
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "42")
    agent = ChatAgent()
    client = agent._get_client(agent._config)
    assert client.max_retries == 0
    assert client.timeout.read == 42


def test_business_retry_recovers_transient_errors(agents, monkeypatch):
    fake, a = agents
    attempts = {"n": 0}

    def flaky(kw):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise api_connection_error()
        return "终于好了"
    fake.responders["fake-chat"] = flaky
    assert a["chat"].run("hi")["content"] == "终于好了"
    assert attempts["n"] == 3


def test_retry_respects_total_deadline(agents, monkeypatch):
    fake, a = agents
    monkeypatch.setenv("LLM_RETRY_BASE_DELAY", "5")
    monkeypatch.setenv("LLM_TOTAL_TIMEOUT_SECONDS", "2")
    fake.responders["fake-chat"] = lambda kw: (_ for _ in ()).throw(api_connection_error())
    start = time.monotonic()
    with pytest.raises(Exception):
        a["chat"].run("hi")
    assert time.monotonic() - start < 2, "should give up instead of sleeping past the deadline"
    assert len(fake.calls_for("fake-chat")) == 1


def test_fallback_model_used_when_primary_fails(monkeypatch, violetdb):
    monkeypatch.setenv("CHAT_LLM_FALLBACK_MODEL", "fake-chat-backup")
    monkeypatch.setenv("LLM_MAX_RETRY_ATTEMPTS", "1")
    fake = install_fake_llm(monkeypatch, FakeLLM({
        "fake-chat": lambda kw: (_ for _ in ()).throw(api_connection_error()),
        "fake-chat-backup": lambda kw: "备用模型回复",
    }))
    from ai.agents import ChatAgent
    assert ChatAgent().run("hi")["content"] == "备用模型回复"
    assert [c["model"] for c in fake.calls] == ["fake-chat", "fake-chat-backup"]


# --- proactive pipeline states ------------------------------------------------

def _proactive(a, ctx=None):
    from ai.chat import run_proactive_pipeline
    return run_proactive_pipeline(
        event_context=ctx or {"event_type": "scheduled_proactive_check", "allowed_tasks": {}},
        proactive_agent=a["proactive"], chat_agent=a["chat"], performer_agent=a["performer"],
    )


def test_proactive_garbage_json_is_skip(agents, violetdb):
    fake, a = agents
    fake.responders["fake-proactive"] = lambda kw: "sure! let me think..."
    result = _proactive(a)
    assert result["should_send"] is False
    assert violetdb.get_proactive_event(result["event_id"])["status"] == "skipped"


def test_proactive_agent_exception_records_failed_event(agents, violetdb):
    fake, a = agents
    fake.responders["fake-proactive"] = lambda kw: (_ for _ in ()).throw(RuntimeError("decision crashed"))
    result = _proactive(a)
    assert result["should_send"] is False and result["error"]
    assert violetdb.get_proactive_event(result["event_id"])["status"] == "failed"


def test_proactive_chat_exception_marks_planned_event_failed(agents, violetdb):
    fake, a = agents
    fake.responders["fake-proactive"] = lambda kw: json.dumps({"should_act": True, "action_type": "chat", "reason": "t"})
    fake.responders["fake-chat"] = lambda kw: (_ for _ in ()).throw(RuntimeError("chat crashed"))
    result = _proactive(a)
    assert result["should_send"] is False
    assert violetdb.get_proactive_event(result["event_id"])["status"] == "failed"
