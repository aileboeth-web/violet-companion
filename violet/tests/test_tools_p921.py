"""Phase 9.2.1: what the real test p92-tools-1 left open (offline; fake SDK, the fake campus MCP server as a stdio child).

#11  Chat was offered two tools (tool_choice=required) and the relay answered HTTP 502 three times; no MCP call ran.
     Proven here: the request Violet builds for two tools, and that Chat runs two tool calls -- in one response or one
     after the other -- and answers from both results; what a 502 does (retries, backoff, fallback reply, memory).
     Whether the relay / upstream rejects that request shape is NOT provable offline (docs/handoff/log, p921).
#9   A switched-off tool was reported to Chat as "not configured": Intent's catalog never listed it, so Intent could only
     write tools=[] + missing. Fixed: the catalog lists switched-off tools and the full names of unreachable ones."""

from __future__ import annotations

import json
import re

import httpx
import openai
import pytest

from tests.fake_mcp import campus_server, not_started
from tests.fakes import completion, intent_json
from tests.test_phase2_scenarios import make_world  # noqa: F401  (fixture)
from tests.test_tools_p92 import fresh_registry, offered, system, world  # noqa: F401  (fresh_registry: fixture)

QUESTION = "周三下午要是没课，我想去西湖边走走。帮我查查那天下午有没有课，天气适不适合出门？"
BOTH = ("campus__get_course_schedule", "campus__get_weather")


def asks_for_both(kw):
    return intent_json("chat", tool_query={"needed": True, "tools": list(BOTH), "reason": "周三下午的课和杭州的天气"})


def tool_messages(kw):
    return [m for m in kw["messages"] if m["role"] == "tool"]


def answer_from(kw):
    results = "\n".join(m["content"] for m in tool_messages(kw))
    has_class = "体育" in results
    rain = "小雨" in results
    return f"周三下午{'有体育课，15:50 开始' if has_class else '没课'}；{'那天可能下小雨，记得带伞' if rain else '天气还行'}"


def test_two_tools_in_one_response_are_both_run_and_the_reply_uses_both(make_world, monkeypatch):
    from tests.test_phase2_scenarios import say

    def parallel(kw):
        if not tool_messages(kw):
            return completion(tool_calls=[{"name": BOTH[0], "arguments": {"day": "周三"}},
                                          {"name": BOTH[1], "arguments": {"city": "杭州", "date": "2026-11-04"}}])
        return answer_from(kw)
    w, chats = world(make_world, monkeypatch, campus_server(), intent=asks_for_both, chat=parallel)
    say(w, QUESTION)
    first, second = chats[0], chats[1]
    # the request of p92-tools-1 #11 (both tools, the plan's note); phase 9.2.2: two tools -> "auto", not "required"
    assert sorted(offered(first)) == sorted(BOTH) and first["tool_choice"] == "auto"
    assert set(first) <= {"model", "messages", "tools", "tool_choice", "temperature"}
    assert "parallel_tool_calls" not in first
    assert second["tool_choice"] == "auto" and len(tool_messages(second)) == 2
    calls = next(m for m in second["messages"] if m["role"] == "assistant" and m.get("tool_calls"))["tool_calls"]
    assert [c["id"] for c in calls] == [m["tool_call_id"] for m in tool_messages(second)]   # each result answers its call
    assert "体育" in tool_messages(second)[0]["content"] and "小雨" in tool_messages(second)[1]["content"]
    assert w.client.sent_texts[-1].startswith("周三下午有体育课") and "带伞" in w.client.sent_texts[-1]
    assert w.db.list_task_sessions() == []


def test_two_tools_one_after_the_other_work_too(make_world, monkeypatch):
    from tests.test_phase2_scenarios import say

    def sequential(kw):
        done = tool_messages(kw)
        if not done:
            return completion(tool_calls=[{"name": BOTH[0], "arguments": {"day": "周三"}}])
        if len(done) == 1:
            return completion(tool_calls=[{"name": BOTH[1], "arguments": {"city": "杭州"}}])
        return answer_from(kw)
    w, chats = world(make_world, monkeypatch, campus_server(), intent=asks_for_both, chat=sequential)
    say(w, QUESTION)
    assert [kw["tool_choice"] for kw in chats[:3]] == ["auto", "auto", "auto"]          # phase 9.2.2: two tools
    assert all(sorted(offered(kw)) == sorted(BOTH) for kw in chats[:3])
    assert w.client.sent_texts[-1].startswith("周三下午有体育课") and "带伞" in w.client.sent_texts[-1]


