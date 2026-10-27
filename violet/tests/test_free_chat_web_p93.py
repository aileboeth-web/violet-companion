"""Phase 9.3 stage B: free_chat --web on (offline).

The child process runs the real free_chat with a fake OpenAI SDK, and the loopback LocalWeb plays the Bocha API and
the public pages: ai.web.netguard's resolver / dialer are swapped for LocalWeb's, the Bocha endpoint is plain http,
everything else is the product path (Intent catalog -> plan -> Chat, violet-web in-process, WebService, SafeFetcher,
the harness network guard). What is proven: real web is off unless chosen at creation and needs --real, its own key
and its own "yes"; the session's web caps count real HTTP connections (redirects too) across launches and refuse past
them; each round records Intent's plan, the offered tools / tool_choice (9.2.2), the searches and page reads field by
field, every HTTP connection and the links kept / removed in the reply; the key never reaches a page or a file."""
from __future__ import annotations

import json
import subprocess
import sys

from tests.test_free_chat_budget_p911 import KEY, child_env, real

SKEY = "fake-search-key-p93-0123456789"
SEARCH, FETCH = "violet-web__web_search", "violet-web__fetch_url"

BOOT = r'''
import atexit, getpass, json, os, sys, time, uuid
from unittest.mock import patch
from openai.types.chat import ChatCompletion
from tools import chat_dialogue as cd
from tools.harness import _AGENT
from tests.fakes import intent_json
from tests import web_fixtures
getpass.getpass = lambda prompt="": ""           # never wait on the console; keys come from the environment
SKEY = "fake-search-key-p93-0123456789"           # the one key the fake Bocha accepts (a wrong one gets 401)
web_fixtures.PUBLIC["api.bochaai.com"] = ["93.184.216.50"]
if os.environ.get("P93_PROVIDER_PRIVATE"):         # the trusted provider host resolving into the internal network
    web_fixtures.PUBLIC["api.bochaai.com"] = ["10.0.0.9"]
web = web_fixtures.LocalWeb()
def bocha(req):
    if req.headers.get("Authorization") != "Bearer " + SKEY:
        return {"status": 401, "body": "{}", "headers": {"Content-Type": "application/json"}}
    q = json.loads(req._body or b"{}").get("query", "")
    items = [] if "没有结果" in q else [{"name": "Violet 2.0 发布说明", "url": "http://news.example.org/release",
             "snippet": "新版本加入了联网搜索", "siteName": "示例新闻", "datePublished": "2026-10-01T10:00:00+08:00"}]
    return {"body": json.dumps({"code": 200, "data": {"webPages": {"value": items}}}, ensure_ascii=False),
            "headers": {"Content-Type": "application/json"}}
web.routes["api.bochaai.com/v1/web-search"] = bocha
web.page("news.example.org", "/release", """<html><head><title>Violet 2.0 发布说明</title>
<meta property="article:published_time" content="2026-10-01T10:00:00+08:00"></head><body><article>
<p>2.0 版本于 10 月 1 日正式发布。</p></article></body></html>""")
web.routes["news.example.org/old"] = {"status": 302, "headers": {"Location": "http://news.example.org/release"}}
import ai.web.netguard as ng, ai.web.providers as providers
ng._default_resolver, ng._default_dialer = web.resolver, web.dialer
providers.BochaProvider.default_endpoint = "http://api.bochaai.com/v1/web-search"
dump = os.environ.get("P93_WEB_DUMP")
if dump:
    atexit.register(lambda: open(dump, "w", encoding="utf-8").write(json.dumps(web.requests, ensure_ascii=False)))
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
def wanted(user):
    if "原文" in user:
        return ["violet-web__web_search", "violet-web__fetch_url"]
    if "http://" in user:
        return ["violet-web__fetch_url"]
    if "最新" in user or "没有结果" in user:
        return ["violet-web__web_search"]
    return []
class FakeSDK(Mock):
    def __init__(self, **kwargs):
        super().__init__()
    def _create(self, **kwargs):
        agent, msgs, model = _AGENT.get(), kwargs.get("messages") or [], str(kwargs.get("model"))
        user = next((m.get("content") or "" for m in reversed(msgs) if m.get("role") == "user"), "")
        offered = [t["function"]["name"] for t in kwargs.get("tools") or []]
        results = "\n".join(m.get("content") or "" for m in msgs if m.get("role") == "tool")
        called = [tc["function"]["name"] for m in msgs if m.get("role") == "assistant" for tc in m.get("tool_calls") or []]
        if agent == "IntentAgent":
            tools = wanted(user)
            extra = {"tool_query": {"needed": True, "tools": tools, "reason": "要查"}} if tools else {}
            return done(intent_json("chat", **extra), model)
        if agent == "ChatAgent":
            if "violet-web__web_search" in offered and "violet-web__web_search" not in called:
                return done(None, model, [("violet-web__web_search", {"query": user.strip()[-20:], "recency_days": 30})])
            if "violet-web__fetch_url" in offered and "violet-web__fetch_url" not in called:
                url = next((w for w in user.split() if w.startswith("http://")), None)
                if url is None and "news.example.org/release" in results:
                    url = "http://news.example.org/release"
                if url:
                    return done(None, model, [("violet-web__fetch_url", {"url": url})])
            if "news.example.org/release" in results:
                return done("查到啦，2.0 已经发布了 http://news.example.org/release 还有 http://made.up/fake-source", model)
            if offered or called:
                return done("刚才没查到呢", model)
            return done("辛苦啦，先歇会儿～", model)
        return done(super()._create(**kwargs).choices[0].message.content, model)
with patch("openai.OpenAI", FakeSDK):
    from tools import free_chat
    raise SystemExit(free_chat.main(json.loads(sys.argv[1])))
'''


