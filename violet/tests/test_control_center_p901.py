"""Phase 9.0.1 -- Control Center security follow-up: token revocation, HTTPS-only behind a proxy, nothing secret in
logs, write integrity, page values = running values, legacy inline handlers.

Fictional data only, an isolated data dir per test, no network, no real model.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import pytest

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}
JSON = {"Accept": "application/json"}
PASSWORD = "lantern orchard 7 rivers"
ROOT = Path(__file__).resolve().parent.parent


class FakeAgent:
    def __init__(self):
        self.reloads = 0

    def reload(self):
        self.reloads += 1


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


def make_app(data_dir, violetdb, agents=None, scheduler=None, secure=False, monkeypatch=None):
    from wx_ilink_py import ILinkClient, create_app
    if monkeypatch is not None:
        monkeypatch.setenv("VIOLET_DASHBOARD_COOKIE_SECURE", "1" if secure else "0")
    app = create_app(client=ILinkClient(data_dir=data_dir, auto_login=False), violetdb=violetdb,
                     proactive_scheduler=scheduler, all_agents=agents if agents is not None else [FakeAgent()],
                     admin_token=TOKEN)
    app.testing = True
    return app


@pytest.fixture()
def app(env_guard, violetdb, monkeypatch):
    return make_app(env_guard, violetdb, monkeypatch=monkeypatch)


def login(app, secret, **kw):
    c = app.test_client()
    return c, c.post("/auth/login", data={"password": secret}, headers=SAME, **kw)


def bearer(app, token):
    return app.test_client().get("/api/control/overview", headers={"Authorization": f"Bearer {token}", **JSON})


# ---------------------------------------------------------------- 1. the admin token can be disabled / rotated

def test_token_cannot_be_disabled_without_a_password(app):
    with pytest.raises(ValueError):
        app.extensions["violet_admin_store"].disable_token()
    assert bearer(app, TOKEN).status_code == 200


def test_disabled_token_stops_working_everywhere_at_once(app):
    store = app.extensions["violet_admin_store"]
    store.set_password(PASSWORD)
    via_token, resp = login(app, TOKEN)
    assert resp.status_code == 302 and via_token.get("/api/control/overview").status_code == 200
    store.disable_token()
    assert bearer(app, TOKEN).status_code == 401                              # scripts
    assert login(app, TOKEN)[1].status_code == 401                            # login form
    assert via_token.get("/api/control/overview", headers=JSON).status_code == 401   # its session ended
    c, resp = login(app, PASSWORD)
    assert resp.status_code == 302
    bad = c.post("/auth/password", headers=SAME, json={"current": TOKEN, "new": "x" * 3 + PASSWORD, "confirm": "x" * 3 + PASSWORD})
    assert bad.status_code == 403                                             # not a "current credential" any more
    assert c.get("/auth/status").get_json()["token_mode"] == "disabled"


def test_rotation_replaces_the_token_and_keeps_only_a_hash(app, env_guard):
    store = app.extensions["violet_admin_store"]
    new = store.rotate_token()
    assert new != TOKEN and len(new) >= 40
    assert bearer(app, TOKEN).status_code == 401                              # VIOLET_ADMIN_TOKEN no longer works
    assert bearer(app, new).status_code == 200
    assert login(app, new)[1].status_code == 302
    raw = (env_guard / "control" / "admin.json").read_text(encoding="utf-8")
    assert new not in raw and json.loads(raw)["token_hash"].startswith("sha256:")
    newer = store.rotate_token()
    assert bearer(app, new).status_code == 401 and bearer(app, newer).status_code == 200
    store.enable_default_token()
    assert bearer(app, newer).status_code == 401 and bearer(app, TOKEN).status_code == 200


def test_unreadable_admin_settings_fail_closed(app, env_guard):
    store = app.extensions["violet_admin_store"]
    store.set_password(PASSWORD)
    (env_guard / "control" / "admin.json").write_text("{not json", encoding="utf-8")
    assert bearer(app, TOKEN).status_code == 401
    assert login(app, PASSWORD)[1].status_code == 401
    store.set_password(PASSWORD)                                              # the repair path
    assert login(app, PASSWORD)[1].status_code == 302
    assert bearer(app, TOKEN).status_code == 401                              # still off until token-enable
    store.enable_default_token()
    assert bearer(app, TOKEN).status_code == 200


def test_cli_token_commands(env_guard, capsys):
    from tools import violet_admin
    assert violet_admin.main(["token-disable"]) == 2                              # no password yet
    answers = iter([PASSWORD, PASSWORD])
    assert violet_admin.main(["set-password"], read_secret=lambda _p: next(answers)) == 0
    capsys.readouterr()
    assert violet_admin.main(["token-rotate"]) == 0
    printed = capsys.readouterr().out
    new = printed.strip().splitlines()[-1]
    from wx_ilink_py.auth import AdminStore
    store = AdminStore(env_guard)
    assert store.token_matches(new, TOKEN) and not store.token_matches(TOKEN, TOKEN)
    assert violet_admin.main(["token-disable"]) == 0 and store.token_mode() == "disabled"
    assert violet_admin.main(["status"]) == 0 and "已禁用" in capsys.readouterr().out
    assert violet_admin.main(["token-enable"]) == 0 and store.token_matches(TOKEN, TOKEN)


# ---------------------------------------------------------------- 2. HTTPS is enforced, not assumed

@pytest.fixture()
def secure_app(env_guard, violetdb, monkeypatch):
    return make_app(env_guard, violetdb, secure=True, monkeypatch=monkeypatch)


def test_https_required_refuses_plain_http_before_reading_credentials(secure_app):
    remote = {"REMOTE_ADDR": "198.51.100.7"}
    c = secure_app.test_client()
    resp = c.post("/auth/login", data={"token": TOKEN}, headers=SAME, environ_base=remote)
    assert resp.status_code == 403 and "Set-Cookie" not in resp.headers
    assert c.get("/api/control/overview", headers={"Authorization": f"Bearer {TOKEN}"}, environ_base=remote).status_code == 403
    # proxied plain HTTP (the proxy is on this machine but says the client used http) is refused as well
    assert c.post("/auth/login", data={"token": TOKEN}, headers={**SAME, "X-Forwarded-Proto": "http"}).status_code == 403
    # a direct local connection (SSH tunnel) is allowed
    assert c.get("/api/control/overview", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_https_requests_get_hsts(secure_app):
    c = secure_app.test_client()
    resp = c.get("/api/control/overview", base_url="https://localhost",
                 headers={"Authorization": f"Bearer {TOKEN}"}, environ_base={"REMOTE_ADDR": "198.51.100.7"})
    assert resp.status_code == 200 and resp.headers["Strict-Transport-Security"].startswith("max-age=")
    assert "Strict-Transport-Security" not in c.get("/healthz").headers                 # never on plain http


def _probe_app():
    from flask import Flask, jsonify, request
    probe = Flask("probe")

    @probe.get("/who")
    def who():
        return jsonify(addr=request.remote_addr, secure=request.is_secure)
    return probe


def test_forwarded_headers_are_trusted_only_from_this_machine():
    from werkzeug.test import Client
    from wx_ilink_py.auth import trusted_proxy_app
    client = Client(trusted_proxy_app(_probe_app()))
    spoof = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}
    direct = client.get("/who", headers=spoof, environ_base={"REMOTE_ADDR": "198.51.100.7"}).json
    assert direct == {"addr": "198.51.100.7", "secure": False}                   # a remote client cannot spoof
    via_proxy = client.get("/who", headers=spoof, environ_base={"REMOTE_ADDR": "127.0.0.1"}).json
    assert via_proxy == {"addr": "203.0.113.9", "secure": True}


def test_spoofed_forwarded_for_does_not_dodge_the_login_lock(app):
    from werkzeug.test import Client
    from wx_ilink_py.auth import trusted_proxy_app
    client = Client(trusted_proxy_app(app))
    for i in range(5):
        client.post("/auth/login", data={"token": "wrong-wrong-wrong"}, headers={**SAME, "X-Forwarded-For": f"10.9.0.{i}"},
                    environ_base={"REMOTE_ADDR": "198.51.100.7"})
    locked = client.post("/auth/login", data={"token": TOKEN}, headers={**SAME, "X-Forwarded-For": "10.9.0.99"},
                         environ_base={"REMOTE_ADDR": "198.51.100.7"})
    assert locked.status_code == 429


# ---------------------------------------------------------------- 3. nothing secret in logs or the change history

def test_no_search_terms_memory_text_or_credentials_in_logs(app, violetdb, env_guard, caplog):
    caplog.set_level(logging.DEBUG)
    secret_q, secret_mem, secret_key = "查询词-9QX7", "记忆原文-ZK31", "sk-test-log-probe-55555"
    store = app.extensions["violet_admin_store"]
    store.set_password(PASSWORD)
    violetdb.add_user_message("text", "测试：" + secret_q)
    mem = violetdb.add_event_memory("测试记忆：旧", category="t")
    login(app, "wrong " + PASSWORD)
    c, _ = login(app, PASSWORD)
    c.get(f"/api/control/messages?q={secret_q}")
    c.get(f"/api/control/memories?kind=event&q={secret_q}")
    c.post(f"/api/control/memories/event/{mem}/correct", headers=SAME, json={"content": secret_mem})
    c.put("/api/control/settings", headers=SAME, json={"changes": {"LLM_API_KEY": secret_key}})
    c.put("/api/control/settings", headers=SAME, json={"changes": {"LLM_API_URL": f"https://u:{secret_key}@relay.test/v1"}})
    c.post("/auth/password", headers=SAME, json={"current": "nope " + PASSWORD, "new": "a", "confirm": "a"})
    c.get("/api/control/memories/event/999999/history")
    text = caplog.text
    assert "failed dashboard login" in text and "violet.dashboard.auth" in "".join(r.name for r in caplog.records)
    for needle in (secret_q, secret_mem, secret_key, PASSWORD, TOKEN):
        assert needle not in text, needle
    history = (env_guard / "control" / "history.jsonl").read_text(encoding="utf-8")
    assert secret_key not in history and "relay.test" not in history          # the URL with a password was refused
    assert PASSWORD not in (env_guard / "control" / "admin.json").read_text(encoding="utf-8")


def test_url_with_credentials_or_query_is_refused():
    from ai.control_config import validate
    for bad in ("https://user:pw@relay.test/v1", "https://relay.test/v1?key=abc", "https://relay.test/v1#x",
                "https://@relay.test/v1"):
        with pytest.raises(ValueError):
            validate("LLM_API_URL", bad)
    assert validate("LLM_API_URL", "https://relay.test/v1/") == "https://relay.test/v1"


def test_main_turns_off_the_access_log_and_flask_logs_only_the_path():
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 'logging.getLogger("werkzeug").setLevel(logging.WARNING)' in text
    import flask.app
    import inspect
    src = inspect.getsource(flask.app.Flask.log_exception)
    assert "request.path" in src and "full_path" not in src and "args" not in src.split("request.path")[1][:40]


# ---------------------------------------------------------------- 4. integrity when writing fails

def test_a_failed_file_write_leaves_everything_as_it_was(env_guard, monkeypatch):
    from ai import control_config as cc
    store = cc.ControlStore(env_guard)
    store.save({"DEBOUNCE_NORMAL": 3, "LLM_API_KEY": "sk-test-before-0001"}, "test", apply=cc.apply_overrides)
    before = {p.name: p.read_bytes() for p in (store.settings_path, store.secrets_path, store.history_path)}
    env_before = (os.environ["DEBOUNCE_NORMAL"], os.environ["LLM_API_KEY"])
    real_write = cc.ControlStore._write

    def flaky(self, path, data, secret=False):
        if path.name == "secrets.json":
            raise OSError("disk full (simulated)")
        return real_write(self, path, data, secret)
    monkeypatch.setattr(cc.ControlStore, "_write", flaky)
    with pytest.raises(OSError):
        store.save({"DEBOUNCE_NORMAL": 8, "LLM_API_KEY": "sk-test-after-0002"}, "test", apply=cc.apply_overrides)
    after = {p.name: p.read_bytes() for p in (store.settings_path, store.secrets_path, store.history_path)}
    assert after == before
    assert (os.environ["DEBOUNCE_NORMAL"], os.environ["LLM_API_KEY"]) == env_before
    assert not list(store.dir.glob("*.tmp"))


def test_a_failed_replace_leaves_no_temporary_file(env_guard, monkeypatch):
    """The real _write: the temp file is written, the final os.replace fails (e.g. the target is locked)."""
    from ai import control_config as cc
    store = cc.ControlStore(env_guard)
    store.save({"DEBOUNCE_NORMAL": 3}, "test")
    before = store.settings_path.read_bytes()
    real_replace = os.replace
    failed = []

    def locked(src, dst):
        if Path(dst).name == "settings.json" and not failed:
            failed.append(dst)
            raise PermissionError("target locked (simulated)")
        return real_replace(src, dst)
    monkeypatch.setattr(cc.os, "replace", locked)
    with pytest.raises(PermissionError):
        store.save({"DEBOUNCE_NORMAL": 8}, "test")
    assert failed and store.settings_path.read_bytes() == before
    assert not list(store.dir.glob("*.tmp"))


def test_a_failed_history_append_undoes_the_save(env_guard, monkeypatch):
    from ai import control_config as cc
    store = cc.ControlStore(env_guard)
    store.save({"DEBOUNCE_NORMAL": 3}, "test")
    settings_before = store.settings_path.read_bytes()
    real_open = Path.open

    def failing_open(self, *a, **k):
        if self.name == "history.jsonl" and a and a[0] == "a":
            raise OSError("read-only (simulated)")
        return real_open(self, *a, **k)
    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(OSError):
        store.save({"DEBOUNCE_NORMAL": 9}, "test")
    assert store.settings_path.read_bytes() == settings_before and len(store.history()) == 1


def test_an_unreadable_settings_file_is_never_overwritten(app, env_guard):
    c, _ = login(app, TOKEN)
    (env_guard / "control").mkdir(parents=True, exist_ok=True)
    damaged = env_guard / "control" / "settings.json"
    damaged.write_text('{"values": {"DEBOUNCE_NORMAL": 3', encoding="utf-8")
    resp = c.put("/api/control/settings", headers=SAME, json={"changes": {"DEBOUNCE_EXTRA": 20}})
    assert resp.status_code == 500 and "没有保存" in resp.get_json()["error"]
    assert damaged.read_text(encoding="utf-8") == '{"values": {"DEBOUNCE_NORMAL": 3'
    from ai.control_config import apply_overrides
    apply_overrides()                                                          # runtime: no crash, .env stays


def test_restore_after_replacing_the_env_key_goes_back_to_env(app, env_guard):
    """Found by the 9.0.1 end-to-end run: the first replacement of a .env key left nothing to restore."""
    c, _ = login(app, TOKEN)
    env_key = os.environ["LLM_API_KEY"]
    assert c.put("/api/control/settings", headers=SAME, json={"changes": {"LLM_API_KEY": "sk-test-dashboard-1111"}}).status_code == 200
    assert os.environ["LLM_API_KEY"] == "sk-test-dashboard-1111"
    resp = c.post("/api/control/settings/restore-secret", headers=SAME, json={"key": "LLM_API_KEY", "confirm": True})
    assert resp.status_code == 200 and os.environ["LLM_API_KEY"] == env_key
    rows = {r["key"]: r for r in c.get("/api/control/settings").get_json()["data"]["settings"]}
    assert rows["LLM_API_KEY"]["source"] == "env"
    again = c.post("/api/control/settings/restore-secret", headers=SAME, json={"key": "LLM_API_KEY", "confirm": True})
    assert again.status_code == 200 and os.environ["LLM_API_KEY"] == "sk-test-dashboard-1111"   # an undo of the undo


def test_non_finite_numbers_and_tampered_history_are_refused(app, env_guard):
    c, _ = login(app, TOKEN)
    for bad in ("NaN", "inf", "-inf", True):
        assert c.put("/api/control/settings", headers=SAME, json={"changes": {"DEBOUNCE_NORMAL": bad}}).status_code == 400
    c.put("/api/control/settings", headers=SAME, json={"changes": {"DEBOUNCE_NORMAL": 3}})
    history = env_guard / "control" / "history.jsonl"
    entry = json.loads(history.read_text(encoding="utf-8").splitlines()[0])
    entry["snapshot"]["DEBOUNCE_NORMAL"] = 99999
    history.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    resp = c.post("/api/control/settings/rollback", headers=SAME, json={"revision": 1, "confirm": True})
    assert resp.status_code == 400
    assert os.environ["DEBOUNCE_NORMAL"] == "3"


def test_memory_change_and_its_revision_commit_together(app, violetdb, monkeypatch):
    c, _ = login(app, TOKEN)
    mem = violetdb.add_core_memory("p901_key", "test", "测试：原来的内容")

    def boom(*_a, **_k):
        raise RuntimeError("delete failed (simulated)")
    monkeypatch.setattr(violetdb, "delete_core_memory", boom)
    with pytest.raises(RuntimeError):
        c.post(f"/api/control/memories/core/{mem}/delete", headers=SAME, json={"confirm": True})
    assert violetdb.get_core_memory(mem)["content"] == "测试：原来的内容"
    assert violetdb.list_memory_revisions("core_memories", mem) == []          # no orphan "deleted" record
    monkeypatch.undo()
    real_execute = violetdb.execute

    def failing_update(sql, params=()):
        if sql.startswith("UPDATE core_memories"):
            raise RuntimeError("update failed (simulated)")
        return real_execute(sql, params)
    monkeypatch.setattr(violetdb, "execute", failing_update)
    with pytest.raises(RuntimeError):
        c.post(f"/api/control/memories/core/{mem}/correct", headers=SAME, json={"content": "测试：新内容"})
    monkeypatch.undo()
    assert violetdb.get_core_memory(mem)["content"] == "测试：原来的内容"
    assert violetdb.list_memory_revisions("core_memories", mem) == []


# ---------------------------------------------------------------- 5. the page shows what Violet is using

def test_agent_settings_show_the_running_agent_values(env_guard, violetdb, monkeypatch):
    from ai.agents import ChatAgent
    monkeypatch.delenv("CHAT_LLM_MODEL", raising=False)
    monkeypatch.delenv("CHAT_LLM_MODEL_TEMPERATURE", raising=False)
    from ai.control_config import PROMPT_DIRS
    monkeypatch.setenv("CHAT_LLM_PROMPT", (PROMPT_DIRS["chat"] / "chat.v1.txt").read_text(encoding="utf-8"))
    agent = ChatAgent()
    app = make_app(env_guard, violetdb, agents=[agent], monkeypatch=monkeypatch)
    c, _ = login(app, TOKEN)
    rows = {r["key"]: r for r in c.get("/api/control/settings").get_json()["data"]["settings"]}
    assert rows["CHAT_LLM_MODEL"]["value"] == agent._config.reply_model == agent.default_model
    assert rows["CHAT_LLM_MODEL_TEMPERATURE"]["value"] == agent._config.reply_temperature
    assert rows["prompt.chat"]["running_version"] == "v1" and rows["prompt.chat"]["value"] == ""
    c.put("/api/control/settings", headers=SAME, json={"changes": {"CHAT_LLM_MODEL": "dash-chat", "prompt.chat": "v2"}})
    rows = {r["key"]: r for r in c.get("/api/control/settings").get_json()["data"]["settings"]}
    assert rows["CHAT_LLM_MODEL"]["value"] == "dash-chat" == agent._config.reply_model
    assert rows["prompt.chat"]["running_version"] == "v2"


def test_proactive_settings_show_the_scheduler_config(env_guard, violetdb, tmp_path, monkeypatch):
    from ai.proactive_scheduler import ProactiveScheduler
    cfg = tmp_path / "proactive.json"
    cfg.write_text(json.dumps({"quiet_hours": {"start": "02:30", "end": "07:15"}, "daily_message_limit": 5}), encoding="utf-8")
    sched = ProactiveScheduler(None, None, None, lambda *_: None, config_path=cfg)
    app = make_app(env_guard, violetdb, scheduler=sched, monkeypatch=monkeypatch)
    c, _ = login(app, TOKEN)
    rows = {r["key"]: r for r in c.get("/api/control/settings").get_json()["data"]["settings"]}
    assert (rows["proactive.quiet_start"]["value"], rows["proactive.quiet_end"]["value"]) == ("02:30", "07:15")
    assert rows["proactive.daily_limit"]["value"] == 5 and rows["proactive.daily_limit"]["source"] == "file"
    c.put("/api/control/settings", headers=SAME, json={"changes": {"proactive.daily_limit": 2}})
    rows = {r["key"]: r for r in c.get("/api/control/settings").get_json()["data"]["settings"]}
    assert rows["proactive.daily_limit"]["value"] == 2 == sched.load_config()["daily_message_limit"]


def test_env_values_are_shown_the_way_the_code_reads_them(app, monkeypatch):
    c, _ = login(app, TOKEN)
    monkeypatch.setenv("DEBOUNCE_NORMAL", "not-a-number")
    monkeypatch.delenv("VIOLET_TYPING_MAX_DELAY_SECONDS", raising=False)
    monkeypatch.setenv("VIOLET_RELATIONSHIP_STATE", "off")
    rows = {r["key"]: r for r in c.get("/api/control/settings").get_json()["data"]["settings"]}
    assert rows["DEBOUNCE_NORMAL"]["value"] == 5.0                            # env_float falls back to 5.0
    assert rows["VIOLET_TYPING_MAX_DELAY_SECONDS"]["value"] == 20.0 and rows["VIOLET_TYPING_MAX_DELAY_SECONDS"]["source"] == "default"
    assert rows["VIOLET_RELATIONSHIP_STATE"]["value"] is False


def test_main_reload_hook_makes_the_two_switches_immediate(monkeypatch):
    import main
    started = []
    monkeypatch.setattr(main.life_scheduler, "start", lambda: started.append(1) or True)
    monkeypatch.setenv("VIOLET_SIMULATE_TYPING", "0")
    main.reload_runtime_switches()
    assert main.runtime.simulate_typing is False and started == []           # tests never start the life thread
    monkeypatch.setattr(main, "_LIFE_STARTED", type("E", (), {"is_set": staticmethod(lambda: True)})())
    monkeypatch.setenv("VIOLET_SIMULATE_TYPING", "1")
    main.reload_runtime_switches()
    assert main.runtime.simulate_typing is True and started == [1]
    # and it is wired into every reload (the Control Center's save calls the same reload_all as /reload-env)
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "extra_reload_hooks=[reload_debounce_config, reload_runtime_switches]" in src


# ---------------------------------------------------------------- 6. legacy pages: no data inside inline handlers

ALLOWED = ("+", "jsarg(", "esc(JSON.stringify(", "!!")
CONSTANT = {"s === '全部' ? 'null' : '\"' + s + '\"'"}                      # memories.js: a fixed status list


def test_inline_handlers_only_take_numbers_or_json_strings():
    files = list((ROOT / "wx_ilink_py" / "templates").glob("*.html")) + list((ROOT / "wx_ilink_py" / "static").glob("*.js"))
    seen = 0
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            for handler in re.findall(r'on\w+="(.*?)">', line):
                for expr in re.findall(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", handler):
                    seen += 1
                    assert expr.startswith(ALLOWED) or expr in CONSTANT, f"{path.name}: ${{{expr}}}"
    assert seen > 20
