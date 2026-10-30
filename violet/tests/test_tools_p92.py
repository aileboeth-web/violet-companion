"""Phase 9.2: unified tool management and Violet's own use of MCP tools (offline).

The fake campus MCP server (tools/fake_mcp_server.py) runs as a real stdio child process with FICTIONAL data; the
models are the fake SDK. What is proven: which tools are offered to which agent and when, what is called, what the
reply is built from, that nothing is invented or turned into a task when a tool is missing / off / down, and that the
Control Center's switches act on the running system. Whether a real model picks tools well is not provable offline
(the real-model plan is in tools/README.md §23)."""

from __future__ import annotations

import json
import time

import pytest

from tests.fake_mcp import campus_server, not_started, use
from tests.fakes import completion, intent_json


@pytest.fixture(autouse=True)
def fresh_registry(tmp_path, monkeypatch):
    import ai.mcp as mcp
    import ai.tool_manager as tm
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    tm._STORE = None
    mcp.reset_registry()
    yield
    mcp.reset_registry()
    tm._STORE = None


def run(coro):
    import asyncio
    return asyncio.run(coro)


# ----------------------------------------------------------------------------------------------- the layer

def test_remote_servers_must_use_https_and_local_ones_need_no_network():
    from ai.mcp import MCPToolBridge, remote_problem
    assert remote_problem({"url": "http://192.168.1.20:8787/mcp"}) == "a remote MCP server must use https"
    assert remote_problem({"url": "https://mcp.example.com/mcp"}) is None
    assert remote_problem({"url": "http://127.0.0.1:8787/mcp"}) is None
    assert remote_problem({"transport": "stdio", "command": ["x"]}) is None
    bridge = MCPToolBridge([{"name": "lan", "url": "http://192.168.1.20:8787/mcp", "tools": {"a": "read"}}])
    run(bridge.connect())
    assert bridge.tools == [] and bridge.unavailable[0]["error_code"] == "MCP_CONFIG_ERROR"


def test_errors_reach_the_model_as_a_category_without_addresses_paths_or_secrets():
    from ai.mcp import MCPErrorCode, safe_error_text, tool_error
    text = safe_error_text("ConnectError at http://10.0.0.8:9000/x?key=abc, file C:\\violet\\data\\a.db, host 172.16.0.3")
    assert "10.0.0.8" not in text and "172.16.0.3" not in text and "C:\\violet" not in text and "key=abc" not in text
    for code, status in ((MCPErrorCode.CONNECT, "unavailable"), (MCPErrorCode.TOOL, "tool_error"),
                         (MCPErrorCode.DENIED, "permission_denied"), (MCPErrorCode.UNKNOWN_TOOL, "not_configured"),
                         (MCPErrorCode.TIMEOUT, "timeout"), (MCPErrorCode.AUTH, "permission_denied")):
        payload = json.loads(tool_error(code, "boom at /opt/violet/x", "s", "t"))
        assert payload["ok"] is False and payload["status"] == status and "不要编造" in payload["note"]
        assert "/opt/violet" not in payload["message"]


def test_settings_persist_apply_at_once_and_never_show_a_credential(tmp_path):
    from ai.tool_manager import ToolSettingsStore, apply_tool_settings, tool_store
    store = tool_store()
    with pytest.raises(ValueError):
        store.save_server("phone", {"url": "http://evil.example/mcp"})          # address / command: .env only
    with pytest.raises(ValueError):
        store.save_server("phone", {"agents": ["memory"]})
    store.save_server("phone", {"enabled": False, "call_timeout": 12, "offer": "always"})
    store.set_tool("phone", "battery", False)
    result = store.replace_secret("phone", "Authorization", "Bearer new-secret-token-123")
    assert "new-secret" not in json.dumps(result) and result["is_set"] and len(result["fingerprint"]) == 8
    servers = apply_tool_settings([{"name": "phone", "url": "http://127.0.0.1:8787/mcp",
                                    "headers": {"Authorization": "Bearer old"}}, {"name": "other", "url": "x"}])
    assert servers[0]["enabled"] is False and servers[0]["call_timeout"] == 12 and servers[0]["offer"] == "always"
    assert servers[0]["disabled_tools"] == ["battery"] and servers[0]["headers"]["Authorization"] == "Bearer new-secret-token-123"
    assert servers[1] == {"name": "other", "url": "x"}
    again = ToolSettingsStore(tmp_path / "data")                                 # "after a restart"
    assert again.servers()["phone"]["enabled"] is False
    assert "new-secret" not in again.history_path.read_text(encoding="utf-8")
    assert "new-secret" not in again.path.read_text(encoding="utf-8")


