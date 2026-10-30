"""Phase 9.3 SearXNG switch (offline): SearXNG is the default search provider, its address is the one trusted
internal endpoint (fetch_url still refuses it and every internal address), its JSON is parsed into the same fields as
before, its failures are told apart (the container down = unavailable), whole turns work through Intent / Tool
Manager, and the Docker Compose deployment files are checked statically (no Docker, no network, no real SearXNG).

Network: the loopback LocalWeb plays SearXNG's /search JSON API and the public pages (tests/web_fixtures.py)."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.fakes import intent_json
from tests.test_phase2_scenarios import make_world, say  # noqa: F401  (make_world: fixture)
from tests.test_tools_p92 import fresh_registry  # noqa: F401  (fixture)
from tests.test_web_p93 import FETCH, PAGE, SEARCH, chat_plays, offered, systems, tool_texts, wants
from tests.test_web_security import make_fetcher
from tests.web_fixtures import LocalWeb

PROJECT = Path(__file__).resolve().parents[1]
DEPLOY = PROJECT / "deploy" / "searxng"
HIT = {"title": "Python 3.14 正式发布", "url": "http://news.example.org/release", "content": "Python 3.14 发布了",
       "publishedDate": "2026-10-07T00:00:00", "engine": "bing", "engines": ["bing"], "score": 1.0}


@pytest.fixture()
def web():
    w = LocalWeb()
    yield w
    w.close()


def searx_route(web, results=(HIT,), unresponsive=(), **spec):
    def handle(req):
        q = parse_qs(urlsplit(req.path).query)
        return {"body": json.dumps({"query": q.get("q", [""])[0], "number_of_results": len(results),
                                    "results": list(results), "answers": [], "suggestions": [],
                                    "unresponsive_engines": [list(u) for u in unresponsive]}, ensure_ascii=False),
                "headers": {"Content-Type": "application/json"}, **spec}
    web.routes["127.0.0.1/search"] = handle


@pytest.fixture()
def searx(web, monkeypatch):
    """The DEFAULT provider (VIOLET_WEB_SEARCH_PROVIDER unset) with VIOLET_WEB_SEARXNG_URL at the loopback LocalWeb."""
    monkeypatch.delenv("VIOLET_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("VIOLET_WEB_SEARXNG_URL", f"http://127.0.0.1:{web.port}")
    return web


def svc(violetdb, web):
    from ai.web.providers import make_provider
    from ai.web.service import WebConfig, WebService
    fetcher = make_fetcher(web)
    return WebService(violetdb, fetcher=fetcher, provider=make_provider(fetcher), config=WebConfig(enabled=True))


def searches(web):
    return [r for r in web.requests if r["path"].startswith("/search")]


# ------------------------------------------------------------------------------------------- provider

def test_searxng_is_the_default_and_needs_its_url(monkeypatch, web):
    from ai.web.providers import DisabledSearchProvider, SearchError, SearxngProvider, make_provider, provider_name
    monkeypatch.delenv("VIOLET_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("VIOLET_WEB_SEARXNG_URL", raising=False)
    assert provider_name() == "searxng"
    provider = make_provider(make_fetcher(web))
    assert isinstance(provider, DisabledSearchProvider)
    with pytest.raises(SearchError) as err:
        provider.search("x")
    assert err.value.code == "not_configured" and "VIOLET_WEB_SEARXNG_URL is not set" in str(err.value)
    monkeypatch.setenv("VIOLET_WEB_SEARXNG_URL", f"http://127.0.0.1:{web.port}")
    assert isinstance(make_provider(make_fetcher(web)), SearxngProvider)
    for other in ("bocha", "brave", "mock", "none"):                          # the others stay selectable
        monkeypatch.setenv("VIOLET_WEB_SEARCH_PROVIDER", other)
        assert make_provider(make_fetcher(web)).name == other


@pytest.mark.parametrize("url,ok", [
    ("http://127.0.0.1:18089", True), ("http://localhost:18089", True), ("http://searxng:8080", True),
    ("http://172.18.0.5:8080", True), ("http://10.0.0.7:8080/", True), ("https://searx.example.net", True),
    ("http://searx.example.net", False), ("http://93.184.216.34:8080", False),
    ("http://user:pw@127.0.0.1:18089", False), ("http://127.0.0.1:18089/?token=x", False),
    ("http://127.0.0.1:18089/#x", False), ("ftp://127.0.0.1", False), ("searxng:8080", False), ("http://", False),
])
def test_only_a_local_or_https_searxng_address_is_trusted(url, ok, monkeypatch, web):
    from ai.web.providers import DisabledSearchProvider, make_provider, searxng_url_problem
    assert (searxng_url_problem(url) is None) is ok
    monkeypatch.setenv("VIOLET_WEB_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("VIOLET_WEB_SEARXNG_URL", url)
    assert isinstance(make_provider(make_fetcher(web)), DisabledSearchProvider) is (not ok)


def test_a_chinese_search_and_the_fields_eve_gets(violetdb, searx):
    searx_route(searx)
    out = svc(violetdb, searx).search("Python 最新版本 发布", recency_days=7)
    assert out["ok"] and out["provider"] == "searxng" and out["retrieved_at"] and "empty" not in out
    sent = searches(searx)[-1]
    q = parse_qs(urlsplit(sent["path"]).query)
    assert q["q"] == ["Python 最新版本 发布"] and q["format"] == ["json"] and q["language"] == ["zh-CN"]
    assert q["time_range"] == ["week"] and q["safesearch"] == ["1"] and sent["method"] == "GET"
    assert "Authorization" not in sent["headers"]                         # no key configured -> no credential
    hit = out["results"][0]
    assert {k: hit[k] for k in ("title", "url", "domain", "source", "snippet", "published_verified")} == {
        "title": "Python 3.14 正式发布", "url": "http://news.example.org/release", "domain": "news.example.org",
        "source": "news.example.org", "snippet": "Python 3.14 发布了", "published_verified": False}
    assert hit["published_at"].startswith("2026-10-07")
    assert "发布时间" in out["notice"] and "不要自己拼或编链接" in out["notice"]


def test_empty_results_are_empty_but_every_engine_failing_is_an_error(violetdb, searx):
    searx_route(searx, results=())
    out = svc(violetdb, searx).search("一个搜不到的词")
    assert out["ok"] and out["empty"] is True and out["results"] == [] and "没有搜到" in out["note"]
    searx_route(searx, results=(), unresponsive=[("google", "timeout"), ("bing", "access denied")])
    out = svc(violetdb, searx).search("另一个词")
    assert out["ok"] is False and out["error_code"] == "provider_error"
    searx_route(searx, results=(HIT,), unresponsive=[("google", "timeout")])     # some engines failing is fine
    assert svc(violetdb, searx).search("第三个词")["ok"]


@pytest.mark.parametrize("spec,code", [
    (dict(status=403), "provider_error"), (dict(status=429), "rate_limited"), (dict(status=500), "provider_error"),
    (dict(delay_headers=1.5), "timeout"), (dict(body="<html>not json</html>"), "invalid"),
])
def test_searxng_failures_are_told_apart(violetdb, searx, spec, code):
    if "body" in spec:
        searx.routes["127.0.0.1/search"] = {"headers": {"Content-Type": "application/json"}, **spec}
    else:
        searx_route(searx, **spec)
    out = svc(violetdb, searx).search("x")
    assert out["ok"] is False and out["error_code"] == code


@pytest.mark.parametrize("url", ["http://127.0.0.1:1", "http://searxng:8080"])
def test_the_container_down_or_not_resolvable_is_unavailable(violetdb, web, monkeypatch, url):
    monkeypatch.delenv("VIOLET_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("VIOLET_WEB_SEARXNG_URL", url)
    out = svc(violetdb, web).search("x")
    assert out["ok"] is False and out["error_code"] == "unavailable" and "连不上" in out["message"]
    row = violetdb.fetch_one("SELECT status, error_code FROM web_requests ORDER BY id DESC LIMIT 1")
    assert (row["status"], row["error_code"]) == ("error", "unavailable")


def test_a_docker_service_name_is_trusted_for_search_only(violetdb, web, monkeypatch):
    """http://searxng:8080 resolves to a private Docker address: the provider may go there, fetch_url never --
    not by that name, not by the address, not by a search result that points back at it."""
    from ai.web.netguard import NetPolicy, SafeFetcher
    from ai.web.providers import make_provider
    from ai.web.service import WebConfig, WebService
    monkeypatch.delenv("VIOLET_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("VIOLET_WEB_SEARXNG_URL", "http://searxng:8080")
    inner = dict(HIT, url="http://172.18.0.5:8080/admin")
    searx_route(web, results=(HIT, inner))

    def resolver(host, port):
        return ["172.18.0.5"] if host == "searxng" else web.resolver(host, port)

    def dialer(ip, port, timeout):
        if (ip, port) == ("172.18.0.5", 8080):
            return socket.create_connection(("127.0.0.1", web.port), timeout=timeout)
        return web.dialer(ip, port, timeout)
    web.routes["searxng/search"] = web.routes["127.0.0.1/search"]
    fetcher = SafeFetcher(NetPolicy.from_env(), resolver=resolver, dialer=dialer)
    service = WebService(violetdb, fetcher=fetcher, provider=make_provider(fetcher), config=WebConfig(enabled=True))
    out = service.search("容器里的搜索")
    assert out["ok"] and [r["url"] for r in out["results"]][:1] == ["http://news.example.org/release"]
    for url in ("http://searxng:8080/", "http://searxng:8080/search?q=x&format=json", "http://172.18.0.5:8080/admin",
                "http://172.18.0.5/", "http://127.0.0.1:18089/"):
        assert service.fetch(url)["error_code"].startswith("blocked_"), url
    assert not any(r["host"] == "searxng" and not r["path"].startswith("/search") for r in web.requests)


def test_a_redirecting_searxng_is_refused_and_a_key_goes_only_there(violetdb, searx, monkeypatch):
    monkeypatch.setenv("VIOLET_WEB_SEARCH_API_KEY", "searx-proxy-key-0123456789")
    searx_route(searx)
    service = svc(violetdb, searx)
    assert service.search("书")["ok"]
    assert searches(searx)[-1]["headers"]["Authorization"] == "Bearer searx-proxy-key-0123456789"
    searx.page("news.example.org", "/release", PAGE)
    assert service.fetch("http://news.example.org/release")["ok"]
    assert "Authorization" not in searx.requests[-1]["headers"]
    searx.routes["127.0.0.1/search"] = {"status": 302, "headers": {"Location": "http://news.example.org/release"}}
    out = service.search("跳转")
    assert out["ok"] is False and out["error_code"] == "redirect"


# ------------------------------------------------------------------------------------------- whole turns

@pytest.fixture()
def searx_world(make_world, monkeypatch, searx):
    """VIOLET_WEB_ENABLED, the default provider (SearXNG at LocalWeb) and the default offer (on_demand)."""
    from ai.web import mcp_server
    from ai.web.providers import make_provider
    from ai.web.service import WebService
    searx_route(searx)
    searx.page("news.example.org", "/release", PAGE)

    def factory():
        from ai.chat import violetdb
        fetcher = make_fetcher(searx)
        return mcp_server.build_server(WebService(violetdb, fetcher=fetcher, provider=make_provider(fetcher)))
    monkeypatch.setattr(mcp_server, "build_default_server", factory)
    monkeypatch.delenv("VIOLET_WEB_OFFER", raising=False)
    w = make_world(VIOLET_WEB_ENABLED="1", VIOLET_WEB_SEARXNG_URL=f"http://127.0.0.1:{searx.port}")
    w.llm.responders["fake-memory"] = lambda kw: json.dumps({"should_save": False, "reason": "t"})
    w.web, w.chats = searx, []
    return w


def test_intent_chooses_search_and_the_reply_cites_a_searxng_result(searx_world):
    w = searx_world
    w.llm.responders["fake-intent"] = wants(SEARCH)
    chat_plays(w, (SEARCH, {"query": "Python 最新版本", "recency_days": 30}),
               final="刚查了一下，3.14 已经发布了（http://news.example.org/release）")
    say(w, "Python 现在最新是哪个版本")
    assert offered(w.chats[0]) == [SEARCH] and w.chats[0]["tool_choice"] == "required"
    assert '"provider": "searxng"' in tool_texts(w.chats[1])[0] and len(searches(w.web)) == 1
    assert "http://news.example.org/release" in w.client.sent_texts[-1]


def test_search_then_read_the_page(searx_world):
    w = searx_world
    w.llm.responders["fake-intent"] = wants(SEARCH, FETCH)
    chat_plays(w, (SEARCH, {"query": "Violet 2.0 发布"}), (FETCH, {"url": "http://news.example.org/release"}),
               final=lambda kw: "看了原文：10 月 1 日正式发布" if any("正式发布" in t for t in tool_texts(kw)) else "没读到")
    say(w, "帮我搜一下 Violet 2.0 什么时候发布的，再看看原文")
    assert w.chats[0]["tool_choice"] == "auto" and sorted(offered(w.chats[0])) == sorted([SEARCH, FETCH])
    assert w.client.sent_texts[-1].startswith("看了原文：10 月 1 日正式发布")
    assert any(r["host"] == "news.example.org" for r in w.web.requests)


def test_small_talk_and_a_switched_off_search_tool_never_reach_searxng(searx_world):
    from ai.tool_manager import tool_store
    w = searx_world
    chat_plays(w, final="早呀～")
    say(w, "早上好呀")
    assert not offered(w.chats[0]) and searches(w.web) == []
    tool_store().set_tool("violet-web", "web_search", False)

    def intent(kw):
        tools = [n for n in dict.fromkeys(re.findall(r"violet-web__\w+", systems(kw))) if n.endswith("web_search")]
        return intent_json("chat", tool_query={"needed": True, "tools": tools, "missing": "联网搜索"})
    w.llm.responders["fake-intent"] = intent
    w.chats.clear()
    chat_plays(w, final="搜索现在关着，我只能说原来知道的")
    say(w, "最新版本是哪个")
    assert not offered(w.chats[0]) and "需要的工具已经停用了" in systems(w.chats[0]) and searches(w.web) == []


def test_searxng_down_degrades_naturally(searx_world, monkeypatch):
    w = searx_world
    monkeypatch.setenv("VIOLET_WEB_SEARXNG_URL", "http://127.0.0.1:1")              # the container is not running
    w.llm.responders["fake-intent"] = wants(SEARCH)
    chat_plays(w, (SEARCH, {"query": "Python 最新版本"}), final="我这边搜索暂时连不上，晚点再帮你查？")
    say(w, "Python 现在最新是哪个版本")
    result = tool_texts(w.chats[-1])[0]
    assert '"error_code": "unavailable"' in result and "搜索服务现在连不上" in result
    assert "http://" not in "".join(w.client.sent_texts) and searches(w.web) == []


def test_the_control_center_shows_why_searxng_is_not_usable(violetdb, monkeypatch, fresh_registry):
    from ai.tool_manager import web_status
    monkeypatch.delenv("VIOLET_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("VIOLET_WEB_SEARXNG_URL", "http://127.0.0.1:18089")
    status = web_status(violetdb)
    assert status["provider"] == "searxng" and status["search_configured"] and status["search_problem"] is None
    monkeypatch.setenv("VIOLET_WEB_SEARXNG_URL", "http://searx.example.net")
    status = web_status(violetdb)
    assert not status["search_configured"] and "https" in status["search_problem"]
    monkeypatch.delenv("VIOLET_WEB_SEARXNG_URL")
    assert web_status(violetdb)["search_problem"] == "VIOLET_WEB_SEARXNG_URL 没有设置"


# ------------------------------------------------------------------------------------------- deployment files

def read(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def test_the_deployment_files_exist_and_say_the_right_things():
    compose, settings, env, script = read("compose.yaml"), read("config/settings.yml"), read("env_example"), read("searxng.sh")
    ports = re.findall(r'^\s*-\s*"([^"]+)"\s*$', compose.split("ports:", 1)[1].split("networks:", 1)[0], re.M)
    assert ports == ["127.0.0.1:${SEARXNG_HOST_PORT:-18089}:8080"]
    assert "image: ${SEARXNG_IMAGE:-docker.io/searxng/searxng:latest}" in compose
    # (0.0.0.0 inside the container is GRANIAN_HOST; what decides exposure is the published port above)
    assert not re.search(r"^\s*(network_mode|privileged|cap_add)\s*:", compose, re.M)
    assert '    user: "977:977"' in compose and 'FORCE_OWNERSHIP: "false"' in compose
    assert "SEARXNG_SECRET: ${SEARXNG_SECRET:?" in compose
    assert re.search(r"^\s*formats:\s*\n\s*- html\s*\n\s*- json\s*$", settings, re.M)
    assert re.search(r"^SEARXNG_SECRET=$", env, re.M) and "SEARXNG_HOST_PORT=18089" in env
    for text in (compose, settings, env, script):
        assert "\r" not in text                                                # LF (the script runs on Linux)
    ignored = read(".gitignore").split()
    assert {"/.env", "/cache/", "/state/", "/config/*", "!/config/settings.yml"} <= set(ignored)


def test_only_up_and_upgrade_pull_or_start_and_nothing_installs_docker():
    script = read("searxng.sh")
    functions = dict(re.findall(r"^(cmd_\w+)\(\) \{\n(.*?)^\}", script, re.M | re.S))
    starters = {name for name, body in functions.items() if re.search(r"compose (pull|up)", body)}
    assert starters == {"cmd_up", "cmd_upgrade"}
    for name in starters:
        body = functions[name]
        assert body.index("confirm ") < body.index("compose pull")              # asks before pulling
    assert not re.search(r"apt(-get)?\s+install|yum\s+install|dnf\s+install|get\.docker\.com|\|\s*(ba)?sh\b|snap\s+install",
                         script)
    assert "source .env" not in script and ". ./.env" not in script            # .env is read as data


def test_eve_itself_never_runs_docker_and_git_has_no_hooks_to_do_it():
    code = [PROJECT / "main.py", PROJECT / "violet_runtime.py", PROJECT / "start.sh"]
    # the one exception (decisions V24, the user chose Docker for QQ): the root installer of SnowLuma; the running
    # Violet (ai, wx_ilink_py) still never starts Docker
    code += [p for d in ("ai", "wx_ilink_py", "tools") for p in (PROJECT / d).rglob("*.py")
             if p.name != "violet_snowluma.py"]
    run = re.compile(r"docker(-compose|\s+(compose|run|pull|start|exec|create|image|container))|searxng\.sh"
                     r"|[\"']docker[\"']|compose\s+up", re.I)          # running it, not mentioning it in a comment
    hits = [str(p) for p in code if run.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert hits == []
    tracked = subprocess.run(["git", "ls-files"], cwd=PROJECT, capture_output=True, text=True, encoding="utf-8")
    if tracked.returncode == 0:
        assert not [f for f in tracked.stdout.splitlines() if "hooks/" in f or f.endswith((".githooks", "husky"))]


def test_compose_and_settings_validate_structurally():
    import yaml                                              # requirements-dev.txt; missing = fail, never skip
    import importlib.util
    spec = importlib.util.spec_from_file_location("validate_config", DEPLOY / "validate_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.validate(DEPLOY) == []
    assert yaml.safe_load(read("compose.yaml"))["services"]["searxng"]["container_name"] == "violet-searxng"


@pytest.mark.parametrize("old,new,needle", [
    ('"127.0.0.1:${SEARXNG_HOST_PORT:-18089}:8080"', '"${SEARXNG_HOST_PORT:-18089}:8080"', "not bound to 127.0.0.1"),
    ("    - json\n", "", "search.formats must include json"),
    ('secret_key: "set-by-SEARXNG_SECRET-environment-variable"', 'secret_key: "a-real-secret-in-git"', "placeholder"),
    ("    driver: bridge", "    driver: bridge\n    internal: true", "cannot be internal"),
    ("image: ${SEARXNG_IMAGE:-docker.io/searxng/searxng:latest}", "image: someone/searxng:latest", "official"),
    ("SEARXNG_SECRET=\n", "SEARXNG_SECRET=abc\n", "present and empty"),
    # closeout: matched against the official image (no USER, FORCE_OWNERSHIP chown, no wget, GRANIAN_HOST "::")
    ('    user: "977:977"\n', "", "user must be 977:977"),
    ("    cap_drop: [ALL]\n", "    cap_drop: [ALL]\n    cap_add: [CHOWN]\n", "no capabilities are needed"),
    ('      FORCE_OWNERSHIP: "false"', '      FORCE_OWNERSHIP: "true"', "FORCE_OWNERSHIP must be false"),
    ("./config:/etc/searxng:ro", "./config:/etc/searxng:rw", "/etc/searxng must be mounted ro"),
    ('      GRANIAN_HOST: "0.0.0.0"', '      GRANIAN_HOST: "::"', "GRANIAN_HOST 0.0.0.0"),
    ('["CMD", "/usr/local/searxng/.venv/bin/python",', '["CMD", "wget",', "image's own Python"),
])
def test_the_validator_catches_unsafe_changes(tmp_path, old, new, needle):
    import importlib.util
    copy = tmp_path / "searxng"
    shutil.copytree(DEPLOY, copy, ignore=shutil.ignore_patterns(".env", "cache", "state"))
    for name in ("compose.yaml", "config/settings.yml", "env_example"):
        path = copy / name
        text = path.read_text(encoding="utf-8")
        if old in text:
            path.write_text(text.replace(old, new), encoding="utf-8")
            break
    else:
        raise AssertionError(f"pattern not found: {old}")
    spec = importlib.util.spec_from_file_location("validate_config", copy / "validate_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert any(needle in p for p in mod.validate(copy))


def _bash() -> str | None:
    found = os.environ.get("VIOLET_TEST_BASH") or shutil.which("bash")
    if not found:
        for candidate in (r"D:\Development\PortableGit\bin\bash.exe", r"C:\Program Files\Git\bin\bash.exe"):
            if Path(candidate).is_file():
                return candidate
    return found


def test_the_script_parses_and_refuses_without_docker(tmp_path):
    bash = _bash()
    if not bash:
        pytest.skip("no bash on this machine")
    assert subprocess.run([bash, "-n", str(DEPLOY / "searxng.sh")], capture_output=True).returncode == 0
    copy = tmp_path / "searxng"
    shutil.copytree(DEPLOY, copy, ignore=shutil.ignore_patterns(".env", "cache", "state"))
    env = {k: v for k, v in os.environ.items()}
    env["PATH"] = str(Path(bash).parent) + os.pathsep + str(Path(bash).parent.parent / "usr" / "bin")   # no docker
    run = lambda *a: subprocess.run([bash, "./searxng.sh", *a], cwd=copy, env=env, capture_output=True, text=True,  # noqa: E731
                                    encoding="utf-8", input="")
    first = run("init")
    assert first.returncode == 0, first.stderr
    secret = re.search(r"^SEARXNG_SECRET=([0-9a-f]+)$", (copy / ".env").read_text(encoding="utf-8"), re.M)
    assert secret and len(secret.group(1)) == 64
    run("init")
    assert secret.group(1) in (copy / ".env").read_text(encoding="utf-8")       # never overwritten
    for cmd in ("check", "up", "upgrade", "down"):
        done = run(cmd)
        assert done.returncode != 0 and "Docker" in done.stderr, (cmd, done.stdout, done.stderr)
    assert run("bogus").returncode != 0
