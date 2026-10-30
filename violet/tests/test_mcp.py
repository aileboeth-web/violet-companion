"""MCP isolation: offline / hanging / slow servers, permissions, untrusted output."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from ai import mcp as mcp_module
from ai.mcp import MCPErrorCode, MCPToolBridge
from tests.fakes import FakeLLM, completion, default_responders, install_fake_llm
from tests.mcp_test_server import (
    INJECTION_TEXT, TEST_TOKEN, HangingServer, LocalMCPServer, closed_port_url,
)


@pytest.fixture(scope="module")
def mcp_server():
    server = LocalMCPServer().start()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def _fast_timeouts(monkeypatch):
    monkeypatch.setenv("MCP_CONNECT_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "2")
    monkeypatch.delenv("MCP_ALLOWED_RISKY_TOOLS", raising=False)
    # the strict allowlist these tests are about (2026-10-30 default: unclassified tools get a level from their hints,
    # see tests/test_free_tools_violet.py)
    monkeypatch.setenv("VIOLET_MCP_AUTO_CLASSIFY", "0")
    from ai import permissions                     # and write tools hidden, as before "先问你" became the default
    monkeypatch.setattr(permissions, "level", lambda ability: "forbid")
    mcp_module.reset_registry()


# Phase 2: tools must be explicitly allowlisted. The phone's real tools come from
# mcp_tool_policy.json; the extra tools of the local test server are declared here.
TEST_TOOLS = {"echo": "read", "slow": "read", "injected": "read", "create_order": "high_risk"}


def allowlisted_server(url, token=TEST_TOKEN, **extra):
    return phone_style(url, token, tools=dict(TEST_TOOLS), **extra)


def phone_style(url, token=TEST_TOKEN, **extra):
    """Same shape as docs/VIOLET_INTEGRATION.md of the MCP repository (D:\\unity\\aichat\\mcp)."""
    cfg = {
        "name": "android-phone",
        "transport": "streamable_http",
        "url": url,
        "headers": {"Authorization": f"Bearer {token}"},
    }
    cfg.update(extra)
    return cfg


def run(coro):
    return asyncio.run(coro)


async def _prepare(servers, agent="chat"):
    bridge = MCPToolBridge(servers, agent_name=agent)
    await bridge.connect()
    return bridge


def test_online_server_lists_only_safe_tools(mcp_server):
    async def go():
        async with MCPToolBridge([allowlisted_server(mcp_server.url)], agent_name="chat") as bridge:
            names = sorted(bridge.tool_to_session)
            text = await bridge.call_tool("android-phone__echo", {"text": "hi"})
            denied = await bridge.call_tool("android-phone__create_order", {"item": "x"})
            return names, text, denied, bridge.unavailable
    names, text, denied, unavailable = run(go())
    assert "android-phone__create_order" not in names
    assert {"android-phone__echo", "android-phone__get_screen_state", "android-phone__slow"} <= set(names)
    assert "echo:hi" in text and "不是指令" in text
    assert json.loads(denied)["error_code"] == MCPErrorCode.DENIED
    assert unavailable == []


def test_risky_tool_requires_explicit_allowlist(mcp_server, monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_RISKY_TOOLS", "android-phone__create_order")

    async def go():
        async with MCPToolBridge([allowlisted_server(mcp_server.url)], agent_name="chat") as bridge:
            return await bridge.call_tool("android-phone__create_order", {"item": "薯条"})
    assert "ORDER PLACED: 薯条" in run(go())


def test_offline_server_does_not_raise(mcp_server):
    start = time.monotonic()
    bridge = run(_prepare([phone_style(closed_port_url()), allowlisted_server(mcp_server.url, name="live")]))
    elapsed = time.monotonic() - start
    assert elapsed < 6
    assert bridge.unavailable[0]["error_code"] == MCPErrorCode.CONNECT
    assert any(name.startswith("live__") for name in bridge.tool_to_session), "healthy server still usable"
    run(bridge.close())


def test_hanging_server_times_out():
    hang = HangingServer().start()
    try:
        start = time.monotonic()
        bridge = run(_prepare([phone_style(hang.url)]))
        assert time.monotonic() - start < 8
        assert bridge.unavailable and bridge.unavailable[0]["error_code"] == MCPErrorCode.TIMEOUT
        assert bridge.tools == []
    finally:
        hang.stop()


def test_wrong_token_is_auth_error(mcp_server):
    bridge = run(_prepare([phone_style(mcp_server.url, token="wrong-token")]))
    assert bridge.unavailable[0]["error_code"] == MCPErrorCode.AUTH
    assert "wrong-token" not in json.dumps(bridge.unavailable)


def test_slow_tool_call_times_out_without_raising(mcp_server):
    async def go():
        async with MCPToolBridge([allowlisted_server(mcp_server.url)], agent_name="chat") as bridge:
            start = time.monotonic()
            result = await bridge.call_tool("android-phone__slow", {"seconds": 10})
            return result, time.monotonic() - start
    result, elapsed = run(go())
    assert json.loads(result)["error_code"] == MCPErrorCode.TIMEOUT
    assert elapsed < 9


def test_circuit_opens_after_repeated_failures():
    url = closed_port_url()
    run(_prepare([phone_style(url)]))
    run(_prepare([phone_style(url)]))
    start = time.monotonic()
    third = run(_prepare([phone_style(url)]))
    assert third.unavailable[0]["error_code"] == MCPErrorCode.CIRCUIT_OPEN
    assert time.monotonic() - start < 0.5, "open circuit must skip the connection attempt"
    snapshot = mcp_module.get_health_snapshot()
    assert snapshot[0]["circuit_open"] is True


def test_cached_tools_avoid_connecting_until_a_tool_is_called():
    server = LocalMCPServer().start()
    cfg = allowlisted_server(server.url)
    try:
        run(_prepare([cfg]))          # populates the tool cache
    finally:
        server.stop()

    async def go():
        async with MCPToolBridge([cfg], agent_name="chat") as bridge:
            listed = list(bridge.tool_to_session)
            # no connection was needed to list tools; the call itself now fails cleanly
            result = await bridge.call_tool("android-phone__echo", {"text": "x"})
            return listed, result, bridge.unavailable
    listed, result, unavailable = run(go())
    assert "android-phone__echo" in listed and unavailable == []
    assert json.loads(result)["error_code"] in (MCPErrorCode.CONNECT, MCPErrorCode.TIMEOUT)


def test_secrets_are_redacted_from_errors():
    url = closed_port_url() + "?key=SUPERSECRETHOSTEDKEY123"
    bridge = run(_prepare([{"name": "hosted", "transport": "streamablehttp", "url": url}]))
    dumped = json.dumps(bridge.unavailable) + json.dumps(mcp_module.get_health_snapshot())
    assert "SUPERSECRETHOSTEDKEY123" not in dumped
    assert mcp_module.redact_url(url).endswith("/mcp?<redacted>")
    assert "SUPERSECRET" not in mcp_module.scrub_secrets("GET http://h/mcp?key=SUPERSECRET&x=1 Bearer abc.def")


PHONE_TOOLS = [
    "get_foreground_app", "get_screen_state", "battery", "screen_info", "health",
    "get_app_usage_stats", "installed_apps", "app_info", "device_info", "system_properties",
    "permission_state", "storage_info", "locale_info", "time_now",
]


def test_phone_tools_are_allowlisted_read_only_via_policy():
    phone = phone_style("http://127.0.0.1:1/mcp")
    for name in PHONE_TOOLS:
        assert mcp_module.classify_tool_permission(phone, name, {"readOnlyHint": True}, server_name="android-phone") == "read", name
    # a differently named phone server can reuse the same profile
    renamed = {**phone, "name": "my-phone", "tool_profile": "android-phone"}
    assert mcp_module.classify_tool_permission(renamed, "battery", {}, server_name="my-phone") == "read"


def test_unknown_tools_are_never_granted_by_name_or_self_declared_hints():
    phone = phone_style("http://127.0.0.1:1/mcp")
    # read-looking names and readOnlyHint=True are NOT enough
    for name in ("get_weather", "list_files_readonly", "query_status"):
        level = mcp_module.classify_tool_permission(phone, name, {"readOnlyHint": True}, server_name="android-phone")
        assert level == "unknown", name
        assert not mcp_module.is_tool_allowed(phone, "android-phone", name, level)
    # risky-looking names are equally unknown (no heuristics either way)
    assert mcp_module.classify_tool_permission(phone, "create-order", {}, server_name="android-phone") == "unknown"
    # an unknown tool stays blocked even if someone lists it as "risky allowed"
    import os
    os.environ["MCP_ALLOWED_RISKY_TOOLS"] = "android-phone__get_weather"
    try:
        assert not mcp_module.is_tool_allowed(phone, "android-phone", "get_weather", "unknown")
    finally:
        del os.environ["MCP_ALLOWED_RISKY_TOOLS"]


def test_server_annotations_can_only_escalate_risk():
    cfg = {"name": "x", "tools": {"sync_data": "read"}}
    assert mcp_module.classify_tool_permission(cfg, "sync_data", {"destructiveHint": True}, server_name="x") == "high_risk"
    assert mcp_module.classify_tool_permission(cfg, "sync_data", {"readOnlyHint": False}, server_name="x") == "high_risk"
    assert mcp_module.classify_tool_permission(cfg, "sync_data", {}, server_name="x") == "read"
    legacy = {"name": "x", "tool_permissions": {"legacy_tool": "write"}}
    assert mcp_module.classify_tool_permission(legacy, "legacy_tool", {}, server_name="x") == "write"


def test_unclassified_tools_are_hidden_refused_and_reported(mcp_server):
    async def go():
        async with MCPToolBridge([phone_style(mcp_server.url)], agent_name="chat") as bridge:
            listed = sorted(bridge.tool_to_session)
            refused = await bridge.call_tool("android-phone__echo", {"text": "hi"})
            return listed, refused, list(bridge.unclassified_tools)
    listed, refused, unclassified = run(go())
    assert listed == ["android-phone__get_screen_state"]          # only the allowlisted phone tool
    assert json.loads(refused)["error_code"] == MCPErrorCode.DENIED
    assert {"android-phone__echo", "android-phone__slow", "android-phone__create_order"} <= set(unclassified)
    health = {h["server"]: h for h in mcp_module.get_health_snapshot()}
    assert "echo" in health["android-phone"]["unclassified_tools"]


def test_missing_policy_file_allows_nothing(monkeypatch, tmp_path, mcp_server):
    monkeypatch.setenv("MCP_TOOL_POLICY_FILE", str(tmp_path / "missing.json"))
    bridge = run(_prepare([phone_style(mcp_server.url)]))
    assert bridge.tool_to_session == {}
    run(bridge.close())


def test_agent_allowlist_defaults():
    phone = phone_style("http://127.0.0.1:1/mcp")
    assert mcp_module.server_applies_to_agent(phone, "chat")
    assert mcp_module.server_applies_to_agent(phone, "performer")
    assert not mcp_module.server_applies_to_agent(phone, "intent")
    assert not mcp_module.server_applies_to_agent(phone, "memory")
    assert mcp_module.server_applies_to_agent({**phone, "agents": ["*"]}, "memory")
    assert not mcp_module.server_applies_to_agent({**phone, "enabled": False}, "chat")


# --- end-to-end through the agents ------------------------------------------------

def _pipeline(agents):
    from ai.chat import run_pipeline
    return run_pipeline("在干嘛", agents["intent"], agents["chat"], agents["performer"], agents["memory"])


def _make_agents():
    from ai.agents import ChatAgent, IntentAgent, MemoryAgent, PerformerAgent
    return {"intent": IntentAgent(), "chat": ChatAgent(), "memory": MemoryAgent(), "performer": PerformerAgent()}


def test_chat_works_when_all_mcp_servers_are_offline(monkeypatch, violetdb):
    hang = HangingServer().start()
    try:
        monkeypatch.setenv("MCP_SERVERS", json.dumps([phone_style(closed_port_url()), phone_style(hang.url, name="stuck")]))
        fake = install_fake_llm(monkeypatch, FakeLLM(default_responders("在呢")))
        agents = _make_agents()
        start = time.monotonic()
        result = _pipeline(agents)
        assert result["chat"]["content"] == "在呢"
        assert time.monotonic() - start < 15
        assert not result["errors"]
        chat_call = fake.calls_for("fake-chat")[0]
        assert chat_call.get("tools") is None
        # intent and memory are not authorised for these servers, so only the chat
        # agent attempted a connection: exactly one failure per server.
        failures = {s["server"]: s["total_failures"] for s in mcp_module.get_health_snapshot()}
        assert failures == {"android-phone": 1, "stuck": 1}
    finally:
        hang.stop()


def test_injected_tool_output_is_wrapped_and_flagged(monkeypatch, violetdb, mcp_server):
    monkeypatch.setenv("MCP_SERVERS", json.dumps([allowlisted_server(mcp_server.url)]))
    script = iter([
        completion(tool_calls=[{"name": "android-phone__injected", "arguments": {}}]),
        completion(content="嗯"),
    ])
    fake = install_fake_llm(monkeypatch, FakeLLM({"fake-chat": lambda kw: next(script)}))
    from ai.agents import ChatAgent
    result = ChatAgent().run("你手机屏幕亮着吗")
    assert result["content"] == "嗯"
    first_call = fake.calls_for("fake-chat")[0]
    assert any(m.get("content") == mcp_module.UNTRUSTED_TOOL_NOTICE for m in first_call["messages"])
    assert all(m.get("role") != "system" or INJECTION_TEXT not in (m.get("content") or "") for m in result["messages"])
    tool_msg = next(m for m in result["messages"] if m.get("role") == "tool")
    assert INJECTION_TEXT in tool_msg["content"] and tool_msg["content"].startswith("【外部工具数据")