def test_the_catalog_lists_only_what_answered_and_is_allowed():
    from ai.mcp import tool_turn
    from ai.tool_manager import discover, intent_note
    with tool_turn():
        catalog = discover([campus_server(disabled_tools=["search_web"]), not_started(name="dorm")])
    assert sorted(catalog.tools) == ["campus__get_course_schedule", "campus__get_weather"]
    assert catalog.disabled == ["campus__search_web"] and catalog.unavailable[0]["server"] == "dorm"
    assert catalog.status_of("dorm__get_weather") == "unavailable"
    assert catalog.status_of("campus__search_web") == "disabled" and catalog.status_of("x__y") == "not_configured"
    note = intent_note(catalog)
    assert "campus__get_course_schedule" in note and "暂时连不上：dorm" in note
    # phase 9.2.1: a switched-off tool is named as such (never as usable), so Intent can say which one it needed
    assert "- campus__search_web：" not in note and "- 已停用：campus__search_web" in note
    assert intent_note(discover([])) is None                     # nothing configured: Intent's input is unchanged


def test_failures_are_classified_and_a_dead_server_is_not_retried_in_the_same_turn():
    from ai.mcp import MCPToolBridge, get_health_snapshot, tool_turn

    async def call(server, tool, args):
        async with MCPToolBridge([server], agent_name="chat") as bridge:
            return json.loads(await bridge.call_tool(tool, args)) if bridge.tools else None
    with tool_turn():
        error = run(call(campus_server(mode="error", name="err"), "err__get_weather", {"city": "示例市"}))
    assert error["status"] == "tool_error" and "10.0.0.8" not in error["message"]
    with tool_turn():
        crashed = run(call(campus_server(mode="crash", name="crash"), "crash__get_weather", {"city": "示例市"}))
        again = run(call(campus_server(mode="crash", name="crash"), "crash__get_weather", {"city": "示例市"}))
    assert crashed["status"] == "unavailable"
    assert again is None or again["error_code"] == "MCP_FAILED_THIS_TURN"       # not started again in this turn
    calls = {h["server"]: h["total_calls"] for h in get_health_snapshot()}
    assert calls["crash"] == 2                                                  # one call + one reconnect, then stop
    with tool_turn():
        start = time.monotonic()
        slow = run(call(campus_server(mode="slow", name="slow", call_timeout=2), "slow__get_weather", {"city": "示例市"}))
    assert slow["status"] in ("timeout", "unavailable") and time.monotonic() - start < 15


def test_after_a_failed_call_the_next_call_of_the_turn_is_not_sent():
    from ai.mcp import MCPToolBridge, get_health_snapshot, tool_turn

    async def two_calls():
        async with MCPToolBridge([campus_server(mode="crash", name="crash")], agent_name="chat") as bridge:
            first = json.loads(await bridge.call_tool("crash__get_weather", {"city": "示例市"}))
            second = json.loads(await bridge.call_tool("crash__get_weather", {"city": "示例市"}))
            return first, second
    with tool_turn():
        first, second = run(two_calls())
    assert first["status"] == "unavailable" and second["error_code"] == "MCP_FAILED_THIS_TURN"
    assert {h["server"]: h["total_calls"] for h in get_health_snapshot()}["crash"] == 2      # nothing more was sent


def test_bad_arguments_are_refused_before_anything_is_sent_and_a_stdio_server_needs_a_command():
    from ai.mcp import MCPToolBridge, get_health_snapshot

    async def bad_call():
        async with MCPToolBridge([campus_server()], agent_name="chat") as bridge:
            return json.loads(await bridge.call_tool("campus__get_course_schedule", {"day": 3}))
    result = run(bad_call())
    assert result["status"] == "invalid_arguments" and "day" in result["message"]
    assert {h["server"]: h["total_calls"] for h in get_health_snapshot()}["campus"] == 0
    bridge = MCPToolBridge([{"name": "nocmd", "transport": "stdio", "tools": {"a": "read"}}])
    run(bridge.connect())
    assert bridge.unavailable[0]["error_code"] == "MCP_CONFIG_ERROR" and "command" in bridge.unavailable[0]["message"]