def web_launch(argv, lines, dump=None, search_key=SKEY, **extra_env):
    from tools import chat_dialogue as cd
    env = {**child_env(), **extra_env}
    if search_key:
        env["VIOLET_TEST_SEARCH_API_KEY"] = search_key
    if dump:
        env["P93_WEB_DUMP"] = str(dump)
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOT, json.dumps(argv)], cwd=cd.PROJECT, env=env,
                          input="\n".join(lines) + "\n", capture_output=True, text=True, encoding="utf-8", timeout=900)
    return done.returncode, done.stdout + done.stderr


def test_free_chat_web_on_runs_the_real_web_path_traced_and_capped_across_launches(tmp_path):
    from tools import free_chat as fc
    path = tmp_path / "web-1"
    dump = tmp_path / "requests.json"
    code, out = web_launch(real(path, "--web", "on", "--session-max-searches", "2", "--session-max-pages", "3"), [
        "yes", "yes", "Python 最新版本是多少", "你看下这个 http://news.example.org/old 写的啥", "帮我搜一下再看看原文",
        "今天好累啊", "/usage", "/quit"], dump=dump)
    assert code == 0, out[-3000:]
    meta = fc.read_meta(path)
    assert meta["web"] == "on" and meta["web_provider"] == "bocha"
    assert meta["web_budget"]["max_search_requests"] == 2 and meta["web_budget"]["max_page_requests"] == 3
    assert meta["launches"][0]["web"]["provider"] == "bocha"
    events = {e["n"]: e for e in fc.read_jsonl(path / fc.EVENTS)}
    trace = {n: e["tool_trace"] for n, e in events.items()}

    def kinds(n, kind):
        return [t for t in trace[n] if t["kind"] == kind]

    def chat_first(n):
        return next(t for t in kinds(n, "model_call") if t["agent"] == "ChatAgent")
    # 1: Intent asks for a search -> one tool, required; fields of the real result; one HTTP request; made-up link cut
    assert kinds(1, "plan")[0]["tool_query"]["needed"] is True and kinds(1, "plan")[0]["plan"]["mode"] == "chat"
    assert chat_first(1)["offered"] == [SEARCH] and chat_first(1)["tool_choice"] == "required"
    hit = kinds(1, "mcp_call")[0]["web"]["results"][0]
    assert hit == {"title": "Violet 2.0 发布说明", "url": "http://news.example.org/release", "source": "示例新闻",
                   "published_at": hit["published_at"], "published_verified": False} and hit["published_at"]
    assert [(t["what"], t["host"], t["refused"]) for t in kinds(1, "web_http")] == [("搜索", "api.bochaai.com", None)]
    assert kinds(1, "reply_links")[0]["removed"] == ["http://made.up/fake-source"]
    assert "http://news.example.org/release" in events[1]["violet"][0] and "made.up" not in "".join(events[1]["violet"])
    # 2: a link the user sent -> read only; the redirect is a second (counted) page request
    assert chat_first(2)["offered"] == [FETCH] and kinds(2, "mcp_call")[0]["web"]["title"] == "Violet 2.0 发布说明"
    assert [t["what"] for t in kinds(2, "web_http")] == ["网页", "网页"]
    # 3: search then read -> both tools, auto (9.2.2)
    assert sorted(chat_first(3)["offered"]) == sorted([SEARCH, FETCH]) and chat_first(3)["tool_choice"] == "auto"
    assert [t["tool"] for t in kinds(3, "mcp_call")] == [SEARCH, FETCH]
    # 4: small talk -> no web tools, no connection
    assert not kinds(4, "web_http") and not [n for n in chat_first(4)["offered"] if n.startswith("violet-web__")]
    assert fc.web_ledger(path) == {"search": 2, "page": 3, "refused": 0}
    assert "联网（会话累计" in out and "搜索 2 / 2" in out and "联网搜索" in out and "回复里的链接" in out
    # the key went to the provider only, never to a page, never into a file
    sent = json.loads(dump.read_text(encoding="utf-8"))
    assert {r["host"] for r in sent if "Authorization" in r["headers"]} == {"api.bochaai.com"}
    assert all(r["method"] == "GET" for r in sent if r["host"] != "api.bochaai.com")
    files = "".join(p.read_text(encoding="utf-8", errors="replace") for p in path.rglob("*")
                    if p.is_file() and p.suffix in (".json", ".jsonl", ".md", ".txt"))
    assert SKEY not in files and KEY not in files

    # the caps hold across launches: they cannot be raised, and a search past them is refused before it is sent
    code, out = web_launch(real(path, "--session-max-searches", "5"), ["yes", "yes", "/quit"])
    assert code == 2 and "只能调低" in out
    code, out = web_launch(real(path), ["yes", "yes", "最新的消息呢", "/quit"], dump=dump)
    assert code == 0, out[-3000:]
    last = fc.read_jsonl(path / fc.EVENTS)[-1]
    refused = [t for t in last["tool_trace"] if t["kind"] == "web_http"]
    assert [(t["what"], t["refused"]) for t in refused] == [("搜索", "session_cap")]
    assert next(t for t in last["tool_trace"] if t["kind"] == "mcp_call")["ok"] is False
    assert fc.web_ledger(path) == {"search": 2, "page": 3, "refused": 1}
    assert not any(r["host"] == "api.bochaai.com" for r in json.loads(dump.read_text(encoding="utf-8")))


