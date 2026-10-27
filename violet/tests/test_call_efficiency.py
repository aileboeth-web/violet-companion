"""Phase 7 B.2 part 2: fewer redundant model calls without losing memory accuracy.

M4a  a background memory job ends right after its writes -- only when the model signals it is done
     (memory_done in the same step) and every write of that step succeeded; otherwise the loop goes on.
M4b  IntentAgent is not offered query_memory while no event memory could be found.
M4c  a confirmed profile name hides old nickname core memories from Chat (display only).
All offline: scripted fake LLM, temp database."""

from __future__ import annotations

import json

import pytest

from tests.fakes import FakeILinkClient, FakeLLM, completion, default_responders, install_fake_llm, intent_json

USER = "我在星河大学读大二，学的是天文"


@pytest.fixture()
def llm(monkeypatch, violetdb):
    return install_fake_llm(monkeypatch, FakeLLM(default_responders("好呀")))


def _script(llm, model, *steps):
    queue = list(steps)
    llm.responders[model] = lambda kw: queue.pop(0) if queue else json.dumps({"should_save": False, "reason": "end"})
    return queue


def _tools_offered(call) -> list[str]:
    return [t["function"]["name"] for t in (call.get("tools") or [])]


def _memory_run(text=USER, background=True, reply="嗯嗯"):
    from ai.agents import MemoryAgent
    from ai.memory_jobs import build_memory_input
    context = {"turn_id": "t-1", "user_message": text, "assistant_reply": reply}
    if background:
        context["background_job"] = True
    return MemoryAgent().run(build_memory_input(text, reply), call_context=context)


def _fact(key, value, evidence="学的是天文"):
    return {"name": "remember_profile_fact", "arguments": {"key": key, "value": value, "evidence": evidence}}


DONE = {"name": "memory_done", "arguments": {}}


# ---------------------------------------------------------------------------
# M4a: MemoryAgent
# ---------------------------------------------------------------------------

def test_successful_writes_with_done_signal_end_without_a_summary_call(llm, violetdb):
    _script(llm, "fake-memory", completion(tool_calls=[_fact("study.major", "天文"),
                                                       _fact("study.school", "星河大学", "我在星河大学读大二"), DONE]))
    result = _memory_run()
    calls = llm.calls_for("fake-memory")
    assert len(calls) == 1 and result["finished_after_tools"] is True and result["content"] == ""
    assert "memory_done" in _tools_offered(calls[0])
    assert any("【结束方式】" in (m.get("content") or "") for m in calls[0]["messages"])
    values = {r["fact_key"]: r["value"] for r in violetdb.fetch_all("SELECT fact_key, value FROM user_profile_facts WHERE status = 'active'")}
    assert values == {"study.major": "天文", "study.school": "星河大学"}


