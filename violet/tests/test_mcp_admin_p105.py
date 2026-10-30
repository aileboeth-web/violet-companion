"""Phase 10.5: MCP administration -- adding a server in the Control Center, connection errors in plain words, the
permission-template mismatch (and its one-click fix), per-tool classification with unclassified tools still refused,
the second key for write / high-risk tools, agent authorisation and the read-only tool test.

Fake servers only: tools/fake_mcp_server.py over stdio and over loopback HTTP; temp data dir and policy file."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.fake_mcp import CAMPUS_TOOLS

ADMIN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}
JSON = {"Accept": "application/json"}
PROJECT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import ai.control_config as cc
    import ai.tool_manager as tm
    saved = dict(os.environ)
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"version": 1, "servers": {
        "campus-template": {"tools": dict(CAMPUS_TOOLS)},
        "other-template": {"tools": {"unrelated_tool": "read"}}}}), encoding="utf-8")
    monkeypatch.setenv("MCP_TOOL_POLICY_FILE", str(policy))
    monkeypatch.delenv("MCP_SERVERS", raising=False)
    monkeypatch.setenv("VIOLET_MCP_AUTO_CLASSIFY", "0")      # the strict allowlist (2026-10-30 default: auto levels)
    cc._ORIGINALS.clear()
    cc._DEFAULT_STORE = None
    tm._STORE = None
    yield tmp_path
    os.environ.clear()
    os.environ.update(saved)
    cc._ORIGINALS.clear()
    cc._DEFAULT_STORE = None
    tm._STORE = None


@pytest.fixture()
def admin(env, violetdb):
    from wx_ilink_py import ILinkClient, create_app
    app = create_app(client=ILinkClient(data_dir=env / "data", auto_login=False), violetdb=violetdb, proactive_scheduler=None,
                     all_agents=[], admin_token=ADMIN)
    app.testing = True
    c = app.test_client()
    assert c.post("/auth/login", data={"token": ADMIN}, headers=SAME).status_code == 302
    return c


def _stdio(name="phone", **extra):
    return {"name": name, "transport": "stdio", "command": [sys.executable, "-m", "tools.fake_mcp_server"],
            "agents": ["chat"], "connect_timeout": 10, "call_timeout": 10, **extra}


def _servers(admin):
    return {s["name"]: s for s in admin.get("/api/control/tools", headers=JSON).get_json()["data"]["servers"]}


def _refresh(admin, name):
    return {s["name"]: s for s in admin.post("/api/control/tools/refresh", json={"server": name},
                                             headers=SAME).get_json()["data"]["servers"]}[name]


# ------------------------------------------------------------------------------------------------ template mismatch

def test_a_server_whose_name_matches_no_template_is_explained_and_fixed_with_one_click(admin, monkeypatch):
    monkeypatch.setenv("MCP_SERVERS", json.dumps([_stdio("phone")]))
    server = _refresh(admin, "phone")
    assert server["status"] == "connected" and all(t["permission"] == "unknown" for t in server["tools"])
    assert all(not t["available"] and "未分类" in t["reason"] for t in server["tools"])
    finding = server["diagnosis"][0]
    assert finding["code"] == "profile_mismatch" and finding["profile"] == "campus-template"
    assert "全部被拒绝" in finding["message"] and "phone" in finding["message"]
    assert admin.post("/api/control/tools/servers/phone/profile", json={"profile": "campus-template"},
                      headers=SAME).status_code == 200
    server = _refresh(admin, "phone")
    assert server["tool_profile"] == "campus-template" and all(t["available"] for t in server["tools"])
    assert server["diagnosis"][0]["code"] == "ok"
    from ai.tool_manager import discover
    assert {t.name for t in discover().for_agent("chat")} == {f"phone__{t}" for t in CAMPUS_TOOLS}
    assert admin.post("/api/control/tools/servers/phone/profile", json={"profile": "made-up"},
                      headers=SAME).status_code == 400


def test_unclassified_tools_stay_refused_until_each_one_is_classified(admin, monkeypatch):
    monkeypatch.setenv("MCP_TOOL_POLICY_FILE", str(Path(os.environ["MCP_TOOL_POLICY_FILE"]).with_name("none.json")))
    monkeypatch.setenv("MCP_SERVERS", json.dumps([_stdio("campus")]))
    server = _refresh(admin, "campus")
    assert server["diagnosis"][0]["code"] in ("tools_unclassified", "profile_mismatch")
    import re
    assert not any(re.search(r"/(all|bulk|allow[-_]all|approve[-_]all)(/|$)", r.rule)
                   for r in admin.application.url_map.iter_rules() if "/tools" in r.rule)   # no "allow everything"
    url = "/api/control/tools/servers/campus/tools/{}/level"
    assert admin.post(url.format("get_weather"), json={"level": "read"}, headers=SAME).status_code == 200
    assert admin.post(url.format("search_web"), json={"level": "deny"}, headers=SAME).status_code == 200
    assert admin.post(url.format("get_weather"), json={"level": "admin"}, headers=SAME).status_code == 400
    tools = {t["name"]: t for t in _refresh(admin, "campus")["tools"]}
    assert tools["get_weather"]["available"] and tools["get_weather"]["level_source"] == "admin"
    assert not tools["search_web"]["enabled"] and "拒绝" in tools["search_web"]["reason"]
    assert tools["get_course_schedule"]["permission"] == "unknown" and not tools["get_course_schedule"]["available"]
    from ai.tool_manager import discover
    assert [t.name for t in discover().for_agent("chat")] == ["campus__get_weather"]


def test_write_tools_need_a_separate_confirmed_authorisation_and_are_never_test_called(admin, monkeypatch):
    from ai import permissions                     # "不允许" (the 2026-10-30 default "先问你" offers them with approval)
    monkeypatch.setattr(permissions, "level", lambda ability: "forbid")
    monkeypatch.setenv("MCP_SERVERS", json.dumps([_stdio("campus", tool_profile="campus-template")]))
    url = "/api/control/tools/servers/campus/tools/get_course_schedule"
    assert admin.post(url + "/level", json={"level": "write"}, headers=SAME).status_code == 200
    tools = {t["name"]: t for t in _refresh(admin, "campus")["tools"]}
    assert tools["get_course_schedule"]["permission"] == "write" and not tools["get_course_schedule"]["available"]
    assert "单独授权" in tools["get_course_schedule"]["reason"]
    assert admin.post(url + "/allow", json={"allowed": True}, headers=SAME).status_code == 400          # no confirm
    assert admin.post("/api/control/tools/servers/campus/tools/get_weather/allow", json={"allowed": True, "confirm": True},
                      headers=SAME).status_code == 400                                                   # a read tool
    assert admin.post(url + "/allow", json={"allowed": True, "confirm": True}, headers=SAME).status_code == 200
    tools = {t["name"]: t for t in _refresh(admin, "campus")["tools"]}
    assert tools["get_course_schedule"]["available"] and tools["get_course_schedule"]["allowed_risky"]
    test = admin.post(url + "/test", json={"arguments": {"day": "周一"}}, headers=SAME).get_json()["data"]
    assert test["ok"] is False and test["status"] == "not_read_only"
    # back to read removes the second key
    admin.post(url + "/level", json={"level": "read"}, headers=SAME)
    from ai.tool_manager import tool_store
    assert "get_course_schedule" not in (tool_store().servers()["campus"].get("allow_tools") or [])


def test_a_read_tool_can_be_tested_with_its_arguments_checked(admin, monkeypatch):
    monkeypatch.setenv("MCP_SERVERS", json.dumps([_stdio("campus", tool_profile="campus-template")]))
    url = "/api/control/tools/servers/campus/tools/get_weather/test"
    ok = admin.post(url, json={"arguments": {"city": "示例市"}}, headers=SAME).get_json()["data"]
    assert ok["ok"] and "虚构" in ok["result"] and ok["latency_ms"] >= 0
    bad = admin.post(url, json={"arguments": {}}, headers=SAME).get_json()["data"]
    assert bad["ok"] is False and bad["status"] == "invalid_arguments"
    missing = admin.post("/api/control/tools/servers/campus/tools/nope/test", json={}, headers=SAME).get_json()["data"]
    assert missing["ok"] is False and missing["status"] == "not_found"


def test_agent_authorisation_decides_which_agent_gets_the_tools(admin, monkeypatch):
    monkeypatch.setenv("MCP_SERVERS", json.dumps([_stdio("campus", tool_profile="campus-template")]))
    _refresh(admin, "campus")
    from ai.tool_manager import discover
    assert discover().for_agent("chat") and not discover().for_agent("performer")
    assert admin.post("/api/control/tools/servers/campus", json={"agents": ["performer"]}, headers=SAME).status_code == 200
    catalog = discover()
    assert not catalog.for_agent("chat") and catalog.for_agent("performer")
    assert admin.post("/api/control/tools/servers/campus", json={"agents": []}, headers=SAME).status_code == 200
    server = _refresh(admin, "campus")
    assert any(f["code"] == "no_agent" for f in server["diagnosis"])


# ------------------------------------------------------------------------------------------------ add a server over HTTP

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def http_mcp():
    port = _free_port()
    env = {**os.environ, "FAKE_MCP_TOKEN": "fake-mcp-token-p105-0000000000", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, "-m", "tools.fake_mcp_server", "--http", str(port)], cwd=PROJECT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.2)
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()
    proc.wait(5)


def test_adding_an_http_server_in_the_dashboard_with_a_wrong_then_right_token(admin, http_mcp):
    body = {"name": "campus-http", "url": http_mcp, "transport": "streamable_http", "agents": ["chat"],
            "header": "Authorization", "header_value": "Bearer wrong-token-000000000000"}
    created = admin.post("/api/control/tools/servers", json=body, headers=SAME)
    assert created.status_code == 201
    server = created.get_json()["data"]
    assert server["custom"] and server["status"] == "unavailable"
    assert server["diagnosis"][0]["code"] == "mcp_not_connected" and "凭据" in server["diagnosis"][0]["message"]
    text = admin.get("/api/control/tools", headers=JSON).get_data(as_text=True)
    assert "wrong-token" not in text
    assert admin.post("/api/control/tools/servers/campus-http/secret", json={
        "header": "Authorization", "value": "Bearer fake-mcp-token-p105-0000000000", "confirm": True},
        headers=SAME).status_code == 200
    server = _refresh(admin, "campus-http")
    assert server["status"] == "connected" and {t["name"] for t in server["tools"]} == set(CAMPUS_TOOLS)
    assert server["diagnosis"][0]["code"] == "profile_mismatch" and server["diagnosis"][0]["profile"] == "campus-template"
    # validation: duplicate name, plain http to a remote host, a command server, query in the url
    for bad, field in (({**body, "header_value": None}, "name"),
                       ({**body, "name": "x2", "url": "http://198.51.100.7/mcp"}, "url"),
                       ({**body, "name": "x3", "transport": "stdio"}, "transport"),
                       ({**body, "name": "x4", "url": "https://example.org/mcp?key=abc"}, "url")):
        resp = admin.post("/api/control/tools/servers", json=bad, headers=SAME)
        assert resp.status_code == 400 and field in resp.get_json()["problems"], (field, resp.get_json())
    assert admin.post("/api/control/tools/servers/campus-http/delete", json={}, headers=SAME).status_code == 400
    assert admin.post("/api/control/tools/servers/campus-http/delete", json={"confirm": True}, headers=SAME).status_code == 200
    from ai.tool_manager import tool_store
    assert "campus-http" not in _servers(admin) and "campus-http" not in tool_store().secret_headers()


def test_servers_from_env_can_be_switched_and_classified_but_not_deleted_or_repointed(admin, monkeypatch):
    monkeypatch.setenv("MCP_SERVERS", json.dumps([_stdio("phone")]))
    assert admin.post("/api/control/tools/servers/phone/delete", json={"confirm": True}, headers=SAME).status_code == 404
    assert admin.post("/api/control/tools/servers/phone/edit", json={"url": "https://x.example/mcp"},
                      headers=SAME).status_code == 404
    assert admin.post("/api/control/tools/servers/phone/tools/get_weather/level", json={"level": "read"},
                      headers=SAME).status_code == 200