def test_real_web_is_off_by_default_and_needs_a_real_model_its_own_key_and_its_own_yes(tmp_path):
    from tools import free_chat as fc
    code, out = web_launch(real(tmp_path / "plain"), ["yes", "Python 最新版本是多少", "/quit"])
    assert code == 0, out[-3000:]
    meta = fc.read_meta(tmp_path / "plain")
    assert meta["web"] == "off" and meta["launches"][0]["web"] == "off" and not (tmp_path / "plain" / fc.WEB_USAGE).exists()
    assert "即将真实联网" not in out
    code, out = web_launch(["chat", "--session", str(tmp_path / "mock"), "--web", "on"], ["/quit"])
    assert code == 2 and "只用于真实模型会话" in out
    code, out = web_launch(real(tmp_path / "both", "--web", "on", "--fake-tools", "on"), ["yes", "/quit"])
    assert code == 2 and "不能和 --fake-tools on 一起用" in out
    code, out = web_launch(real(tmp_path / "nokey", "--web", "on"), ["yes", "/quit"], search_key="")
    assert code == 2 and "no search key" in out
    code, out = web_launch(real(tmp_path / "no", "--web", "on"), ["yes", "no", "/quit"])
    assert code == 2 and "real web not confirmed" in out and "即将真实联网" in out
    code, out = web_launch(real(tmp_path / "plain", "--web", "on"), ["yes", "/quit"])
    assert code == 2 and "联网是 off" in out