def test_a_write_tool_never_answers_a_chat_query():
    from ai.tool_manager import Catalog, ToolInfo, plan_for_turn
    catalog = Catalog(configured=True, tools={
        "shop__order": ToolInfo("shop__order", "shop", "order", "下单", "write", ["chat", "performer"])})
    plan = plan_for_turn({"route": "chat", "tool_query": {"needed": True, "tools": ["shop__order"]}}, catalog)
    assert plan["mode"] == "performer"                       # an execution goes through the Performer, never Chat


def test_a_provider_that_refuses_required_still_gets_the_turn_done(make_world, monkeypatch):
    import httpx
    import openai
    from tests.test_phase2_scenarios import say
    answer = look_up_then_answer("campus__get_course_schedule", {"day": "周一"})
    choices = []

    def picky(kw):
        choices.append(kw.get("tool_choice"))
        if kw.get("tool_choice") == "required":
            raise openai.BadRequestError("tool_choice required is not supported",
                                         response=httpx.Response(400, request=httpx.Request("POST", "http://x/v1")),
                                         body=None)
        return answer(kw)
    w, chats = world(make_world, monkeypatch, campus_server(), intent=asks_for("campus__get_course_schedule"), chat=picky)
    say(w, "我周一有什么课")
    assert choices[:2] == ["required", "auto"] and "高等数学" in w.client.sent_texts[-1]


@pytest.mark.parametrize("query,mode,status", [
    (None, "none", None),
    ({"needed": True, "tools": ["campus__get_course_schedule"]}, "chat", "ok"),
    ({"needed": True, "tools": ["dorm__get_weather"]}, "blocked", "unavailable"),
    ({"needed": True, "tools": ["campus__search_web"]}, "blocked", "disabled"),
    ({"needed": True, "tools": [], "missing": "订机票"}, "blocked", "not_configured"),
])
def test_the_plan_of_a_turn(query, mode, status):
    from ai.mcp import tool_turn
    from ai.tool_manager import discover, plan_for_turn
    with tool_turn():
        catalog = discover([campus_server(disabled_tools=["search_web"]), not_started(name="dorm")])
    plan = plan_for_turn({"route": "chat", **({"tool_query": query} if query else {})}, catalog)
    assert plan["mode"] == mode and plan.get("status") == status


def test_a_new_task_without_any_executing_tool_is_blocked():
    from ai.tool_manager import Catalog, plan_for_turn
    plan = plan_for_turn({"route": "task", "active_task_id": None, "task": {"name": "点外卖"}}, Catalog(configured=False))
    assert plan["mode"] == "blocked" and plan["status"] == "no_executor" and plan["missing"] == "点外卖"
    assert plan_for_turn({"route": "task", "active_task_id": 3}, Catalog())["mode"] == "none"   # continuing a task


# ----------------------------------------------------------------------------------------------- whole turns

def world(make_world, monkeypatch, *servers, intent=None, chat=None):
    use(monkeypatch, *servers)
    w = make_world()
    if intent is not None:
        w.llm.responders["fake-intent"] = intent
    chats: list[dict] = []

    def chat_responder(kw):
        chats.append(kw)
        return chat(kw) if chat else "嗯嗯"
    w.llm.responders["fake-chat"] = chat_responder
    return w, chats


def asks_for(*tools, missing=None):
    query = {"needed": True, "tools": list(tools), "reason": "用户想知道的"}
    if missing:
        query["missing"] = missing
    return lambda kw: intent_json("chat", tool_query=query)


def look_up_then_answer(tool, args):
    def respond(kw):
        results = [m["content"] for m in kw["messages"] if m["role"] == "tool"]
        if not results:
            return completion(tool_calls=[{"name": tool, "arguments": args}])
        return "查到啦：" + tool_data(results[-1])[:200]
    return respond


