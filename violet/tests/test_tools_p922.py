"""Phase 9.2.2: tool_choice for one vs several planned tools (offline; fake SDK, the fake campus MCP server via stdio).

The real A-E comparison through the current relay (local/p921/probe-results.jsonl): two tools + "required" -> HTTP 502
(A, D, E, reproducible); two tools + "auto" -> 200 with both tool calls (B); one tool + "required" -> 200 (C). Where
in the relay chain the 502 comes from is not known. Violet now forces the first call only when exactly one planned tool
is offered; with several it uses "auto", and when the model answers having looked up only some (or none) of them it
is told once which are missing: call them, or say that part was not looked up -- never fill it in."""

from __future__ import annotations

import pytest

from tests.fake_mcp import campus_server, not_started
from tests.fakes import completion, intent_json
from tests.test_phase2_scenarios import make_world, say  # noqa: F401  (make_world: fixture)
from tests.test_tools_p92 import fresh_registry, offered, system, world  # noqa: F401  (fresh_registry: fixture)

SCHEDULE, WEATHER, SEARCH = "campus__get_course_schedule", "campus__get_weather", "campus__search_web"
QUESTION = "周三下午要是没课，我想去西湖边走走。帮我查查那天下午有没有课，天气适不适合出门？"
ARGS = {SCHEDULE: {"day": "周三"}, WEATHER: {"city": "杭州"}, SEARCH: {"query": "西湖 周三"}}


def asks_for(*tools):
    return lambda kw: intent_json("chat", tool_query={"needed": True, "tools": list(tools), "reason": "周三下午"})


def tool_results(kw) -> list[str]:
    return [m["content"] for m in kw["messages"] if m["role"] == "tool"]


def reminded(kw) -> bool:
    return any(m["role"] == "system" and (m.get("content") or "").startswith("【还没查】") for m in kw["messages"])


def call(*names):
    return completion(tool_calls=[{"name": n, "arguments": ARGS[n]} for n in names])


def honest_answer(kw) -> str:
    text = "\n".join(tool_results(kw))
    parts = ["周三下午有体育课" if "体育" in text else "课表这次没查到",
             "那天可能下小雨" if "小雨" in text else "天气这次没查到"]
    return "；".join(parts)


def mcp_calls() -> dict[str, int]:
    from ai.mcp import get_health_snapshot
    return {h["server"]: h["total_calls"] for h in get_health_snapshot()}


def test_one_planned_tool_keeps_required(make_world, monkeypatch):
    chat = lambda kw: call(SCHEDULE) if not tool_results(kw) else honest_answer(kw)
    w, chats = world(make_world, monkeypatch, campus_server(), intent=asks_for(SCHEDULE), chat=chat)
    say(w, "我周三下午有课吗")
    assert offered(chats[0]) == [SCHEDULE] and chats[0]["tool_choice"] == "required"
    assert [kw["tool_choice"] for kw in chats[1:]] == ["auto"] and not any(reminded(kw) for kw in chats)


@pytest.mark.parametrize("tools", [(SCHEDULE, WEATHER), (SCHEDULE, WEATHER, SEARCH)])
def test_two_or_more_planned_tools_start_with_auto(make_world, monkeypatch, tools):
    chat = lambda kw: call(*tools) if not tool_results(kw) else honest_answer(kw)
    w, chats = world(make_world, monkeypatch, campus_server(), intent=asks_for(*tools), chat=chat)
    say(w, QUESTION)
    assert sorted(offered(chats[0])) == sorted(tools) and chats[0]["tool_choice"] == "auto"
    assert len(chats) == 2 and len(tool_results(chats[1])) == len(tools) and not reminded(chats[1])
    assert w.client.sent_texts[-1] == "周三下午有体育课；那天可能下小雨"


def test_two_tools_one_after_the_other_need_no_reminder(make_world, monkeypatch):
    def chat(kw):
        done = tool_results(kw)
        return call(SCHEDULE) if not done else call(WEATHER) if len(done) == 1 else honest_answer(kw)
    w, chats = world(make_world, monkeypatch, campus_server(), intent=asks_for(SCHEDULE, WEATHER), chat=chat)
    say(w, QUESTION)
    assert [kw["tool_choice"] for kw in chats] == ["auto", "auto", "auto"] and not any(reminded(kw) for kw in chats)
    assert w.client.sent_texts[-1] == "周三下午有体育课；那天可能下小雨"