def test_a_failing_or_empty_search_is_reported_and_degrades(tmp_path):
    from tools import free_chat as fc
    path = tmp_path / "web-2"
    code, out = web_launch(real(path, "--web", "on"), ["yes", "yes", "搜一下没有结果的东西", "/quit"])
    assert code == 0, out[-3000:]
    e = fc.read_jsonl(path / fc.EVENTS)[-1]
    w = next(t for t in e["tool_trace"] if t["kind"] == "mcp_call")["web"]
    assert w["ok"] and w["empty"] and w["results"] == []
    assert e["violet"] == ["刚才没查到呢"]
    # a wrong key: the provider says 401 -> auth, the reply does not pretend
    path = tmp_path / "web-3"
    code, out = web_launch(real(path, "--web", "on"), ["yes", "yes", "Python 最新版本是多少", "/quit"],
                           search_key="wrong-search-key-000000")
    assert code == 0, out[-3000:]
    e = fc.read_jsonl(path / fc.EVENTS)[-1]
    call = next(t for t in e["tool_trace"] if t["kind"] == "mcp_call")
    assert call["ok"] is False and call["error_code"] == "auth" and e["violet"] == ["刚才没查到呢"]
    assert fc.web_ledger(path) == {"search": 1, "page": 0, "refused": 0}


def test_the_provider_host_is_rechecked_before_any_connection(tmp_path):
    """SafeFetcher trusts the provider host (no public-address check there); the free_chat dialer still refuses a
    provider address inside the internal network, before connecting."""
    from tools import free_chat as fc
    path = tmp_path / "web-4"
    code, out = web_launch(real(path, "--web", "on"), ["yes", "yes", "Python 最新版本是多少", "/quit"],
                           P93_PROVIDER_PRIVATE="1")
    assert code == 0, out[-3000:]
    rows = fc.read_jsonl(path / fc.WEB_USAGE)
    assert [(r["kind"], r["refused"]) for r in rows] == [("search", "address")]
    assert fc.web_ledger(path) == {"search": 0, "page": 0, "refused": 1}


def test_the_guard_pass_is_for_one_connect_on_one_thread():
    import socket
    import threading
    from tools.harness import NetworkGuard
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]
    guard = NetworkGuard([]).install()
    try:
        guard.pass_next_connect()
        first = socket.create_connection(("127.0.0.1", port), timeout=2)        # passed once
        first.close()
        try:
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
            raise AssertionError("a second connect got through")
        except ConnectionRefusedError as exc:
            assert "network guard" in str(exc)
        guard.pass_next_connect()
        seen = []

        def other():
            try:
                socket.create_connection(("127.0.0.1", port), timeout=2).close()
                seen.append("through")
            except ConnectionRefusedError:
                seen.append("refused")
        t = threading.Thread(target=other)
        t.start()
        t.join()
        assert seen == ["refused"] and guard.web_connections == 1
    finally:
        guard.uninstall()
        server.close()