def tool_data(message: str) -> str:
    """What a model takes from a tool message: the data, not Violet's 【外部工具数据…】 wrapper lines (phase 10: the
    outbound guard refuses a reply that repeats the wrapper)."""
    return "\n".join(line for line in message.splitlines() if not line.startswith("【")).strip()


def offered(kw) -> list[str]:
    return [t["function"]["name"] for t in kw.get("tools") or []]


def system(kw) -> str:
    return "\n".join(m.get("content") or "" for m in kw["messages"] if m["role"] == "system")


def test_a_question_that_needs_the_timetable_is_looked_up_and_answered_from_the_result(make_world, monkeypatch):
    from tests.test_phase2_scenarios import say
    w, chats = world(make_world, monkeypatch, campus_server(), intent=asks_for("campus__get_course_schedule"),
                     chat=look_up_then_answer("campus__get_course_schedule", {"day": "周一"}))
    say(w, "我周一都有什么课来着")
    intent_request = w.llm.calls_for("fake-intent")[-1]
    assert "campus__get_course_schedule" in system(intent_request)              # Intent saw the real catalog
    first, second = chats[0], chats[1]
    assert offered(first) == ["campus__get_course_schedule"] and first.get("tool_choice") == "required"
    assert "【这一轮先查再回答】" in system(first)
    assert "高等数学" in next(m["content"] for m in second["messages"] if m["role"] == "tool")
    assert "高等数学" in w.client.sent_texts[-1]                                   # the reply is built from the result
    assert w.db.list_task_sessions() == [] and w.llm.calls_for("fake-performer") == []


def test_small_talk_gets_no_tools_and_calls_nothing(make_world, monkeypatch):
    from tests.test_phase2_scenarios import say
    w, chats = world(make_world, monkeypatch, campus_server())
    say(w, "今天好累啊，抱抱")
    assert chats and all(not offered(kw) for kw in chats) and "先查再回答" not in system(chats[0])
    assert w.llm.calls_for("fake-performer") == []


def test_a_server_that_is_not_running_is_tried_once_and_chat_says_it_cannot_look_it_up(make_world, monkeypatch):
    from ai.mcp import get_health_snapshot
    from tests.test_phase2_scenarios import say
    w, chats = world(make_world, monkeypatch, not_started(), intent=asks_for("campus__get_course_schedule"))
    say(w, "明天有课吗")
    assert "暂时连不上：campus" in system(w.llm.calls_for("fake-intent")[-1])
    assert not offered(chats[0]) and "需要用到的工具现在连不上" in system(chats[0])
    assert {h["server"]: h["total_failures"] for h in get_health_snapshot()}["campus"] == 1    # not again this turn
    assert w.db.list_task_sessions() == []


def test_the_connection_dropping_mid_call_ends_in_an_honest_reply(make_world, monkeypatch):
    from tests.test_phase2_scenarios import say
    w, chats = world(make_world, monkeypatch, campus_server(mode="crash"), intent=asks_for("campus__get_weather"),
                     chat=look_up_then_answer("campus__get_weather", {"city": "示例市"}))
    say(w, "明天要不要带伞")
    result = json.loads(next(m["content"] for m in chats[-1]["messages"] if m["role"] == "tool"))
    assert result["status"] == "unavailable" and "不要编造" in result["note"]
    assert w.client.sent_texts and w.db.list_task_sessions() == []


def test_a_tool_error_is_a_failed_query_not_a_result(make_world, monkeypatch):
    from tests.test_phase2_scenarios import say
    w, chats = world(make_world, monkeypatch, campus_server(mode="error"), intent=asks_for("campus__get_weather"),
                     chat=look_up_then_answer("campus__get_weather", {"city": "示例市"}))
    say(w, "明天天气怎么样")
    tool_text = next(m["content"] for m in chats[-1]["messages"] if m["role"] == "tool")
    assert json.loads(tool_text)["status"] == "tool_error" and "10.0.0.8" not in tool_text and "/internal" not in tool_text


