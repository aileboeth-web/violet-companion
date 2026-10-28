"""Violet 0.7.4: "必应直连" -- Bing's RSS search results as a search provider (no key, nothing to install), chosen in
the Control Center; removing SearXNG no longer switches web search off while Bing is the search service. Fake
fetcher; no network."""

from __future__ import annotations

import json
import os

import pytest

from ai.web.netguard import SafeFetcher, WebPolicyError
from ai.web.providers import BingRssProvider, SearchError, make_provider

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}

RSS = ('<?xml version="1.0" encoding="utf-8" ?><rss version="2.0"><channel><title>必应：天气</title>'
       '<item><title>北京天气预报</title><link>https://weather.example.cn/beijing</link>'
       '<description>今天晴，最高 18 度。</description><pubDate>Tue, 28 Oct 2026 08:00:00 GMT</pubDate></item>'
       '<item><title>坏链接</title><link>javascript:alert(1)</link><description>x</description></item>'
       '<item><title>上海天气</title><link>https://weather.example.cn/shanghai</link><description>多云</description></item>'
       '</channel></rss>')


class Fetcher:
    def __init__(self, text=None, error=None):
        self.text_, self.error, self.urls = text, error, []

    def fetch(self, url, **kw):
        self.urls.append((url, kw))
        if self.error:
            raise self.error
        body = self.text_

        class R:
            def text(self):
                return body
        return R()


def test_bing_rss_results_become_search_results():
    f = Fetcher(RSS)
    out = BingRssProvider(f).search("天气", limit=5)
    assert [r.title for r in out] == ["北京天气预报", "上海天气"]            # the javascript: link is dropped
    assert out[0].url == "https://weather.example.cn/beijing" and out[0].snippet.startswith("今天晴")
    assert out[0].engine == "bing" and out[0].published_at
    url, kw = f.urls[0]
    assert url.startswith("https://cn.bing.com/search?") and "format=rss" in url and "q=%E5%A4%A9%E6%B0%94" in url
    assert "application/rss+xml" in kw["accept_types"]
    assert BingRssProvider(Fetcher(RSS)).search("天气", limit=1)[0].title == "北京天气预报"


def test_bing_failures_are_search_errors(monkeypatch):
    with pytest.raises(SearchError) as info:
        BingRssProvider(Fetcher("<html><body>请完成验证</body>")).search("x")
    assert info.value.code == "blocked"
    with pytest.raises(SearchError) as info:
        BingRssProvider(Fetcher(error=WebPolicyError("timeout", "total time limit reached"))).search("x")
    assert info.value.code == "timeout"
    monkeypatch.setenv("VIOLET_WEB_BING_HOST", "evil.example")              # only the two Bing hosts
    assert BingRssProvider(Fetcher(RSS)).host == "cn.bing.com"
    monkeypatch.setenv("VIOLET_WEB_BING_HOST", "www.bing.com")
    assert BingRssProvider(Fetcher(RSS)).host == "www.bing.com"


def test_make_provider_gives_bing_a_browser_user_agent(monkeypatch):
    monkeypatch.setenv("VIOLET_WEB_SEARCH_PROVIDER", "bing")
    provider = make_provider(SafeFetcher())
    assert isinstance(provider, BingRssProvider)
    assert provider.fetcher.policy.user_agent.startswith("Mozilla/5.0")


@pytest.fixture()
def env_guard(tmp_path, monkeypatch):
    import ai.control_config as cc
    saved = dict(os.environ)
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    cc._ORIGINALS.clear()
    cc._DEFAULT_STORE = None
    yield tmp_path / "data"
    os.environ.clear()
    os.environ.update(saved)
    cc._ORIGINALS.clear()
    cc._DEFAULT_STORE = None


@pytest.fixture()
def admin(env_guard, violetdb):
    from wx_ilink_py import ILinkClient, create_app
    app = create_app(client=ILinkClient(data_dir=env_guard, auto_login=False), violetdb=violetdb, proactive_scheduler=None,
                     all_agents=[], admin_token=TOKEN)
    app.testing = True
    c = app.test_client()
    assert c.post("/auth/login", data={"token": TOKEN}, headers=SAME).status_code == 302
    return c


def test_page_switches_to_bing_and_removing_searxng_keeps_it_on(admin, env_guard, violetdb):
    url = "/api/control/tools/web/searxng"
    assert admin.post(url, json={"action": "bing", "confirm": True}, headers=SAME).status_code == 200
    s = admin.get(url).get_json()["data"]
    assert s["provider"] == "bing" and s["web_enabled"] is True
    from ai.tool_manager import web_status
    assert web_status(violetdb)["search_configured"] is True
    assert admin.post(url, json={"action": "disable", "confirm": True}, headers=SAME).status_code == 200
    s = admin.get(url).get_json()["data"]
    assert s["web_enabled"] is True and s["provider"] == "bing"              # SearXNG gone, Bing still searching
    # with SearXNG as the search service, removing it switches web search off
    (env_guard / "control" / "searxng.json").write_text(json.dumps({"url": "http://127.0.0.1:18089"}), encoding="utf-8")
    assert admin.post(url, json={"action": "enable", "confirm": True}, headers=SAME).status_code == 200
    assert admin.post(url, json={"action": "disable", "confirm": True}, headers=SAME).status_code == 200
    assert admin.get(url).get_json()["data"]["web_enabled"] is False
