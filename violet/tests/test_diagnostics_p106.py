"""Phase 10.6: the unified diagnosis -- each failure category is told apart, explained, and fixed in one click where
that is safe; the turn id correlates diagnosis, inbox / outbox and log lines. Fake model, fake channels and MCP."""

from __future__ import annotations

import json
import logging
import os
import sys
import time

import httpx
import openai
import pytest

from tests.fake_channels import TOKEN, MemoryChannel
from tests.fake_mcp import CAMPUS_TOOLS
from tests.fakes import wait_until
from tests.test_channels_p10_runtime import _bind, world  # noqa: F401  (fixture)

ADMIN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}
JSON = {"Accept": "application/json"}


@pytest.fixture(autouse=True)
def _fresh_events():
    from ai import diagnostics
    diagnostics.reset_for_tests()
    yield
    diagnostics.reset_for_tests()


@pytest.fixture()
def app(world):  # noqa: F811
    import ai.control_config as cc
    import ai.tool_manager as tm
    cc._DEFAULT_STORE = None
    tm._STORE = None
    from wx_ilink_py import ILinkClient, create_app
    agents = list(world.rt.agents.values())
    app = create_app(client=ILinkClient(data_dir=world.data, auto_login=False), violetdb=world.db, proactive_scheduler=None,
                     all_agents=agents, runtime=world.rt, admin_token=ADMIN)
    app.testing = True
    c = app.test_client()
    assert c.post("/auth/login", data={"token": ADMIN}, headers=SAME).status_code == 302
    yield c
    cc._DEFAULT_STORE = None
    tm._STORE = None


def _report(app, kind=None):
    findings = app.get("/api/control/diagnostics", headers=JSON).get_json()["data"]["findings"]
    return [f for f in findings if kind is None or f["kind"] == kind]


class BrokenChannel(MemoryChannel):
    def receive_once(self) -> int:
        raise ConnectionError("upstream reset at https://api.telegram.org/bot" + TOKEN + "/getUpdates")


def test_a_channel_that_is_down_is_named_with_its_reason_and_can_be_restarted(world, app):  # noqa: F811
    world.mgr._factory_overrides["telegram"] = BrokenChannel
    world.rt.start(poll=False)
    world.mgr.start_enabled()
    world.mgr.add("telegram", "tg", secrets={"bot_token": TOKEN})
    assert wait_until(lambda: world.mgr.get("tg").status().state in ("reconnecting", "error"))
    down = [f for f in _report(app, "channel_down") if f["level"] == "error"]
    assert down and "Telegram" in down[0]["message"] and TOKEN not in json.dumps(down)
    assert down[0]["fix"] == {"action": "restart_channel", "target": "tg"}
    world.mgr._factory_overrides["telegram"] = MemoryChannel
    assert app.post("/api/control/diagnostics/fix", json=down[0]["fix"], headers=SAME).status_code == 200
    assert wait_until(lambda: world.mgr.get("tg").status().state == "connected")
    ok = _report(app, "channel_down")
    assert all(f["level"] != "error" for f in ok) and any("还没有绑定" in f["message"] for f in ok)


def test_a_failing_model_is_told_apart_with_the_turn_that_hit_it(world, app, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_API_KEY", "sk-test-p106-0000000000")
    monkeypatch.setenv("LLM_API_URL", "http://127.0.0.1:9/v1")
    world.rt.start(poll=False)
    world.mgr.start_enabled()
    world.mgr.add("telegram", "tg", secrets={"bot_token": TOKEN})
    adapter = world.mgr.get("tg")
    _bind(world, adapter)

    def denied(kw):
        raise openai.AuthenticationError("bad key sk-test-p106", response=httpx.Response(
            401, request=httpx.Request("POST", "http://127.0.0.1:9/v1")), body=None)
    world.llm.default = denied
    world.llm.responders.clear()
    adapter.push("在吗")
    assert wait_until(lambda: any("卡了一下" in t for t in adapter.sent_texts))
    turn = world.db.fetch_one("SELECT turn_id FROM inbound_messages WHERE content = '在吗'")["turn_id"]
    model = [f for f in _report(app, "model_failed") if f["level"] == "error"]
    assert model and "API Key" in model[0]["message"] and turn in model[0]["turns"]
    assert "sk-test" not in json.dumps(model) and model[0]["fix"] == {"action": "retest_model"}
    world.llm.default = lambda kw: "在的"
    result = app.post("/api/control/diagnostics/fix", json={"action": "retest_model"}, headers=SAME).get_json()["data"]
    assert result["ok"]


def test_mcp_not_connected_and_unauthorized_tools_are_separate_findings_with_safe_fixes(world, app, monkeypatch, tmp_path):  # noqa: F811
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"servers": {"campus-template": {"tools": dict(CAMPUS_TOOLS)}}}), encoding="utf-8")
    monkeypatch.setenv("MCP_TOOL_POLICY_FILE", str(policy))
    down = {"name": "relay", "transport": "stdio", "command": [sys.executable, "-c", "import sys; sys.exit(1)"],
            "agents": ["chat"], "connect_timeout": 5}
    phone = {"name": "phone", "transport": "stdio", "command": [sys.executable, "-m", "tools.fake_mcp_server"],
             "agents": ["chat"], "connect_timeout": 10}
    monkeypatch.setenv("MCP_SERVERS", json.dumps([down, phone]))
    app.post("/api/control/tools/refresh", json={}, headers=SAME)
    findings = _report(app)
    not_connected = [f for f in findings if f["kind"] == "mcp_not_connected"]
    unauthorized = [f for f in findings if f["kind"] == "tool_unauthorized"]
    assert not_connected and "relay" in not_connected[0]["message"] and not_connected[0]["fix"]["action"] == "check_mcp"
    assert unauthorized and "phone" in unauthorized[0]["message"] and "全部被拒绝" in unauthorized[0]["message"]
    fix = unauthorized[0]["fix"]
    assert fix == {"action": "apply_profile", "target": "phone", "profile": "campus-template"}
    stale = app.post("/api/control/diagnostics/fix", json={**fix, "profile": "something-else"}, headers=SAME)
    assert stale.status_code == 409
    assert app.post("/api/control/diagnostics/fix", json=fix, headers=SAME).status_code == 200
    assert not [f for f in _report(app, "tool_unauthorized") if "phone" in f["message"]]