def test_a_capability_that_does_not_exist_is_said_so_and_never_becomes_a_task(make_world, monkeypatch):
    from tests.test_phase2_scenarios import say
    w, chats = world(make_world, monkeypatch, campus_server(),
                     intent=lambda kw: intent_json("task", tool_query={"needed": True, "tools": [], "missing": "订机票"}))
    say(w, "帮我订一张明天去北京的机票")
    assert "现在没有能做这件事的工具" in system(chats[0]) and "订机票" in system(chats[0])
    assert not offered(chats[0]) and w.db.list_task_sessions() == [] and w.llm.calls_for("fake-performer") == []


def test_several_possible_tools_are_all_offered_and_the_model_picks(make_world, monkeypatch):
    from tests.test_phase2_scenarios import say
    w, chats = world(make_world, monkeypatch, campus_server(),
                     intent=asks_for("campus__get_weather", "campus__search_web"),
                     chat=look_up_then_answer("campus__search_web", {"query": "示例市 明天 降雨"}))
    say(w, "明天会下雨吗，网上怎么说")
    assert sorted(offered(chats[0])) == ["campus__get_weather", "campus__search_web"]
    assert "示例结果" in w.client.sent_texts[-1]


def test_a_task_with_no_executing_tool_creates_nothing(make_world, monkeypatch):
    from tests.test_phase2_scenarios import say
    w, chats = world(make_world, monkeypatch, campus_server(agents=("chat",)),
                     intent=lambda kw: intent_json("task"))
    say(w, "帮我点杯奶茶")
    assert w.db.list_task_sessions() == [] and w.llm.calls_for("fake-performer") == []
    assert "没有交给任务执行" in system(chats[0])


def test_the_performers_tool_data_reaches_the_reply(make_world, monkeypatch):
    from tests.test_phase2_scenarios import say
    w, chats = world(make_world, monkeypatch, campus_server(agents=("performer",), name="ops"),
                     intent=lambda kw: intent_json("task", tool_query={"needed": True, "tools": ["ops__get_weather"]}))
    w.llm.responders["fake-performer"] = look_up_then_answer("ops__get_weather", {"city": "示例市"})
    say(w, "帮我看看示例市的天气然后安排一下")
    assert len(w.db.list_task_sessions()) == 1
    assert "多云转小雨" in system(chats[0]) and "刚才执行任务时工具返回的数据" in system(chats[0])


def test_the_control_center_switches_act_on_the_next_turn(make_world, monkeypatch, admin):
    from tests.test_control_center_p90 import SAME
    from tests.test_phase2_scenarios import say
    w, chats = world(make_world, monkeypatch, campus_server(), intent=asks_for("campus__get_course_schedule"),
                     chat=look_up_then_answer("campus__get_course_schedule", {"day": "周一"}))
    listed = admin.post("/api/control/tools/refresh", headers=SAME, json={}).get_json()["data"]["servers"]
    campus = next(s for s in listed if s["name"] == "campus")
    assert campus["status"] == "connected" and {t["name"] for t in campus["tools"]} >= {"get_course_schedule"}
    resp = admin.post("/api/control/tools/servers/campus/tools/get_course_schedule", headers=SAME, json={"enabled": False})
    assert resp.status_code == 200
    say(w, "我周一有什么课")
    assert not offered(chats[0]) and "需要的工具已经停用了" in system(chats[0])
    admin.post("/api/control/tools/servers/campus/tools/get_course_schedule", headers=SAME, json={"enabled": True})
    chats.clear()
    say(w, "我周一有什么课呀")
    assert offered(chats[0]) == ["campus__get_course_schedule"]
    assert admin.post("/api/control/tools/servers/campus", headers=SAME, json={"enabled": False}).status_code == 200
    chats.clear()
    say(w, "那周一的课呢")
    assert not offered(chats[0])
    assert admin.post("/api/control/tools/servers/nope", headers=SAME, json={"enabled": False}).status_code == 404
    assert admin.post("/api/control/tools/servers/campus", headers=SAME, json={"command": ["calc.exe"]}).status_code == 400


