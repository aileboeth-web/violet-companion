"""Natural recall queries through real handlers/pipeline; no model or network calls."""
import json

import pytest

from tests.fakes import FakeLLM, default_responders, install_fake_llm, intent_json
from tests.test_phase2_scenarios import make_world, say, script, tool


@pytest.fixture
def agents(monkeypatch, violetdb):
    install_fake_llm(monkeypatch, FakeLLM(default_responders()))
    from ai.agents import IntentAgent, MemoryAgent
    return {"intent": IntentAgent(), "memory": MemoryAgent()}


@pytest.mark.parametrize("agent", ["intent", "memory"])
@pytest.mark.parametrize("query,content", [
    ("上次说的答辩你还记得吗", "用户已完成天文观测课程答辩，过程挺顺利。"),
    ("之前提到的吉他还记得吗？", "周末学会了吉他的第一首曲子。"),
    ("用户先前聊过的服务器你记得吗", "公司服务器故障已经修好了。"),
])
def test_natural_query_returns_completed_target(agents, violetdb, agent, query, content):
    target = violetdb.add_event_memory(content, category="event", kind="event", progress="done")
    result = agents[agent]._tool_query_memory({"query": query})
    assert [(m["id"], m["content"], m["progress"]) for m in result["memories"]] == [(target, content, "done")]


def test_original_question_enters_chat_as_actual_completed_event(make_world):
    w = make_world()
    content = "用户已完成天文观测课程答辩，过程挺顺利。"
    target = w.db.add_event_memory(content, category="event", kind="event", progress="done")
    question = "上次说的答辩你还记得吗"
    w.llm.responders["fake-intent"] = script(tool("query_memory", query=question), final=intent_json())
    w.llm.responders["fake-memory"] = script(tool("query_memory", query=question))
    observed = []
    def chat(request):
        system = "\n".join(m.get("content", "") for m in request["messages"] if m["role"] == "system")
        lines = [line for line in system.splitlines() if line.startswith(f"- id={target}; ")]
        assert any(content in line and "已结束" in line for line in lines)
        observed.extend(lines)
        return "记得，答辩已经顺利结束了。"
    w.llm.responders["fake-chat"] = chat
    say(w, question)
    assert observed
    assert any("顺利结束" in (r["content"] or "") for r in w.db.get_recent_messages())
    memory_request = w.llm.calls_for("fake-memory")[-1]
    results = [json.loads(m["content"])["memories"] for m in memory_request["messages"] if m["role"] == "tool"]
    assert any(any(m["id"] == target and m["progress"] == "done" for m in rows) for rows in results)


@pytest.mark.parametrize("agent", ["intent", "memory"])
def test_empty_recall_wrapper_does_not_match_any_memory(agents, violetdb, agent):
    violetdb.add_event_memory("你还记得吗", kind="background")
    assert agents[agent]._tool_query_memory({"query": "你还记得吗？"})["memories"] == []


def test_recall_keeps_category_limit_and_explicit_threshold(agents, violetdb):
    for category in ("event", "event", "preference"):
        violetdb.add_event_memory("用户的吉他已经修好了", category=category, kind="event", progress="done")
    query = {"query": "之前提到的吉他还记得吗", "category": "event", "limit": 1}
    for agent in agents.values():
        rows = agent._tool_query_memory(query)["memories"]
        assert len(rows) == 1 and rows[0]["category"] == "event"
    assert agents["memory"]._tool_query_memory({**query, "min_similarity": .99})["memories"] == []
    assert agents["intent"]._tool_query_memory({**query, "category": "missing"})["memories"] == []


def test_disabled_deleted_and_expired_semantics_unchanged(agents, violetdb):
    disabled = violetdb.add_event_memory("吉他已经修好了", kind="event", progress="done")
    deleted = violetdb.add_event_memory("吉他已经修好了", kind="event", progress="done")
    expired = violetdb.add_event_memory("吉他已经修好了", kind="event", progress="done")
    violetdb.execute("UPDATE event_memories SET use_in_context=0 WHERE id=?", (disabled,))
    violetdb.execute("UPDATE event_memories SET status='deleted' WHERE id=?", (deleted,))
    violetdb.execute("UPDATE event_memories SET status='expired',valid_until='2000-01-01T00:00:00+08:00' WHERE id=?", (expired,))
    query = {"query": "上次说的吉他你还记得吗"}
    assert [m["id"] for m in agents["intent"]._tool_query_memory(query)["memories"]] == [expired]
    assert {m["id"] for m in agents["memory"]._tool_query_memory(query)["memories"]} == {disabled, expired}


@pytest.mark.parametrize("query", ["服务器", "server outage", "我不记得密码", "吉他谱", "记得交作业", "记得", "我不记得"])
def test_ordinary_queries_and_write_similarity_unchanged(agents, violetdb, query):
    target = violetdb.add_event_memory(query, category="event", kind="event")
    assert [m["id"] for m in agents["intent"]._tool_query_memory({"query": query})["memories"]] == [target]
    assert violetdb.text_similarity(query, query) == 1


def test_unrelated_topic_is_not_returned(agents, violetdb):
    violetdb.add_event_memory("上次说的事情已经完成，用户很高兴。", kind="event", progress="done")
    for agent in agents.values():
        assert agent._tool_query_memory({"query": "上次说的吉他你还记得吗"})["memories"] == []
