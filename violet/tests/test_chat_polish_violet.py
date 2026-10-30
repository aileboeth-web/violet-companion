"""2026-10-30 rework step 2 ("打磨聊天"): the read-only tools go to Chat on every turn (VIOLET_CHAT_TOOLS_DIRECT), the
token ledger (ai/usage.py) and the overview, the last prompt on the admin page, the persona first for the prefix
cache. Offline; the fake campus MCP server via stdio, fake SDK."""

from __future__ import annotations

from types import SimpleNamespace

from tests.fake_mcp import campus_server
from tests.fakes import completion, intent_json
from tests.test_phase2_scenarios import make_world, say  # noqa: F401  (make_world: fixture)
from tests.test_qq_social_violet import SAME, admin, db  # noqa: F401
from tests.test_tools_p92 import fresh_registry, offered, system, world  # noqa: F401  (fresh_registry: fixture)

SCHEDULE, WEATHER, SEARCH = "campus__get_course_schedule", "campus__get_weather", "campus__search_web"


def tool_results(kw) -> list[str]:
    return [m["content"] for m in kw["messages"] if m["role"] == "tool"]


def test_chat_gets_the_read_only_tools_without_intent_asking(make_world, monkeypatch):
    """The real miss: Intent did not write a tool_query for "今天杭州什么天气", so Chat never saw the weather tool."""
    def chat(kw):
        if not tool_results(kw):
            return completion(tool_calls=[{"name": WEATHER, "arguments": {"city": "杭州"}}])
        return "杭州今天可能下小雨" if "小雨" in "\n".join(tool_results(kw)) else "没查到"
    w, chats = world(make_world, monkeypatch, campus_server(), intent=lambda kw: intent_json("chat"), chat=chat)
    say(w, "今天杭州什么天气")
    assert sorted(offered(chats[0])) == sorted([SCHEDULE, WEATHER, SEARCH]) and chats[0]["tool_choice"] == "auto"
    assert "【可以用的工具】" in system(chats[0])
    assert w.client.sent_texts[-1] == "杭州今天可能下小雨"


def test_a_planned_tool_is_never_required_when_several_are_offered(make_world, monkeypatch):
    """The relay answered HTTP 502 for "required" with several tools (phase 9.2.2): the planned one is still offered,
    the first call is "auto", the reminder asks for it once if the model skips it."""
    asks = lambda kw: intent_json("chat", tool_query={"needed": True, "tools": [SCHEDULE], "reason": "周一的课"})
    w, chats = world(make_world, monkeypatch, campus_server(), intent=asks, chat=lambda kw: "我看看")
    say(w, "我周一有什么课")
    assert SCHEDULE in offered(chats[0]) and chats[0]["tool_choice"] == "auto"
    assert any((m.get("content") or "").startswith("【还没查】") for m in chats[-1]["messages"] if m["role"] == "system")


def test_the_old_way_is_one_switch_away(make_world, monkeypatch):
    monkeypatch.setenv("VIOLET_CHAT_TOOLS_DIRECT", "0")
    w, chats = world(make_world, monkeypatch, campus_server(), intent=lambda kw: intent_json("chat"))
    say(w, "今天好累啊")
    assert not offered(chats[0]) and "【可以用的工具】" not in system(chats[0])


def test_usage_ledger_numbers_and_summary(violetdb):
    from ai import usage
    from ai.settings import CURRENT_TURN
    violetdb.execute("DELETE FROM llm_usage")
    assert usage.numbers({"prompt_tokens": 1000, "completion_tokens": 50, "prompt_cache_hit_tokens": 800}) == (1000, 50, 800)
    details = SimpleNamespace(prompt_tokens=10, completion_tokens=2, prompt_tokens_details=SimpleNamespace(cached_tokens=4))
    assert usage.numbers(details) == (10, 2, 4) and usage.numbers(None) == (0, 0, 0)
    token = CURRENT_TURN.set("t-1")
    try:
        usage.record("IntentAgent", "m", {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_cache_hit_tokens": 800},
                     db=violetdb)
        usage.record("ChatAgent", "m", {"prompt_tokens": 3000, "completion_tokens": 150}, db=violetdb)
    finally:
        CURRENT_TURN.reset(token)
    usage.record("social", "m", {"prompt_tokens": 2000, "completion_tokens": 100}, db=violetdb)
    s = usage.summary(violetdb)
    assert s["calls"] == 3 and s["total"] == 6300 and s["cached"] == 800 and s["cache_rate"] == round(800 / 6000, 3)
    assert s["turns"] == 1 and s["per_turn"] == 4200 and s["calls_per_turn"] == 2.0
    assert [p["label"] for p in s["parts"]] == ["回复", "群聊 / 别人私聊", "意图识别"]
    assert usage.turn(violetdb, "t-1")["sources"] == ["意图识别", "回复"]
    usage.record("x", "m", object(), db=violetdb)          # odd usage objects never raise
    usage.record("x", "m", {"prompt_tokens": 1}, db=None)


def test_every_agent_call_is_metered(make_world, monkeypatch):
    w, chats = world(make_world, monkeypatch, campus_server(), intent=lambda kw: intent_json("chat"))
    w.db.execute("DELETE FROM llm_usage")
    say(w, "在吗")
    sources = [r["source"] for r in w.db.fetch_all("SELECT source FROM llm_usage")]
    assert "IntentAgent" in sources and "ChatAgent" in sources


def test_overview_usage_and_last_prompt(admin, violetdb):
    from ai.agents.chat_agent import LAST_PROMPT, remember_prompt
    LAST_PROMPT.clear()
    assert admin.get("/api/control/last-prompt").get_json()["data"] is None
    remember_prompt([{"role": "system", "content": "【规则】\n第一条"}, {"role": "user", "content": "你好"}])
    d = admin.get("/api/control/last-prompt").get_json()["data"]
    assert d["chars"] == len("【规则】\n第一条") + 2 and d["parts"][0]["title"] == "【规则】"
    o = admin.get("/api/control/overview").get_json()["data"]
    assert "usage" in o


def test_persona_comes_first_for_the_prefix_cache(violetdb):
    from ai.context_engine import ContextEngine
    packet = ContextEngine(violetdb).build("chat", "你好")
    names = list(packet.report["sections"])
    assert names and names[0] == "persona_core"