def test_a_switch_reaches_an_agent_that_was_built_before_it_without_a_reload(monkeypatch, violetdb):
    from ai.agents import PerformerAgent
    from ai.tool_manager import tool_store
    use(monkeypatch, campus_server(agents=("performer",), name="ops"))
    agent = PerformerAgent()                                 # built (config read) before the switches below
    tool_store().set_tool("ops", "get_weather", False)
    assert agent._mcp_servers_for_run(agent._config)[0]["disabled_tools"] == ["get_weather"]
    tool_store().set_tool("ops", "get_weather", True)        # and back on: not stuck in a stale copy
    assert agent._mcp_servers_for_run(agent._config)[0].get("disabled_tools") == []
    tool_store().save_server("ops", {"enabled": False})
    assert agent._mcp_servers_for_run(agent._config) == []


def test_a_replaced_credential_is_never_shown(make_world, monkeypatch, admin):
    from tests.test_control_center_p90 import SAME
    use(monkeypatch, {"name": "phone", "url": "http://127.0.0.1:1/mcp", "headers": {"Authorization": "Bearer old-token-1"},
                      "tools": {"battery": "read"}})
    assert admin.post("/api/control/tools/servers/phone/secret", headers=SAME,
                      json={"header": "Authorization", "value": "Bearer new-token-2"}).status_code == 400   # no confirm
    resp = admin.post("/api/control/tools/servers/phone/secret", headers=SAME,
                      json={"header": "Authorization", "value": "Bearer new-token-2", "confirm": True})
    assert resp.status_code == 200
    page = admin.get("/api/control/tools", headers=SAME).get_data(as_text=True)
    assert "new-token-2" not in page and "old-token-1" not in page and "fingerprint" in page
    from ai.chat import get_mcp_servers
    assert get_mcp_servers()[0]["headers"]["Authorization"] == "Bearer new-token-2"


def test_free_chat_can_run_with_the_fictional_tools_only(tmp_path):
    """The real-model verification of tools/README §23 needs free_chat with the fake campus server; nothing else."""
    from tests.test_free_chat_budget_p911 import launch, real
    from tools import free_chat as fc
    path = tmp_path / "tools-1"
    code, out = launch(real(path, "--fake-tools", "on"), ["yes", "我周一都有什么课来着", "/quit"])
    assert code == 0, out[-3000:]
    meta = fc.read_meta(path)
    assert meta["fake_tools"] == "on" and meta["launches"][0]["mcp"].startswith("fake-campus")
    intent = next(r for r in fc.read_jsonl(path / fc.REQUESTS) if r["agent"] == "IntentAgent")
    catalog = "\n".join(m["content"] or "" for m in intent["request"] if m["role"] == "system")
    assert "campus__get_course_schedule" in catalog and "tool_query" in catalog
    code, out = launch(real(path, "--fake-tools", "off"), ["yes", "/quit"])
    assert code == 2 and "虚构工具" in out


TOOL_BOOT = r'''
import json, sys, time, uuid
from unittest.mock import patch
from openai.types.chat import ChatCompletion
from tools import chat_dialogue as cd
from tools.harness import _AGENT
from tests.fakes import intent_json
Mock = cd._mock_class()
def done(content, model, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [{"id": "call_" + uuid.uuid4().hex[:6], "type": "function",
                              "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}} for n, a in tool_calls]
    return ChatCompletion.model_validate({
        "id": "fake-" + uuid.uuid4().hex[:8], "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop", "message": msg}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}})
class FakeSDK(Mock):
    def __init__(self, **kwargs):
        super().__init__()
    def _create(self, **kwargs):
        agent, msgs, model = _AGENT.get(), kwargs.get("messages") or [], str(kwargs.get("model"))
        user = next((m.get("content") or "" for m in reversed(msgs) if m.get("role") == "user"), "")
        offered = [t["function"]["name"] for t in kwargs.get("tools") or []]
        results = [m for m in msgs if m.get("role") == "tool"]
        if agent == "IntentAgent":
            tool = "campus__get_weather" if "天气" in user else "campus__get_course_schedule" if "课" in user else None
            extra = {"tool_query": {"needed": True, "tools": [tool], "reason": "想知道"}} if tool else {}
            return done(intent_json("chat", **extra), model)
        if agent == "ChatAgent":
            mine = [n for n in offered if n.startswith("campus__")]
            if mine and not results:
                return done(None, model, [(mine[0], {"day": "周一"} if mine[0].endswith("schedule") else {"city": "示例市"})])
            return done("查到啦" if results else "嗯嗯，我在呢～", model)
        return done(super()._create(**kwargs).choices[0].message.content, model)
with patch("openai.OpenAI", FakeSDK):
    from tools import free_chat
    raise SystemExit(free_chat.main(json.loads(sys.argv[1])))
'''