def bad_gateway():
    response = httpx.Response(502, request=httpx.Request("POST", "http://relay.invalid/v1/chat/completions"))
    return openai.InternalServerError(
        "Error code: 502 - {'error': {'message': 'Upstream service temporarily unavailable', 'type': 'upstream_error'}}",
        response=response, body={"error": {"message": "Upstream service temporarily unavailable"}})


def test_a_502_is_tried_three_times_with_backoff_then_the_fallback_reply_and_memory_still_runs(make_world, monkeypatch):
    """p92-tools-1 #11 as it happened: three identical requests (05:42:05 / :09 / :16 = 3 s and 6 s waits), the canned
    fallback reply, no MCP call, and the memory job ran on the user's words with the fallback as Violet's reply.
    Phase 9.2.2: two tools now go out with "auto"; this keeps the 5xx path itself (one planned tool -> "required")."""
    import ai.agents.base_agent as base
    from ai.mcp import get_health_snapshot
    from tests.test_phase2_scenarios import say
    for key in ("LLM_MAX_RETRY_ATTEMPTS", "LLM_RETRY_BASE_DELAY", "LLM_TOTAL_TIMEOUT_SECONDS", "CHAT_LLM_FALLBACK_MODEL"):
        monkeypatch.delenv(key, raising=False)
    import time
    from types import SimpleNamespace
    waits = []
    monkeypatch.setattr(base, "time", SimpleNamespace(monotonic=time.monotonic, sleep=waits.append))   # this module only

    def relay_down(kw):
        raise bad_gateway()
    one = lambda kw: intent_json("chat", tool_query={"needed": True, "tools": [BOTH[0]], "reason": "周三下午的课"})
    w, chats = world(make_world, monkeypatch, campus_server(), intent=one, chat=relay_down)
    say(w, QUESTION)
    assert len(chats) == 3 and all(kw["tool_choice"] == "required" for kw in chats)   # no switch to auto on a 5xx
    assert waits[:2] == [3.0, 6.0]
    assert w.client.sent_texts == ["我这边刚刚卡了一下，没接上你的话，你再说一次好不好"]
    assert all(h["total_calls"] == 0 for h in get_health_snapshot() if h["server"] == "campus")   # MCP never called
    job = w.db.list_memory_jobs()[0]
    assert job["status"] == "done" and job["assistant_reply"] == w.client.sent_texts[0]
    assert w.db.fetch_one("SELECT status FROM inbound_messages")["status"] == "failed"
    assert w.db.list_task_sessions() == []


def intent_reading_the_catalog(tool_suffix, missing):
    """An Intent that can only name tools its catalog shows (the fake used before named tools it could not see)."""
    def respond(kw):
        names = re.findall(r"campus__\w+", system(kw))
        pick = [n for n in dict.fromkeys(names) if n.endswith(tool_suffix)]
        query = {"needed": True, "tools": pick, "reason": "想知道"} if pick else {"needed": True, "tools": [], "missing": missing}
        return intent_json("chat", tool_query=query)
    return respond


@pytest.mark.parametrize("server,status,chat_says", [
    (campus_server(disabled_tools=["get_weather"]), "disabled", "需要的工具已经停用了"),
    (not_started(), "unavailable", "需要用到的工具现在连不上"),
])
def test_a_switched_off_or_unreachable_tool_is_reported_as_such(make_world, monkeypatch, server, status, chat_says):
    from ai import tool_manager as tm
    from tests.test_phase2_scenarios import say
    plans = []
    original = tm.plan_for_turn
    monkeypatch.setattr(tm, "plan_for_turn", lambda i, c: plans.append(original(i, c)) or plans[-1])
    w, chats = world(make_world, monkeypatch, server, intent=intent_reading_the_catalog("get_weather", "天气"))
    say(w, "明天广州的天气怎么样")
    assert "campus__get_weather" in system(w.llm.calls_for("fake-intent")[-1])
    assert plans[-1]["status"] == status and chat_says in system(chats[0]) and not offered(chats[0])


def test_the_catalog_names_switched_off_and_unreachable_tools_in_full():
    from ai.mcp import tool_turn
    from ai.tool_manager import discover, intent_note
    with tool_turn():
        catalog = discover([campus_server(disabled_tools=["search_web"]), not_started(name="dorm")])
    note = intent_note(catalog)
    assert "- 已停用：campus__search_web" in note and "dorm__get_weather" in note
    assert "- campus__search_web：" not in note                         # never offered as usable
    assert json.dumps(sorted(catalog.tools)) == json.dumps(["campus__get_course_schedule", "campus__get_weather"])