def test_only_one_of_two_looked_up_is_named_and_the_other_part_is_not_made_up(make_world, monkeypatch):
    """The model calls the timetable only and wants to answer: it is told once that the weather is still missing; it
    still does not look it up -> the reply says so. The weather tool never ran and no weather data reached Chat."""
    chat = lambda kw: call(SCHEDULE) if not tool_results(kw) else honest_answer(kw)
    w, chats = world(make_world, monkeypatch, campus_server(), intent=asks_for(SCHEDULE, WEATHER), chat=chat)
    say(w, QUESTION)
    assert len(chats) == 3 and not reminded(chats[1]) and reminded(chats[2])        # one reminder, one extra call
    note = next(m["content"] for m in chats[2]["messages"] if (m.get("content") or "").startswith("【还没查】"))
    assert WEATHER in note and SCHEDULE not in note and "绝不编造" in note and "一项都还没查" not in note
    assert all("小雨" not in r for r in tool_results(chats[2]))                     # nothing about the weather
    assert w.client.sent_texts[-1] == "周三下午有体育课；天气这次没查到"
    assert mcp_calls()["campus"] == 1


def test_nothing_looked_up_is_reminded_once_then_the_lookup_happens(make_world, monkeypatch):
    def chat(kw):
        if not reminded(kw):
            return "周三应该没课吧，天气不错"                               # the guess the reminder stops
        return call(SCHEDULE, WEATHER) if not tool_results(kw) else honest_answer(kw)
    w, chats = world(make_world, monkeypatch, campus_server(), intent=asks_for(SCHEDULE, WEATHER), chat=chat)
    say(w, QUESTION)
    assert "一项都还没查" in next(m["content"] for m in chats[1]["messages"] if (m.get("content") or "").startswith("【还没查】"))
    assert w.client.sent_texts[-1] == "周三下午有体育课；那天可能下小雨"
    assert "应该没课" not in "".join(w.client.sent_texts)


def test_nothing_looked_up_and_still_declined_ends_after_one_reminder(make_world, monkeypatch):
    chat = lambda kw: honest_answer(kw)                                       # never calls a tool
    w, chats = world(make_world, monkeypatch, campus_server(), intent=asks_for(SCHEDULE, WEATHER), chat=chat)
    say(w, QUESTION)
    assert len(chats) == 2 and reminded(chats[1])
    assert w.client.sent_texts[-1] == "课表这次没查到；天气这次没查到" and mcp_calls()["campus"] == 0


def test_a_failed_tool_counts_as_looked_up_and_is_reported_as_failed(make_world, monkeypatch):
    chat = lambda kw: call(SCHEDULE, WEATHER) if not tool_results(kw) else honest_answer(kw)
    w, chats = world(make_world, monkeypatch, campus_server(mode="error"), intent=asks_for(SCHEDULE, WEATHER),
                     chat=chat)
    say(w, QUESTION)
    assert len(chats) == 2 and not reminded(chats[1])
    assert all('"status": "tool_error"' in r for r in tool_results(chats[1]))
    assert w.client.sent_texts[-1] == "课表这次没查到；天气这次没查到"


def test_an_unreachable_server_offers_nothing_and_reminds_nothing(make_world, monkeypatch):
    w, chats = world(make_world, monkeypatch, not_started(), intent=asks_for(SCHEDULE, WEATHER))
    say(w, QUESTION)
    assert len(chats) == 1 and not offered(chats[0]) and chats[0].get("tool_choice") is None
    assert "需要用到的工具现在连不上" in system(chats[0]) and w.db.list_task_sessions() == []


def test_small_talk_offers_no_external_tool_and_makes_one_call(make_world, monkeypatch):
    w, chats = world(make_world, monkeypatch, campus_server())
    say(w, "今天好累啊，抱抱")
    assert len(chats) == 1 and not offered(chats[0]) and not reminded(chats[0]) and mcp_calls().get("campus", 0) == 0


def test_the_performer_and_proactive_paths_get_no_reminder():
    from ai.agents import ChatAgent, PerformerAgent
    from ai.agents.base_agent import _CALL_CONTEXT
    token = _CALL_CONTEXT.set({"tool_plan": {"mode": "performer", "tools": [SCHEDULE]}, "_planned_offered": [SCHEDULE]})
    try:
        assert ChatAgent()._lookup_reminder([]) is None and PerformerAgent()._lookup_reminder([]) is None
    finally:
        _CALL_CONTEXT.reset(token)