def tool_launch(argv, lines):
    import subprocess
    import sys
    from tests.test_free_chat_budget_p911 import child_env
    from tools import chat_dialogue as cd
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", TOOL_BOOT, json.dumps(argv)], cwd=cd.PROJECT,
                          env=child_env(), input="\n".join(lines) + "\n", capture_output=True, text=True,
                          encoding="utf-8", timeout=900)
    return done.returncode, done.stdout + done.stderr


def test_free_chat_traces_every_tool_step_and_switches_the_fictional_tools(tmp_path):
    """The real-model check (tools/README §23) runs in one free_chat session with the fake campus server only; each
    round records the catalog, Intent's tool_query, the plan, the tools offered + tool_choice, the model's tool calls
    and the MCP results, and /tool and /fake-mode switch the fictional tools between rounds."""
    from tests.test_free_chat_budget_p911 import KEY, real
    from tools import free_chat as fc
    path = tmp_path / "tools-1"
    code, out = tool_launch(real(path, "--fake-tools", "on"), [
        "yes", "我周一有什么课", "/tool off get_weather", "明天天气怎么样", "/fake-mode error", "我周一有什么课",
        "/fake-mode normal", "/tool on get_weather", "明天天气怎么样", "/show 1", "/tool off get_lunch", "/quit"])
    assert code == 0, out[-3000:]
    meta = fc.read_meta(path)
    assert [s.get("tool") or s.get("fake_mode") for s in meta["tool_switches"]] == ["get_weather", "error", "normal",
                                                                                     "get_weather"]
    events = fc.read_jsonl(path / fc.EVENTS)
    trace = {e["n"]: e["tool_trace"] for e in events}

    def kinds(n, kind):
        return [t for t in trace[n] if t["kind"] == kind]
    # 1: looked up in Chat's own turn, first call forced, the result came back
    assert kinds(1, "plan")[0]["plan"]["mode"] == "chat" and kinds(1, "plan")[0]["tool_query"]["needed"] is True
    first = next(t for t in kinds(1, "model_call") if t["agent"] == "ChatAgent")
    assert first["tool_choice"] == "required" and "campus__get_course_schedule" in first["offered"]
    assert first["tool_calls"][0]["name"] == "campus__get_course_schedule"
    assert kinds(1, "mcp_call")[0]["ok"] and "高等数学" in kinds(1, "mcp_call")[0]["preview"]
    # 2: switched off -> not in the catalog, the plan says disabled, nothing was called
    assert any("get_weather" in d for d in kinds(2, "catalog")[0]["disabled"])
    assert kinds(2, "plan")[0]["plan"]["status"] == "disabled" and not kinds(2, "mcp_call")
    # 3: the fake server fails -> a tool_error, no internal address in what was recorded
    assert kinds(3, "mcp_call")[0]["status"] == "tool_error" and "10.0.0.8" not in json.dumps(trace[3])
    # 4: both switches undone -> works again
    assert kinds(4, "mcp_call")[0]["ok"] and kinds(4, "mcp_call")[0]["tool"] == "campus__get_weather"
    assert "· 工具判断：" in out and "MCP campus__get_course_schedule" in out and "没有这个虚构工具" in out
    assert KEY not in "".join(p.read_text(encoding="utf-8") for p in path.glob("*.json*"))


from tests.test_control_center_p90 import admin, app, env_guard, made  # noqa: E402,F401  (fixtures)
from tests.test_phase2_scenarios import make_world  # noqa: E402,F401  (fixture)


@pytest.fixture(autouse=True)
def _planned_tools_only(monkeypatch):
    """These tests describe the tools offered only when Intent plans them (VIOLET_CHAT_TOOLS_DIRECT=0); the default
    since 2026-10-30 (every read-only tool on every turn) is tested in tests/test_chat_polish_violet.py."""
    monkeypatch.setenv("VIOLET_CHAT_TOOLS_DIRECT", "0")
