"""Phase 9.0 -- Violet Control Center: authentication, sessions, CSRF, settings, memories, chat history.

Fictional data only, an isolated temp data dir per test, no network, no real model.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
BEARER = {"Authorization": f"Bearer {TOKEN}"}
SAME = {"Origin": "http://localhost"}
JSON = {"Accept": "application/json"}
SECRET_CHAT = "这是一句只在测试里出现的悄悄话-7Q4"
PASSWORD = "correct horse battery 42"


class FakeAgent:
    def __init__(self):
        self.reloads = 0

    def reload(self):
        self.reloads += 1


@pytest.fixture()
def env_guard(tmp_path, monkeypatch):
    """Each test gets its own data dir; os.environ and the override bookkeeping are restored afterwards."""
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
def made(env_guard, violetdb):
    from wx_ilink_py import ILinkClient, create_app
    agent = FakeAgent()
    app = create_app(client=ILinkClient(data_dir=env_guard, auto_login=False), violetdb=violetdb, proactive_scheduler=None,
                     all_agents=[agent], admin_token=TOKEN)
    app.testing = True
    return app, agent


@pytest.fixture()
def app(made):
    return made[0]


@pytest.fixture()
def api(app):
    return app.test_client()


@pytest.fixture()
def admin(app):
    c = app.test_client()
    assert c.post("/auth/login", data={"token": TOKEN}, headers=SAME).status_code == 302
    return c


def _placeholder(rule) -> str:
    path = rule.rule
    for name, conv in rule._converters.items():
        value = "1" if conv.__class__.__name__ in ("IntegerConverter", "FloatConverter") else "x"
        path = re.sub(r"<(?:[^:<>]+:)?" + re.escape(name) + r">", value, path)
    return path


# ---------------------------------------------------------------- authentication on every route

def test_every_route_rejects_anonymous_requests_without_side_effects(app, api, violetdb):
    violetdb.add_user_message("text", SECRET_CHAT)
    mem = violetdb.add_event_memory("测试记忆：周五要交实验报告", category="study")
    checked = 0
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static" or rule.rule in ("/auth/login", "/healthz"):
            continue
        path = _placeholder(rule)
        for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
            resp = api.open(path, method=method, json={"confirm": True, "changes": {"DEBOUNCE_NORMAL": 1}},
                            headers=JSON)
            assert resp.status_code == 401, (method, rule.rule, resp.status_code)
            assert SECRET_CHAT not in resp.get_data(as_text=True)
            checked += 1
    assert checked > 60                                     # the whole dashboard, not a sample
    assert violetdb.get_event_memory(mem)["status"] == "active"
    assert not (Path(os.environ["VIOLET_DATA_DIR"]) / "control" / "settings.json").exists()


def test_control_routes_are_registered_and_require_auth(app, api):
    paths = {r.rule for r in app.url_map.iter_rules()}
    for p in ("/control", "/api/control/overview", "/api/control/messages", "/api/control/messages/export",
              "/api/control/memories", "/api/control/settings", "/api/control/settings/history",
              "/api/control/settings/rollback", "/auth/password", "/auth/logout-all", "/auth/status"):
        assert p in paths
    page = api.get("/control", headers={"Accept": "text/html"})
    assert page.status_code == 302 and page.headers["Location"].startswith("/auth/login")   # browser -> login page
    assert api.get("/control", headers=JSON).status_code == 401


def test_bearer_header_is_checked_exactly(api):
    assert api.get("/api/control/overview", headers={"Authorization": "Bearer " + TOKEN[:-1]}).status_code == 401
    assert api.get("/api/control/overview", headers={"Authorization": "Basic " + TOKEN}).status_code == 401
    assert api.get("/api/control/overview", headers=BEARER).status_code == 200


# ---------------------------------------------------------------- password, sessions, rate limit

def test_no_default_password_and_password_is_stored_only_as_a_hash(app, env_guard):
    store = app.extensions["violet_admin_store"]
    assert not store.has_password()
    store.set_password(PASSWORD)
    raw = (env_guard / "control" / "admin.json").read_text(encoding="utf-8")
    assert PASSWORD not in raw and json.loads(raw)["password_hash"].startswith("scrypt:")
    assert store.verify_password(PASSWORD) and not store.verify_password(PASSWORD + "x")
    for weak in ("", "short", "aaaaaaaaaaaaaaaa"):
        with pytest.raises(ValueError):
            store.set_password(weak)


def test_password_login_and_token_login_both_work(app, api):
    app.extensions["violet_admin_store"].set_password(PASSWORD)
    assert api.post("/auth/login", data={"password": "wrong password!!"}, headers=SAME).status_code == 401
    assert api.get("/api/control/overview", headers=JSON).status_code == 401
    assert api.post("/auth/login", data={"password": PASSWORD}, headers=SAME).status_code == 302
    assert api.get("/api/control/overview", headers=JSON).status_code == 200
    other = app.test_client()
    assert other.post("/auth/login", data={"password": TOKEN}, headers=SAME).status_code == 302


def test_session_secret_is_random_per_install_not_derived_from_the_token(app, env_guard):
    import hashlib
    assert app.secret_key != hashlib.sha256(("violet-dashboard-session:" + TOKEN).encode()).digest()
    stored = (env_guard / "control" / "session_secret").read_text(encoding="utf-8").strip()
    assert bytes.fromhex(stored) == app.secret_key and len(stored) == 64


def test_login_is_locked_after_repeated_failures_even_for_the_right_token(app):
    c = app.test_client()
    for _ in range(5):
        assert c.post("/auth/login", data={"token": "nope-nope-nope"}, headers=SAME).status_code == 401
    locked = c.post("/auth/login", data={"token": TOKEN}, headers=SAME)
    assert locked.status_code == 429
    assert "violet_admin_session" not in locked.headers.get("Set-Cookie", "")
    other_ip = app.test_client()
    resp = other_ip.post("/auth/login", data={"token": TOKEN}, headers=SAME, environ_base={"REMOTE_ADDR": "10.0.0.9"})
    assert resp.status_code == 302                               # the lock is per client


def test_a_successful_login_clears_earlier_failures(app):
    c = app.test_client()
    for _round in range(2):                                     # 4 + 4 failures, a success in between
        for _ in range(4):
            assert c.post("/auth/login", data={"token": "nope-nope-nope"}, headers=SAME).status_code == 401
        assert c.post("/auth/login", data={"token": TOKEN}, headers=SAME).status_code == 302


def test_login_rejects_a_cross_origin_form(api):
    assert api.post("/auth/login", data={"token": TOKEN}, headers={"Origin": "http://evil.example"}).status_code == 403


def test_logout_all_ends_every_session(app):
    a, b = app.test_client(), app.test_client()
    for c in (a, b):
        c.post("/auth/login", data={"token": TOKEN}, headers=SAME)
    assert b.get("/api/control/overview").status_code == 200
    assert a.post("/auth/logout-all", headers=SAME).status_code == 200
    assert a.get("/api/control/overview", headers=JSON).status_code == 401
    assert b.get("/api/control/overview", headers=JSON).status_code == 401
    assert b.get("/api/control/overview", headers=BEARER).status_code == 200     # scripts keep working


def test_password_change_needs_the_current_one_and_ends_other_sessions(app):
    a, b = app.test_client(), app.test_client()
    for c in (a, b):
        c.post("/auth/login", data={"token": TOKEN}, headers=SAME)
    bad = a.post("/auth/password", json={"current": "guess-guess-guess", "new": PASSWORD, "confirm": PASSWORD},
                 headers=SAME)
    assert bad.status_code == 403
    mismatch = a.post("/auth/password", json={"current": TOKEN, "new": PASSWORD, "confirm": PASSWORD + "!"}, headers=SAME)
    assert mismatch.status_code == 400
    weak = a.post("/auth/password", json={"current": TOKEN, "new": "short", "confirm": "short"}, headers=SAME)
    assert weak.status_code == 400
    assert a.post("/auth/password", json={"current": TOKEN, "new": PASSWORD, "confirm": PASSWORD},
                  headers=SAME).status_code == 200
    assert a.get("/api/control/overview").status_code == 200                       # this session continues
    assert b.get("/api/control/overview", headers=JSON).status_code == 401          # the other one ended
    assert a.get("/auth/status").get_json()["password_set"] is True


def test_idle_and_absolute_session_timeouts(app, monkeypatch):
    import wx_ilink_py.auth as auth
    c = app.test_client()
    c.post("/auth/login", data={"token": TOKEN}, headers=SAME)
    real = auth.time.time
    monkeypatch.setattr(auth.time, "time", lambda: real() + 119 * 60)
    assert c.get("/api/control/overview").status_code == 200          # refreshes the idle clock
    monkeypatch.setattr(auth.time, "time", lambda: real() + 119 * 60 + 121 * 60)
    assert c.get("/api/control/overview", headers=JSON).status_code == 401
    d = app.test_client()
    monkeypatch.setattr(auth.time, "time", real)
    d.post("/auth/login", data={"token": TOKEN}, headers=SAME)
    for hours in range(1, 13):
        monkeypatch.setattr(auth.time, "time", lambda h=hours: real() + h * 3600 - 60)
        d.get("/api/control/overview")
    monkeypatch.setattr(auth.time, "time", lambda: real() + 12 * 3600 + 60)
    assert d.get("/api/control/overview", headers=JSON).status_code == 401       # 12 h absolute limit


def test_state_changes_with_a_session_cookie_need_the_same_origin(admin, violetdb):
    mem = violetdb.add_event_memory("测试记忆：下周去看牙医", category="health")
    url = f"/api/control/memories/event/{mem}/delete"
    assert admin.post(url, json={"confirm": True}).status_code == 403
    assert admin.post(url, json={"confirm": True}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert admin.put("/api/control/settings", json={"changes": {"DEBOUNCE_NORMAL": 3}}).status_code == 403
    assert violetdb.get_event_memory(mem)["status"] == "active"
    assert admin.post(url, json={"confirm": True}, headers=SAME).status_code == 200


def test_security_headers_and_no_store(admin):
    page = admin.get("/control")
    assert page.status_code == 200
    for name in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
        assert name in page.headers
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert page.headers["Cache-Control"] == "no-store"
    assert admin.get("/api/control/overview").headers["Cache-Control"] == "no-store"
    assert admin.get("/static/control.js").headers.get("Cache-Control") != "no-store"


def test_cookie_flags(app):
    c = app.test_client()
    cookie = c.post("/auth/login", data={"token": TOKEN}, headers=SAME).headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" not in cookie
    app.config["SESSION_COOKIE_SECURE"] = True
    cookie = app.test_client().post("/auth/login", data={"token": TOKEN}, headers=SAME).headers["Set-Cookie"]
    assert "Secure" in cookie


# ---------------------------------------------------------------- bind policy

# Phase 9.0.1 changed this deliberately: no combination of switches opens a non-loopback address any more (9.0 allowed
# 0.0.0.0 with VIOLET_DASHBOARD_PUBLIC=1 + COOKIE_SECURE=1, which a flag cannot prove to be behind HTTPS).
@pytest.mark.parametrize("host,secure,expected", [
    ("0.0.0.0", "0", "127.0.0.1"), ("0.0.0.0", "1", "127.0.0.1"), ("192.168.1.20", "1", "127.0.0.1"),
    ("::", "1", "127.0.0.1"), ("localhost", "0", "localhost"), ("::1", "0", "::1"), ("127.0.0.1", "1", "127.0.0.1"),
])
def test_the_dashboard_only_listens_on_loopback(monkeypatch, host, secure, expected):
    import main
    monkeypatch.setenv("VIOLET_DASHBOARD_HOST", host)
    monkeypatch.setenv("VIOLET_DASHBOARD_PUBLIC", "1")                 # the 9.0 switch no longer exists
    monkeypatch.setenv("VIOLET_DASHBOARD_COOKIE_SECURE", secure)
    assert main._dashboard_bind() == (expected, 7100)


def test_proxy_headers_are_trusted_only_when_asked(monkeypatch):
    # 9.0.1: with the switch on, a wrapper that trusts forwarded headers only from this machine (see p901 tests)
    import main
    monkeypatch.delenv("VIOLET_DASHBOARD_TRUST_PROXY", raising=False)
    assert main._served_app() is main.app
    monkeypatch.setenv("VIOLET_DASHBOARD_TRUST_PROXY", "1")
    assert main._served_app() is not main.app


# ---------------------------------------------------------------- settings

def _settings(client):
    return {s["key"]: s for s in client.get("/api/control/settings").get_json()["data"]["settings"]}


def test_settings_list_only_whitelisted_keys_and_never_the_api_key(admin):
    body = admin.get("/api/control/settings").get_data(as_text=True)
    assert "sk-test-not-a-real-key" not in body
    rows = _settings(admin)
    assert rows["LLM_API_KEY"]["value"] is None and rows["LLM_API_KEY"]["is_set"] is True
    assert rows["LLM_API_KEY"]["fingerprint"].startswith("sha256:")
    for hidden in ("VIOLET_ADMIN_TOKEN", "VIOLET_DB_PATH", "MCP_SERVERS", "VIOLET_DASHBOARD_HOST", "DEBUG_MODE"):
        assert hidden not in rows
    assert rows["CHAT_LLM_MODEL"]["value"] == "fake-chat"
    # 9.0.1: every whitelisted setting now takes effect at once (main's reload hook starts / stops the life thread
    # and re-reads VIOLET_SIMULATE_TYPING); 9.0 labelled those two "restart". Violet stage 3 added settings that are
    # read once at start (timezone, sessions, logging, send retries, ...): exactly those say "restart"
    restart = {k for k, r in rows.items() if r["effect"] == "restart"}
    assert restart == {"VIOLET_LIFE_LLM_MODEL", "VIOLET_ASYNC_MEMORY", "VIOLET_SEMANTIC_GUARD_MODEL",
                       "VIOLET_SEMANTIC_GUARD_MAX_CALLS_PER_DAY", "VIOLET_SEMANTIC_GUARD_MAX_TOKENS_PER_DAY",
                       "VIOLET_SEND_MAX_ATTEMPTS", "VIOLET_SEND_RETRY_DELAY_SECONDS", "VIOLET_STARTUP_MESSAGE",
                       "VIOLET_TIMEZONE", "VIOLET_LOG_LEVEL", "VIOLET_DASHBOARD_IDLE_MINUTES",
                       "VIOLET_DASHBOARD_SESSION_HOURS"}


def test_no_setting_caps_the_number_of_messages_in_a_reply():
    from ai.control_config import SETTINGS
    for s in SETTINGS:
        assert not re.search(r"MAX_(MESSAGES|SEGMENTS|LINES)|SEGMENT_LIMIT", s.key)


def test_an_invalid_value_saves_nothing(admin, made, env_guard):
    agent = made[1]
    resp = admin.put("/api/control/settings", headers=SAME, json={"changes": {
        "DEBOUNCE_NORMAL": 6, "CHAT_LLM_MODEL": "bad model name", "LLM_API_URL": "ftp://x"}})
    assert resp.status_code == 400
    problems = resp.get_json()["problems"]
    assert set(problems) == {"CHAT_LLM_MODEL", "LLM_API_URL"}
    assert not (env_guard / "control" / "settings.json").exists()
    assert os.environ.get("DEBOUNCE_NORMAL") != "6" and agent.reloads == 0
    for bad in ({"VIOLET_ADMIN_TOKEN": "x" * 20}, {"DEBOUNCE_NORMAL": 9999}, {"proactive.quiet_start": "25:00"},
                {"LLM_MAX_RETRY_ATTEMPTS": 2.5}, {"prompt.chat": "v99"}, {"VIOLET_LIFE_ENGINE": "maybe"}):
        assert admin.put("/api/control/settings", headers=SAME, json={"changes": bad}).status_code == 400, bad
    assert admin.put("/api/control/settings", headers=SAME, json={"changes": {
        "VIOLET_TYPING_MIN_DELAY_SECONDS": 30, "VIOLET_TYPING_MAX_DELAY_SECONDS": 5}}).status_code == 400


def test_save_applies_now_reloads_agents_and_reports_restart_items(admin, made, env_guard):
    agent = made[1]
    resp = admin.put("/api/control/settings", headers=SAME, json={"reason": "测试", "changes": {
        "DEBOUNCE_NORMAL": 4, "CHAT_LLM_MODEL": "fake-chat-2", "VIOLET_LIFE_ENGINE": False}})
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["needs_restart"] == []                                   # 9.0.1: VIOLET_LIFE_ENGINE is immediate now
    assert set(data["applied_now"]) == {"DEBOUNCE_NORMAL", "CHAT_LLM_MODEL", "VIOLET_LIFE_ENGINE"}
    assert os.environ["CHAT_LLM_MODEL"] == "fake-chat-2" and os.environ["DEBOUNCE_NORMAL"] == "4"
    assert agent.reloads == 1
    rows = _settings(admin)
    assert rows["CHAT_LLM_MODEL"]["value"] == "fake-chat-2" and rows["CHAT_LLM_MODEL"]["source"] == "dashboard"
    history = admin.get("/api/control/settings/history").get_json()["data"]
    assert history[0]["reason"] == "测试" and history[0]["actor"] == "admin:session"


def test_overrides_survive_an_env_reload_and_a_restart(env_guard):
    from ai import control_config as cc
    from ai.settings import ENV_PATH
    Path(ENV_PATH).write_text("DEBOUNCE_NORMAL=7\nCHAT_LLM_MODEL=env-chat\n", encoding="utf-8")
    try:
        store = cc.ControlStore(env_guard)
        store.save({"DEBOUNCE_NORMAL": 2.5}, "test")
        cc.reload_env()                                               # what every agent reload does
        assert os.environ["DEBOUNCE_NORMAL"] == "2.5" and os.environ["CHAT_LLM_MODEL"] == "env-chat"
        fresh_environ = {"DEBOUNCE_NORMAL": "7"}                       # a new process: .env read, then overrides
        cc.ControlStore(env_guard).apply_to_environ(fresh_environ)
        assert fresh_environ["DEBOUNCE_NORMAL"] == "2.5"
        store.save({"DEBOUNCE_NORMAL": None}, "test")                  # back to .env
        cc.reload_env()
        assert os.environ["DEBOUNCE_NORMAL"] == "7"
    finally:
        Path(ENV_PATH).unlink()


def test_a_real_agent_reload_keeps_the_dashboard_model(env_guard):
    """BaseAgent._load_config reads .env with override=True; the dashboard value must still win afterwards."""
    from ai import control_config as cc
    from ai.agents import ChatAgent
    from ai.settings import ENV_PATH
    Path(ENV_PATH).write_text("CHAT_LLM_MODEL=env-chat\nCHAT_LLM_MODEL_TEMPERATURE=0.3\n", encoding="utf-8")
    try:
        cc.ControlStore(env_guard).save({"CHAT_LLM_MODEL": "dashboard-chat", "CHAT_LLM_MODEL_TEMPERATURE": 0.9}, "test")
        agent = ChatAgent()
        assert agent._config.reply_model == "dashboard-chat" and agent._config.reply_temperature == 0.9
        agent.reload()
        assert agent._config.reply_model == "dashboard-chat"
        cc.ControlStore(env_guard).save({"CHAT_LLM_MODEL": None}, "test")
        agent.reload()
        assert agent._config.reply_model == "env-chat"
    finally:
        Path(ENV_PATH).unlink()


def test_removing_an_override_without_env_value_restores_the_original(env_guard, monkeypatch):
    from ai import control_config as cc
    monkeypatch.setenv("DEBOUNCE_EXTRA", "11")
    store = cc.ControlStore(env_guard)
    store.save({"DEBOUNCE_EXTRA": 20}, "test", apply=cc.apply_overrides)
    assert os.environ["DEBOUNCE_EXTRA"] == "20"
    store.save({"DEBOUNCE_EXTRA": None}, "test", apply=cc.apply_overrides)
    assert os.environ["DEBOUNCE_EXTRA"] == "11"


def test_api_key_is_replaced_but_never_shown_and_can_be_restored(admin, env_guard):
    new_key = "sk-test-replacement-key-000"
    resp = admin.put("/api/control/settings", headers=SAME, json={"changes": {"LLM_API_KEY": new_key}})
    assert resp.status_code == 200 and new_key not in resp.get_data(as_text=True)
    assert os.environ["LLM_API_KEY"] == new_key
    history_text = (env_guard / "control" / "history.jsonl").read_text(encoding="utf-8")
    assert new_key not in history_text and "sk-test-not-a-real-key" not in history_text
    assert new_key not in admin.get("/api/control/settings").get_data(as_text=True)
    assert new_key not in admin.get("/api/control/settings/history").get_data(as_text=True)
    assert new_key not in (env_guard / "control" / "settings.json").read_text(encoding="utf-8")
    newer = "sk-test-replacement-key-111"
    admin.put("/api/control/settings", headers=SAME, json={"changes": {"LLM_API_KEY": newer}})
    assert admin.post("/api/control/settings/restore-secret", headers=SAME,
                      json={"key": "LLM_API_KEY"}).status_code == 400                    # needs confirmation
    assert admin.post("/api/control/settings/restore-secret", headers=SAME,
                      json={"key": "LLM_API_KEY", "confirm": True}).status_code == 200
    assert os.environ["LLM_API_KEY"] == new_key


def test_rollback_restores_a_snapshot_and_leaves_the_api_key(admin):
    admin.put("/api/control/settings", headers=SAME, json={"changes": {"DEBOUNCE_NORMAL": 3, "LLM_API_KEY": "sk-test-k1-aaaa"}})
    admin.put("/api/control/settings", headers=SAME, json={"changes": {"DEBOUNCE_NORMAL": 9, "DEBOUNCE_EXTRA": 30,
                                                                          "LLM_API_KEY": "sk-test-k2-bbbb"}})
    assert admin.post("/api/control/settings/rollback", headers=SAME, json={"revision": 1}).status_code == 400
    resp = admin.post("/api/control/settings/rollback", headers=SAME, json={"revision": 1, "confirm": True})
    assert resp.status_code == 200
    rows = _settings(admin)
    assert rows["DEBOUNCE_NORMAL"]["value"] == 3 and rows["DEBOUNCE_EXTRA"]["source"] != "dashboard"
    assert os.environ["LLM_API_KEY"] == "sk-test-k2-bbbb"
    changes = {c["key"]: c for c in resp.get_json()["data"]["changes"]}
    assert changes["DEBOUNCE_NORMAL"]["old"] == 9 and changes["DEBOUNCE_EXTRA"]["new"] is None
    assert admin.post("/api/control/settings/rollback", headers=SAME, json={"revision": 99, "confirm": True}).status_code == 400


def test_a_failed_apply_puts_the_previous_settings_back(env_guard):
    from ai import control_config as cc
    store = cc.ControlStore(env_guard)
    store.save({"DEBOUNCE_NORMAL": 3}, "test")

    def boom():
        raise RuntimeError("reload failed")
    with pytest.raises(RuntimeError):
        store.save({"DEBOUNCE_NORMAL": 8, "LLM_API_KEY": "sk-test-never-kept"}, "test", apply=boom)
    assert store.values() == {"DEBOUNCE_NORMAL": 3}
    assert "LLM_API_KEY" not in store.secret_values()
    assert len(store.history()) == 1


def test_prompt_version_setting_switches_the_prompt_text_and_back(env_guard):
    from ai import control_config as cc
    versions = cc.prompt_versions("chat")
    assert {"v1", "v2"} <= set(versions)
    os.environ["CHAT_LLM_PROMPT"] = "env prompt"
    store = cc.ControlStore(env_guard)
    store.save({"prompt.chat": "v2"}, "test", apply=cc.apply_overrides)
    assert os.environ["CHAT_LLM_PROMPT"] == (cc.PROMPT_DIRS["chat"] / "chat.v2.txt").read_text(encoding="utf-8")
    store.save({"prompt.chat": None}, "test", apply=cc.apply_overrides)
    assert os.environ["CHAT_LLM_PROMPT"] == "env prompt"


def test_proactive_settings_reach_the_scheduler_config(env_guard, tmp_path):
    from ai import control_config as cc
    from ai.proactive_scheduler import ProactiveScheduler
    store = cc.ControlStore(env_guard)
    store.save({"proactive.daily_limit": 7, "proactive.quiet_start": "23:30", "proactive.enabled": False}, "test")
    sched = ProactiveScheduler(None, None, None, lambda *_: None, config_path=tmp_path / "missing.json")
    config = sched.load_config()
    assert config["daily_message_limit"] == 7 and config["enabled"] is False
    assert config["quiet_hours"]["start"] == "23:30" and config["quiet_hours"]["end"]          # end kept from file


# ---------------------------------------------------------------- memories

def test_event_memory_correct_and_delete_keep_history(admin, violetdb):
    mem = violetdb.add_event_memory("测试记忆：周三下午有篮球赛", category="activity")
    resp = admin.post(f"/api/control/memories/event/{mem}/correct", headers=SAME,
                      json={"content": "测试记忆：周四下午有篮球赛", "reason": "日期记错了"})
    assert resp.status_code == 200 and resp.get_json()["data"]["content"] == "测试记忆：周四下午有篮球赛"
    revs = violetdb.list_memory_revisions("event_memories", mem)
    assert revs[-1]["old_content"] == "测试记忆：周三下午有篮球赛" and revs[-1]["source"] == "dashboard"
    assert admin.post(f"/api/control/memories/event/{mem}/delete", headers=SAME, json={}).status_code == 400
    assert admin.post(f"/api/control/memories/event/{mem}/delete", headers=SAME, json={"confirm": True}).status_code == 200
    row = violetdb.get_event_memory(mem)
    assert row is not None and row["status"] == "deleted"                       # soft delete, row kept
    last = violetdb.list_memory_revisions("event_memories", mem)[-1]
    assert last["action"] == "retract" and last["source"] == "dashboard"
    assert admin.post(f"/api/control/memories/event/{mem}/delete", headers=SAME, json={"confirm": True}).status_code == 409
    assert admin.post(f"/api/control/memories/event/{mem}/correct", headers=SAME,
                      json={"content": "再改"}).status_code == 409
    history = admin.get(f"/api/control/memories/event/{mem}/history").get_json()["data"]
    assert [r["action"] for r in history["revisions"]][-2:] == ["correct", "retract"]


def test_core_memory_correct_and_delete_keep_the_old_text(admin, violetdb):
    mem = violetdb.add_core_memory("test_nickname", "nickname", "测试：喜欢被叫小林")
    assert admin.post(f"/api/control/memories/core/{mem}/correct", headers=SAME,
                      json={"content": "测试：喜欢被叫阿林"}).status_code == 200
    assert violetdb.get_core_memory(mem)["content"] == "测试：喜欢被叫阿林"
    resp = admin.post(f"/api/control/memories/core/{mem}/delete", headers=SAME, json={"confirm": True, "reason": "不准"})
    assert resp.status_code == 200 and violetdb.get_core_memory(mem) is None
    revs = violetdb.list_memory_revisions("core_memories", mem)
    assert [(r["action"], r["old_content"]) for r in revs] == [("correct", "测试：喜欢被叫小林"), ("delete", "测试：喜欢被叫阿林")]
    assert admin.post(f"/api/control/memories/core/{mem}/delete", headers=SAME, json={"confirm": True}).status_code == 404


def test_memory_inputs_are_validated(admin, violetdb):
    mem = violetdb.add_event_memory("测试记忆：要买猫粮", category="life")
    for body in ({"content": ""}, {"content": "x" * 1001}, {"content": "测试记忆：要买猫粮"}):
        assert admin.post(f"/api/control/memories/event/{mem}/correct", headers=SAME, json=body).status_code == 400
    assert admin.post("/api/control/memories/other/1/correct", headers=SAME, json={"content": "x"}).status_code == 404
    assert admin.post("/api/control/memories/event/999999/correct", headers=SAME, json={"content": "x"}).status_code == 404
    assert admin.get("/api/control/memories?kind=nope").status_code == 400


def test_memory_search_filters_and_pages(admin, violetdb):
    for i in range(5):
        violetdb.add_event_memory(f"测试记忆：第{i}次去图书馆", category="study")
    violetdb.add_event_memory("测试记忆：100%完成作业", category="study")
    data = admin.get("/api/control/memories?kind=event&q=图书馆&limit=2&offset=2").get_json()["data"]
    assert data["total"] == 5 and len(data["memories"]) == 2
    assert all("revisions" in m for m in data["memories"])
    assert admin.get("/api/control/memories?kind=event&q=%25").get_json()["data"]["total"] == 1   # % is literal


# ---------------------------------------------------------------- chat history

def _outbox(violetdb, message_id, content, origin, event_id=None):
    now = violetdb.now()
    violetdb.execute("INSERT INTO outbound_messages (client_id, origin, proactive_event_id, message_type, content, status, "
                  "message_id, created_at, updated_at) VALUES (?, ?, ?, 'text', ?, 'sent', ?, ?, ?)",
                  (f"test-{message_id}", origin, event_id, content, message_id, now, now))


def test_chat_history_marks_who_sent_what_and_is_read_only(admin, app, violetdb):
    u = violetdb.add_user_message("text", "测试：今天好累")
    r = violetdb.add_assistant_message("text", "测试：那早点休息")
    p = violetdb.add_assistant_message("text", "测试：晚饭吃了吗")
    now = violetdb.now()
    violetdb.execute("INSERT INTO proactive_events (event_type, status, created_at, updated_at) VALUES "
                  "('companion_check', 'sent', ?, ?)", (now, now))
    event_id = violetdb.fetch_one("SELECT MAX(id) AS id FROM proactive_events")["id"]
    _outbox(violetdb, r, "测试：那早点休息", "reply")
    _outbox(violetdb, p, "测试：晚饭吃了吗", "proactive", event_id)
    rows = admin.get("/api/control/messages").get_json()["data"]["messages"]
    kinds = {m["id"]: m["kind"] for m in rows}
    assert kinds == {u: "user", r: "violet", p: "proactive"}
    assert [m["id"] for m in rows] == [u, r, p]                                   # oldest first
    only_user = admin.get("/api/control/messages?role=user").get_json()["data"]["messages"]
    assert [m["id"] for m in only_user] == [u]
    for rule in app.url_map.iter_rules():                                     # no route edits or deletes chat
        if rule.rule.startswith("/api/control/messages"):
            assert (rule.methods or set()) <= {"GET", "HEAD", "OPTIONS"}


def test_chat_history_search_date_and_paging(admin, violetdb):
    ids = [violetdb.add_user_message("text", f"测试：第{i}条", created_at=f"2026-10-0{1 + i % 2}T10:0{i}:00+08:00")
           for i in range(6)]
    day = admin.get("/api/control/messages?date=2026-10-02").get_json()["data"]["messages"]
    assert [m["id"] for m in day] == [ids[1], ids[3], ids[5]]
    page = admin.get("/api/control/messages?limit=4").get_json()["data"]
    assert len(page["messages"]) == 4 and page["next_before_id"] == ids[2]
    older = admin.get(f"/api/control/messages?limit=4&before_id={page['next_before_id']}").get_json()["data"]
    assert [m["id"] for m in older["messages"]] == ids[:2] and older["next_before_id"] is None
    assert [m["id"] for m in admin.get("/api/control/messages?q=第3").get_json()["data"]["messages"]] == [ids[3]]
    assert admin.get("/api/control/messages?q=_").get_json()["data"]["messages"] == []    # _ is literal
    assert admin.get("/api/control/messages?date=2026/10/02").status_code == 400
    days = admin.get("/api/control/messages/days").get_json()["data"]
    assert {d["day"]: d["n"] for d in days} == {"2026-10-01": 3, "2026-10-02": 3}


def test_chat_export_is_a_download_with_a_valid_range(admin, violetdb):
    violetdb.add_user_message("text", "测试：导出这一句", created_at="2026-10-05T09:00:00+08:00")
    assert admin.get("/api/control/messages/export?from=2026-10-06&to=2026-10-05").status_code == 400
    resp = admin.get("/api/control/messages/export?from=2026-10-05&to=2026-10-05")
    assert resp.status_code == 200 and "attachment" in resp.headers["Content-Disposition"]
    assert resp.headers["Cache-Control"] == "no-store"
    body = json.loads(resp.get_data(as_text=True))
    assert body["count"] == 1 and body["messages"][0]["content"] == "测试：导出这一句"


def test_overview_has_status_but_no_private_content(admin, violetdb):
    violetdb.add_user_message("text", SECRET_CHAT)
    mem = violetdb.add_event_memory("测试记忆：" + SECRET_CHAT, category="private")
    violetdb.execute("UPDATE event_memories SET content = content WHERE id = ?", (mem,))
    now = violetdb.now()
    violetdb.execute("INSERT INTO outbound_messages (client_id, origin, message_type, content, status, error, created_at, "
                  "updated_at) VALUES ('t-fail', 'reply', 'text', ?, 'failed', ?, ?, ?)",
                  (SECRET_CHAT, "TimeoutError: " + SECRET_CHAT, now, now))
    resp = admin.get("/api/control/overview")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert SECRET_CHAT not in text
    data = resp.get_json()["data"]
    assert data["agents"][0]["agent"] == "FakeAgent"
    assert {"where": "发出的消息", "type": "TimeoutError"}.items() <= data["errors"][0].items()


# ---------------------------------------------------------------- front end

def test_control_page_script_never_builds_html_from_data():
    root = Path(__file__).resolve().parent.parent / "wx_ilink_py"
    js = (root / "static" / "control.js").read_text(encoding="utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in js, banned
    html = (root / "templates" / "control.html").read_text(encoding="utf-8")
    assert "<script src=\"/static/control.js\"></script>" in html and "{{" not in html
    assert "localStorage" not in js and "sessionStorage" not in js


def test_the_old_memories_page_is_gone():
    """The old memories.js (audit fix: a model-written category pasted into an inline onclick) went with the old
    panel in 0.7.1; the Control Center builds DOM nodes only."""
    assert not (Path(__file__).resolve().parent.parent / "wx_ilink_py" / "static" / "memories.js").exists()


def test_admin_cli_sets_a_password_without_echoing_it(env_guard, capsys):
    from tools import violet_admin
    from wx_ilink_py.auth import AdminStore
    answers = iter([PASSWORD, PASSWORD])
    assert violet_admin.main(["set-password"], read_secret=lambda _p: next(answers)) == 0
    assert PASSWORD not in capsys.readouterr().out
    assert AdminStore(env_guard).verify_password(PASSWORD)
    mismatch = iter([PASSWORD, "something else entirely"])
    assert violet_admin.main(["set-password"], read_secret=lambda _p: next(mismatch)) == 2
    weak = iter(["short"])
    assert violet_admin.main(["set-password"], read_secret=lambda _p: next(weak)) == 2
    assert violet_admin.main(["status"]) == 0 and "已设置" in capsys.readouterr().out