def test_tool_planning_and_tool_call_failures_are_reported_per_turn(app):
    from ai import diagnostics
    from ai.chat import _record_tool_plan
    from ai.agents.base_agent import _record_tool_call
    with diagnostics.turn_scope("t-1"):
        _record_tool_plan({"tool_plan": {"needed": True, "mode": "chat", "status": "ok", "tools": ["campus__get_weather"]},
                           "chat": {"tools_called": []}})
    with diagnostics.turn_scope("t-2"):
        _record_tool_plan({"tool_plan": {"needed": True, "mode": "blocked", "status": "not_configured",
                                         "tools": ["campus__old_name"]}, "chat": {}})
    with diagnostics.turn_scope("t-3"):
        _record_tool_call("campus__get_weather", json.dumps({"ok": False, "error_code": "MCP_TIMEOUT"}), "ChatAgent")
        _record_tool_call("campus__get_weather", "【外部工具数据｜来源 campus/get_weather】\n{}", "ChatAgent")
    planned = _report(app, "tool_not_planned")
    assert any("没有调用" in f["message"] and "t-1" in f["turns"] for f in planned)
    assert any("campus__old_name" in f["message"] and "t-2" in f["turns"] for f in planned)
    failed = _report(app, "tool_failed")
    assert len(failed) == 1 and "MCP_TIMEOUT" in failed[0]["message"] and "1 次" in failed[0]["message"]


def test_service_ok_but_outside_network_unreachable_is_its_own_finding(world, app, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_API_URL", "http://127.0.0.1:9/v1")          # nothing listens there (no DNS in tests)
    monkeypatch.setenv("LLM_API_KEY", "sk-test-p106-0000000000")
    result = app.post("/api/control/diagnostics/fix", json={"action": "network_check"}, headers=SAME).get_json()["data"]
    assert result["results"][0]["ok"] is False and result["results"][0]["stage"] in ("dns", "tcp")
    network = _report(app, "network_unreachable")
    assert network[0]["level"] == "error" and "外部网络不可达" in network[0]["message"] and "模型接口" in network[0]["message"]


def test_log_lines_of_a_turn_carry_its_correlation_id(world, caplog):  # noqa: F811
    world.rt.start(poll=False)
    world.mgr.start_enabled()
    world.mgr.add("telegram", "tg", secrets={"bot_token": TOKEN})
    adapter = world.mgr.get("tg")
    _bind(world, adapter)
    original = world.rt.pipeline_runner

    def logging_pipeline(**kwargs):
        logging.getLogger("violet.test").info("inside the turn")
        return original(**kwargs)
    world.rt.pipeline_runner = logging_pipeline
    with caplog.at_level(logging.INFO):
        adapter.push("早")
        assert wait_until(lambda: "我在听" in adapter.sent_texts)
    turn = world.db.fetch_one("SELECT turn_id FROM inbound_messages WHERE content = '早'")["turn_id"]
    tagged = [r for r in caplog.records if getattr(r, "turn", "") == f" [turn {turn}]"]
    assert tagged, "no log line of the turn carries its id"
    from ai.settings import LOG_FORMAT
    assert "%(turn)s" in LOG_FORMAT and logging.Formatter(LOG_FORMAT).format(tagged[0])


def test_a_model_service_on_this_machine_is_reached_directly_even_with_a_system_proxy(monkeypatch, tmp_path):
    """The 10.3 acceptance run got a 502 from a system proxy for 127.0.0.1: the real OpenAI client must go direct."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps({"id": "x", "object": "chat.completion", "created": 1, "model": "m",
                               "choices": [{"index": 0, "finish_reason": "stop",
                                            "message": {"role": "assistant", "content": "在的"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        from ai.agents.base_agent import BaseAgent
        monkeypatch.setattr(BaseAgent, "client_factory", None)
        monkeypatch.setenv("LLM_API_URL", f"http://127.0.0.1:{server.server_address[1]}/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test-p106-0000000000")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")            # a proxy that does not exist
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
        from ai.agents import ChatAgent
        from ai.setup_status import test_model
        result = test_model(ChatAgent(), data_dir=tmp_path / "data")
        assert result["ok"], result
    finally:
        server.shutdown()