@pytest.mark.parametrize("bad_step", [
    _fact("study.major", "天文", evidence="不在原话里的证据"),            # -> candidate (unconfirmed)
    _fact("no.such.key", "x"),                                         # -> rejected
    {"name": "save_memory", "arguments": {"summary": ""}},             # -> ok False (empty)
    {"name": "update_core_memory", "arguments": {"memory_id": 999999, "content": "x"}},   # handler raises
    {"name": "create_commitment", "arguments": {"content": "提醒", "evidence": "没说过", "due_time": "2099-01-01T09:00"}},
])
def test_any_failed_or_unconfirmed_write_keeps_the_loop(llm, violetdb, bad_step):
    _script(llm, "fake-memory",
            completion(tool_calls=[_fact("study.school", "星河大学", "我在星河大学读大二"), bad_step, DONE]),
            completion(content=json.dumps({"should_save": True, "reason": "fixed"})))
    result = _memory_run()
    calls = llm.calls_for("fake-memory")
    assert len(calls) == 2 and not result.get("finished_after_tools")
    tool_messages = [m for m in calls[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 3                                   # the model sees every result, failures included


def test_needs_decision_keeps_the_loop(llm, violetdb):
    violetdb.save_event_memory(content="用户下周三有天文观测课程答辩", category="event", kind="event", progress="planned")
    _script(llm, "fake-memory",
            completion(tool_calls=[{"name": "save_memory", "arguments": {
                "summary": "用户下周三有天文观测课程答辩，有点紧张", "kind": "event", "progress": "planned"}}, DONE]),
            completion(content="ok"))
    _memory_run("下周三有个天文观测课程的答辩，有点紧张")
    calls = llm.calls_for("fake-memory")
    first_result = json.loads(next(m for m in calls[1]["messages"] if m.get("role") == "tool")["content"])
    assert first_result["action"] == "needs_decision" and len(calls) == 2


def test_query_mixed_with_writes_never_ends_early(llm, violetdb):
    _script(llm, "fake-memory",
            completion(tool_calls=[{"name": "query_memory", "arguments": {"query": "天文"}},
                                   _fact("study.major", "天文"), DONE]),
            completion(content="ok"))
    _memory_run()
    assert len(llm.calls_for("fake-memory")) == 2


def test_writes_without_the_done_signal_keep_the_old_loop(llm, violetdb):
    """A model that writes in two separate steps keeps both writes (nothing is cut off)."""
    _script(llm, "fake-memory",
            completion(tool_calls=[_fact("study.school", "星河大学", "我在星河大学读大二")]),
            completion(tool_calls=[_fact("study.major", "天文")]),
            completion(content="ok"))
    result = _memory_run()
    assert len(llm.calls_for("fake-memory")) == 3 and not result.get("finished_after_tools")
    keys = {r["fact_key"] for r in violetdb.fetch_all("SELECT fact_key FROM user_profile_facts WHERE status = 'active'")}
    assert keys == {"study.school", "study.major"}


def test_done_signal_alone_after_a_query_step_is_honoured(llm, violetdb):
    _script(llm, "fake-memory",
            completion(tool_calls=[{"name": "query_memory", "arguments": {"query": "天文"}}]),
            completion(tool_calls=[_fact("study.major", "天文"), DONE]))
    result = _memory_run()
    assert len(llm.calls_for("fake-memory")) == 2 and result["finished_after_tools"] is True


def test_synchronous_memory_runs_are_unchanged(llm, violetdb):
    """No background flag: memory_done is not offered, and calling it anyway is an unknown tool."""
    _script(llm, "fake-memory",
            completion(tool_calls=[_fact("study.major", "天文"), DONE]),
            completion(content="ok"))
    result = _memory_run(background=False)
    calls = llm.calls_for("fake-memory")
    assert "memory_done" not in _tools_offered(calls[0])
    assert not any("【结束方式】" in (m.get("content") or "") for m in calls[0]["messages"])
    assert len(calls) == 2 and not result.get("finished_after_tools")
    done_result = json.loads([m for m in calls[1]["messages"] if m.get("role") == "tool"][-1]["content"])
    assert done_result["error_code"] == "UNKNOWN_TOOL"


def test_switch_off_restores_the_old_behaviour(llm, violetdb, monkeypatch):
    monkeypatch.setenv("VIOLET_MEMORY_FINISH_SIGNAL", "0")
    _script(llm, "fake-memory", completion(tool_calls=[_fact("study.major", "天文"), DONE]), completion(content="ok"))
    _memory_run()
    calls = llm.calls_for("fake-memory")
    assert "memory_done" not in _tools_offered(calls[0]) and len(calls) == 2


@pytest.mark.parametrize("name,payload,expected", [
    ("remember_profile_fact", {"ok": True, "action": "created"}, True),
    ("remember_profile_fact", {"ok": False, "action": "created"}, False),        # ok=false always wins
    ("remember_profile_fact", {"ok": True, "action": "candidate"}, False),
    ("retract_profile_fact", {"action": "retracted"}, True),                       # results without "ok"
    ("retract_profile_fact", {"action": "rejected"}, False),
    ("save_memory", {"ok": False, "action": "needs_decision"}, False),
    ("query_memory", {"memories": []}, False),                                    # a read is never a final write
    ("memory_done", {"ok": True, "action": "done_requested"}, False),
])
def test_write_success_is_decided_by_an_explicit_whitelist(name, payload, expected):
    from ai.agents.memory_agent import write_step_succeeded
    assert write_step_succeeded(name, json.dumps(payload)) is expected
    assert write_step_succeeded("save_memory", "not json") is False


def test_only_memory_agent_may_end_after_tools():
    from ai.agents import ChatAgent, IntentAgent, MemoryAgent, PerformerAgent, ProactiveAgent
    from ai.agents.base_agent import BaseAgent
    step = [{"name": "memory_done", "result": json.dumps({"ok": True, "action": "done_requested"}), "denied": False}]
    for cls in (ChatAgent, IntentAgent, PerformerAgent, ProactiveAgent):
        assert cls._should_finish_after_tools is BaseAgent._should_finish_after_tools
    assert BaseAgent._should_finish_after_tools(object(), step) is False
    assert MemoryAgent._should_finish_after_tools is not BaseAgent._should_finish_after_tools


def test_chat_agent_always_answers_after_its_tools(llm, violetdb):
    from ai.agents import ChatAgent
    violetdb.add_event_memory("用户家的狗叫豆豆", category="event")
    llm.responders["fake-chat"] = (lambda q: lambda kw: q.pop(0))(
        [completion(tool_calls=[{"name": next(n for n in _tools_offered({"tools": ChatAgent()._tools}) if n),
                                 "arguments": {"query": "豆豆"}}]),
         completion(content="豆豆还好吗")])
    result = ChatAgent().run("你还记得我家狗吗", call_context={"background_job": True})
    assert result["content"] == "豆豆还好吗" and not result.get("finished_after_tools")


def test_background_flag_comes_from_the_memory_worker(llm, violetdb, monkeypatch):
    """End to end: the runtime's MemoryWorker marks its jobs; one memory call instead of two."""
    from ai.agents import ChatAgent, IntentAgent, MemoryAgent, PerformerAgent
    from violet_runtime import VioletRuntime, MessageSender
    monkeypatch.setenv("DEBOUNCE_NORMAL", "0.05")
    monkeypatch.setenv("DEBOUNCE_EXTRA", "0.1")
    _script(llm, "fake-memory", completion(tool_calls=[_fact("study.major", "天文"), DONE]))
    client = FakeILinkClient()
    agents = {"intent": IntentAgent(), "chat": ChatAgent(), "memory": MemoryAgent(), "performer": PerformerAgent()}
    rt = VioletRuntime(client, violetdb, agents, sender=MessageSender(client, violetdb, simulate_typing=False, sleep=lambda s: None))
    rt.start_worker()
    try:
        rt.ingest_text(USER)
        assert rt.wait_idle() and rt.wait_memory_idle()
    finally:
        rt.stop(timeout=2)
    assert len(llm.calls_for("fake-memory")) == 1
    assert violetdb.fetch_one("SELECT value FROM user_profile_facts WHERE fact_key = 'study.major'")["value"] == "天文"
    assert violetdb.fetch_one("SELECT status FROM memory_jobs")["status"] == "done"


# ---------------------------------------------------------------------------
# M4b: IntentAgent
# ---------------------------------------------------------------------------

def _intent_run(llm, text="我在星河大学读大二", script=None):
    from ai.agents import IntentAgent
    if script is not None:
        llm.responders["fake-intent"] = (lambda q: lambda kw: q.pop(0) if q else intent_json("chat"))(list(script))
    return IntentAgent().run(text)


def test_empty_event_store_offers_no_search(llm, violetdb):
    _intent_run(llm)
    calls = llm.calls_for("fake-intent")
    assert len(calls) == 1 and "query_memory" not in _tools_offered(calls[0]) and calls[0].get("tools") is None
    assert any("没有可查询的事件记忆" in (m.get("content") or "") for m in calls[0]["messages"])


def test_a_searchable_event_keeps_the_tool_and_it_finds_it(llm, violetdb):
    violetdb.add_event_memory("用户下周三有天文观测课程答辩", category="event")
    _intent_run(llm, "答辩准备得差不多了", script=[
        completion(tool_calls=[{"name": "query_memory", "arguments": {"query": "答辩 观测"}}]),
        completion(content=intent_json("chat"))])
    calls = llm.calls_for("fake-intent")
    assert "query_memory" in _tools_offered(calls[0])
    assert not any("没有可查询的事件记忆" in (m.get("content") or "") for m in calls[0]["messages"])
    found = json.loads(next(m for m in calls[1]["messages"] if m.get("role") == "tool")["content"])
    assert [m["content"] for m in found["memories"]] == ["用户下周三有天文观测课程答辩"]


def test_deleted_and_disabled_events_do_not_count_but_expired_ones_do(llm, violetdb):
    a = violetdb.add_event_memory("用户下周三有天文观测课程答辩", category="event")
    violetdb.retract_event_memory(a)
    b = violetdb.add_event_memory("用户周五要交实验报告", category="event")
    violetdb.execute("UPDATE event_memories SET use_in_context = 0 WHERE id = ?", (b,))
    _intent_run(llm)
    assert "query_memory" not in _tools_offered(llm.calls_for("fake-intent")[-1])
    violetdb.execute("UPDATE event_memories SET status = 'expired' WHERE id = ?", (b,))
    violetdb.execute("UPDATE event_memories SET use_in_context = 1 WHERE id = ?", (b,))
    _intent_run(llm)
    assert "query_memory" in _tools_offered(llm.calls_for("fake-intent")[-1])


@pytest.mark.parametrize("status", ["active", "expired", "archived", "deleted"])
@pytest.mark.parametrize("use_in_context", [0, 1])
def test_visibility_matches_the_search_filter_exactly(violetdb, status, use_in_context):
    memory_id = violetdb.add_event_memory("用户周五要交实验报告", category="event")
    violetdb.execute("UPDATE event_memories SET status = ?, use_in_context = ? WHERE id = ?",
                  (status, use_in_context, memory_id))
    searchable = bool(violetdb.search_event_memories("实验报告", min_similarity=0.0))
    assert violetdb.has_searchable_event_memories() is searchable
    assert searchable is (status != "deleted" and use_in_context == 1)


def test_events_added_later_are_searchable_again(llm, violetdb):
    _intent_run(llm)
    assert "query_memory" not in _tools_offered(llm.calls_for("fake-intent")[-1])
    violetdb.add_event_memory("用户下周三有天文观测课程答辩", category="event")
    _intent_run(llm)
    assert "query_memory" in _tools_offered(llm.calls_for("fake-intent")[-1])


def test_profile_facts_alone_do_not_need_the_query_tool_and_still_reach_chat(llm, violetdb):
    from ai.context_engine import ContextEngine
    from ai.profile_store import Profile
    Profile(violetdb).facts.remember("study.school", "星河大学", evidence="我在星河大学读大二", user_message=USER)
    _intent_run(llm)
    assert "query_memory" not in _tools_offered(llm.calls_for("fake-intent")[-1])
    packet = ContextEngine(violetdb).build("chat", "我在学校挺好的")
    assert any("学校：星河大学" in s for s in packet.sections)


def test_intent_switch_off_offers_the_tool_on_an_empty_store(llm, violetdb, monkeypatch):
    monkeypatch.setenv("VIOLET_INTENT_HIDE_EMPTY_MEMORY_TOOL", "0")
    _intent_run(llm)
    assert "query_memory" in _tools_offered(llm.calls_for("fake-intent")[-1])


# ---------------------------------------------------------------------------
# M4c: nickname shown once
# ---------------------------------------------------------------------------

def _chat_system(violetdb, text="早"):
    from ai.context_engine import ContextEngine
    return "\n".join(ContextEngine(violetdb).build("chat", text).sections)


def test_confirmed_profile_name_hides_the_legacy_nickname_without_touching_data(violetdb):
    from ai.profile_store import Profile
    violetdb.upsert_core_memory("nickname_preference", "nickname", "用户希望被称呼为“小满”。")
    violetdb.upsert_core_memory("nickname", "relationship", "叫她小满就行")                 # nickname by key only
    violetdb.upsert_core_memory("call_me", "nickname", "称呼偏好：小满")                    # nickname by category only
    violetdb.upsert_core_memory("reply_style", "reply_style", "用户喜欢短句回复")
    before = violetdb.fetch_all("SELECT * FROM core_memories ORDER BY id")
    assert "用户希望被称呼为“小满”" in _chat_system(violetdb)                       # no profile name yet: shown
    Profile(violetdb).facts.remember("address_name", "小满", evidence="以后叫我小满就好", user_message="以后叫我小满就好")
    system = _chat_system(violetdb, "小满来啦")
    assert "希望被称呼为：小满" in system and "用户希望被称呼为“小满”" not in system
    assert "叫她小满就行" not in system and "称呼偏好：小满" not in system
    assert "用户喜欢短句回复" in system                                            # other legacy items stay
    assert violetdb.fetch_all("SELECT * FROM core_memories ORDER BY id") == before    # display only


def test_renamed_user_sees_no_contradiction(violetdb):
    from ai.profile_store import Profile
    facts = Profile(violetdb).facts
    violetdb.upsert_core_memory("nickname_preference", "nickname", "用户希望被称呼为“小满”。")
    facts.remember("address_name", "小满", evidence="以后叫我小满就好", user_message="以后叫我小满就好")
    facts.remember("address_name", "阿澈", evidence="以后叫我阿澈吧", user_message="以后叫我阿澈吧", correction=True)
    system = _chat_system(violetdb, "阿澈在吗")
    assert "希望被称呼为：阿澈" in system and "小满" not in system


def test_legacy_nickname_stays_without_a_usable_profile_name_or_with_the_switch_off(violetdb, monkeypatch):
    from ai.profile_store import Profile
    violetdb.upsert_core_memory("nickname_preference", "nickname", "用户希望被称呼为“小满”。")
    fact = Profile(violetdb).facts.remember("address_name", "小满", evidence="以后叫我小满就好",
                                         user_message="以后叫我小满就好")["fact"]
    monkeypatch.setenv("VIOLET_CONTEXT_HIDE_LEGACY_NICKNAME", "0")
    assert "用户希望被称呼为“小满”" in _chat_system(violetdb)
    monkeypatch.setenv("VIOLET_CONTEXT_HIDE_LEGACY_NICKNAME", "1")
    Profile(violetdb).facts.set_enabled(fact["id"], False)
    system = _chat_system(violetdb)
    assert "用户希望被称呼为“[已停用的信息]”" in system and "小满" not in system   # shown again, value still masked


def test_memory_agent_still_sees_existing_core_memories(llm, violetdb):
    from ai.agents import MemoryAgent
    from ai.agents.base_agent import _CALL_CONTEXT
    from ai.profile_store import Profile
    violetdb.upsert_core_memory("nickname_preference", "nickname", "用户希望被称呼为“小满”。")
    Profile(violetdb).facts.remember("address_name", "小满", evidence="以后叫我小满就好", user_message="以后叫我小满就好")
    token = _CALL_CONTEXT.set({})
    try:
        messages = MemoryAgent().build_context_messages("用户说：早")
    finally:
        _CALL_CONTEXT.reset(token)
    assert any("nickname_preference" in (m.get("content") or "") for m in messages)   # "切勿重复存储" still works
