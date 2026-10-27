"""Phase 6 review of phase 5B leftovers and of tool permissions (offline)."""

from __future__ import annotations

import json

import pytest

from tests.test_web_security import make_fetcher, service as web_service
from tests.web_fixtures import LocalWeb, searxng_payload


@pytest.fixture()
def web():
    w = LocalWeb()
    yield w
    w.close()


def test_searxng_trusted_exception_cannot_be_used_by_ordinary_requests(violetdb, web, monkeypatch):
    from ai.web.netguard import NetPolicy, SafeFetcher
    from ai.web.providers import make_provider
    web.routes["127.0.0.1/search"] = {"body": searxng_payload(
        {"title": "书评", "url": "https://books.example.org/r", "content": "x"}), "headers": {"Content-Type": "application/json"}}
    monkeypatch.setenv("VIOLET_WEB_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("VIOLET_WEB_SEARXNG_URL", f"http://127.0.0.1:{web.port}")
    provider = make_provider(make_fetcher(web))
    svc = web_service(violetdb, web, provider)
    assert svc.search("书评")["ok"]                                         # the provider itself works
    # fetch_url to the provider endpoint (or anything on its host / port) is refused
    for url in (f"http://127.0.0.1:{web.port}/search?q=x", f"http://127.0.0.1:{web.port}/", "http://127.0.0.1/"):
        assert svc.fetch(url)["error_code"].startswith("blocked_")
    # a public-looking SearXNG host is on the internal block list for ordinary requests
    monkeypatch.setenv("VIOLET_WEB_SEARXNG_URL", "https://searx.example.net")
    fetcher = SafeFetcher(NetPolicy.from_env(), resolver=web.resolver, dialer=web.dialer)
    from ai.web.netguard import WebPolicyError
    with pytest.raises(WebPolicyError) as info:
        fetcher.check_url("https://searx.example.net/search?q=x")
    assert info.value.code == "host"
    # a search result pointing back at the provider cannot be fetched either
    assert not any("127.0.0.1" in r["url"] for r in svc.search("书评")["results"])


def test_stdio_server_budget_survives_restart(tmp_path, monkeypatch, web):
    from ai.web.providers import MockSearchProvider
    from ai.web.service import WebConfig, WebService
    monkeypatch.setenv("VIOLET_WEB_BUDGET_FILE", str(tmp_path / "web_budget_state.json"))
    provider = MockSearchProvider()
    first = WebService(None, fetcher=make_fetcher(web), provider=provider,
                       config=WebConfig(enabled=True, max_searches_per_day=2))
    assert first.search("小说")["ok"] and first.search("专辑")["ok"]
    restarted = WebService(None, fetcher=make_fetcher(web), provider=provider,
                           config=WebConfig(enabled=True, max_searches_per_day=2))
    assert restarted.search("活动")["error_code"] == "budget" and len(provider.calls) == 2
    state = json.loads((tmp_path / "web_budget_state.json").read_text(encoding="utf-8"))
    assert list(state.values())[0]["search"] == 2


def test_only_the_chat_agent_gets_web_or_image_tools(monkeypatch):
    from ai.agents import ChatAgent, IntentAgent, MemoryAgent, PerformerAgent, ProactiveAgent
    from ai.chat import get_mcp_servers
    from ai.mcp import load_tool_policy, server_applies_to_agent
    monkeypatch.setenv("VIOLET_WEB_ENABLED", "1")
    monkeypatch.setenv("VIOLET_IMAGE_ENABLED", "1")
    servers = get_mcp_servers()
    for agent in (IntentAgent(), MemoryAgent(), PerformerAgent(), ProactiveAgent()):
        assert not [s for s in servers if s["name"] == "violet-web" and server_applies_to_agent(s, agent.mcp_agent_name)]
        names = {t["function"]["name"] for t in agent._tools}
        assert not names & {"create_image", "edit_image", "look_at_image"}, agent
    chat = ChatAgent()
    assert {"create_image", "edit_image", "look_at_image"} <= {t["function"]["name"] for t in chat._tools}
    # the web MCP tools are classified read-only; nothing unlisted can be granted
    assert load_tool_policy()["servers"]["violet-web"]["tools"] == {"web_search": "read", "fetch_url": "read"}


@pytest.mark.parametrize("line, flagged", [
    ("我刚拍的自拍", True),
    ("这是我用手机拍的", True),
    ("用摄像头拍的，原图直出", True),
    ("这是一张真实照片", True),
    ("这不是照片，是我画给你的", False),
    ("你的自拍好可爱", False),
    ("你拍的照片真好看", False),
    ("给你画了一张", False),
])
def test_camera_claims(violetdb, line, flagged):
    from ai.consistency import ConsistencyGuard
    checks = [v.check for v in ConsistencyGuard(violetdb).check_reply(line)]
    assert ("camera_claim" in checks) is flagged, (line, checks)
