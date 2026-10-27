"""Violet stage 2: 1Panel-style access -- security entrance, username + password, `violet info | reset-password |
reset-entrance | set-port`. Temp data dir, no network, no service."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}
JSON = {"Accept": "application/json"}
PASSWORD = "lantern orchard 7 rivers"
USER = "violet_abc234"
ENTRANCE = "k3x9q2m7wz8a"


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
def store(env_guard):
    from wx_ilink_py.auth import AdminStore
    s = AdminStore(env_guard)
    s.set_password(PASSWORD)
    return s


@pytest.fixture()
def app(env_guard, violetdb, monkeypatch, store):
    from wx_ilink_py import ILinkClient, create_app
    monkeypatch.setenv("VIOLET_DASHBOARD_COOKIE_SECURE", "0")
    made = create_app(client=ILinkClient(data_dir=env_guard, auto_login=False), violetdb=violetdb,
                      proactive_scheduler=None, all_agents=[], admin_token=TOKEN)
    made.testing = True
    return made


def _locked(store):
    store.set_username(USER)
    store.set_entrance(ENTRANCE)


def _enter(app):
    c = app.test_client()
    resp = c.get(f"/{ENTRANCE}")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/auth/login?next=/control")
    return c


def _login(c, username=USER, password=PASSWORD):
    return c.post("/auth/login", data={"username": username, "password": password}, headers=SAME)


# ------------------------------------------------------------------------------------------------ entrance

def test_without_an_entrance_everything_works_as_before_and_any_username_is_accepted(app, store):
    c = app.test_client()
    assert c.get("/auth/login").status_code == 200
    assert _login(c, username="whoever").status_code == 302          # no username set yet: not checked


def test_without_the_entrance_every_path_is_a_plain_404(app, store):
    _locked(store)
    c = app.test_client()
    for path in ("/auth/login", "/control", "/static/control.js", "/api/control/overview", "/", "/auth/status",
                 f"/{ENTRANCE}x", f"/{ENTRANCE.upper()}"):
        resp = c.get(path, headers=JSON)
        assert resp.status_code == 404 and resp.get_data(as_text=True) == "404 Not Found\n", path
    assert _login(c).status_code == 404                                 # not even the password is looked at
    # what still answers: the installer's health check, a valid Bearer token, signed channel webhooks
    assert c.get("/healthz").get_json() == {"ok": True, "service": "violet"}
    assert c.get("/api/control/overview", headers={"Authorization": f"Bearer {TOKEN}", **JSON}).status_code == 200
    assert c.get("/api/control/overview", headers={"Authorization": "Bearer wrong-token", **JSON}).status_code == 404


def test_through_the_entrance_the_login_needs_the_username_and_survives_logout(app, store):
    _locked(store)
    c = _enter(app)
    assert c.get("/auth/login").status_code == 200
    assert _login(c, username="admin").status_code == 401
    assert _login(c, password="wrong password!!").status_code == 401
    assert _login(c, username=USER.upper()).status_code == 302          # the username is not case-sensitive
    assert c.get("/api/control/overview", headers=JSON).status_code == 200
    assert c.get(f"/{ENTRANCE}/").headers["Location"] == "/control"    # logged in: straight to the Control Center
    c.post("/auth/logout", headers=SAME)
    assert c.get("/api/control/overview", headers=JSON).status_code == 401   # logged out ...
    assert c.get("/auth/login").status_code == 200                           # ... but the login page stays reachable
    # the admin token still logs in on its own (no username)
    assert c.post("/auth/login", data={"token": TOKEN}, headers=SAME).status_code == 302


def test_the_session_cookie_does_not_reveal_the_entrance(app, store):
    _locked(store)
    c = _enter(app)
    cookie = c.get_cookie("violet_admin_session")
    import base64
    import zlib
    raw = cookie.value.lstrip(".").split(".")[0]
    payload = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    if cookie.value.startswith("."):
        payload = zlib.decompress(payload)
    assert ENTRANCE.encode() not in payload


def test_a_new_entrance_locks_out_other_browsers_but_not_the_one_that_changed_it(app, store):
    _locked(store)
    admin, other = _enter(app), _enter(app)
    assert _login(admin).status_code == 302 and _login(other).status_code == 302
    resp = admin.post("/auth/account", json={"current": PASSWORD, "entrance_random": True}, headers=SAME)
    body = resp.get_json()
    assert resp.status_code == 200 and body["changed"] == ["entrance"] and body["entrance"] != ENTRANCE
    assert admin.get("/api/control/overview", headers=JSON).status_code == 200
    assert other.get("/api/control/overview", headers=JSON).status_code == 404
    assert other.get(f"/{ENTRANCE}").status_code == 404                     # the old entrance is gone
    assert other.get(f"/{body['entrance']}").status_code == 302
    assert other.get("/api/control/overview", headers=JSON).status_code == 200   # its login itself was not ended


def test_account_changes_need_the_current_password_and_valid_values(app, store):
    _locked(store)
    c = _enter(app)
    _login(c)
    post = lambda body: c.post("/auth/account", json=body, headers=SAME)
    assert post({"current": "wrong", "username": "someone"}).status_code == 403
    assert store.username() == USER
    for bad in ({"username": "ab"}, {"username": "1abc"}, {"username": "has space"},
                {"entrance": "short"}, {"entrance": "control"}, {"entrance": "Static"}, {"entrance": "a/b/c/d/e"},
                {"entrance": "../../etc"}):
        resp = post({"current": PASSWORD, **bad})
        assert resp.status_code == 400 and resp.get_json()["error"], bad
    assert store.username() == USER and store.entrance() == ENTRANCE
    assert post({"current": PASSWORD, "username": USER, "entrance": ENTRANCE}).get_json()["changed"] == []
    # a cross-origin post is refused before anything is read
    assert c.post("/auth/account", json={"current": PASSWORD, "username": "eviluser"},
                  headers={"Origin": "http://evil.example"}).status_code == 403


def test_a_new_username_ends_the_other_sessions_and_keeps_this_one(app, store):
    _locked(store)
    admin, other = _enter(app), _enter(app)
    _login(admin), _login(other)
    resp = admin.post("/auth/account", json={"current": PASSWORD, "username": "Owner.main"}, headers=SAME)
    assert resp.status_code == 200 and resp.get_json()["changed"] == ["username"]
    assert admin.get("/api/control/overview", headers=JSON).status_code == 200
    assert other.get("/api/control/overview", headers=JSON).status_code == 401
    assert _login(other).status_code == 401 and _login(other, username="owner.main").status_code == 302


def test_status_reports_username_entrance_and_port(app, store, monkeypatch):
    _locked(store)
    monkeypatch.setenv("VIOLET_DASHBOARD_PORT", "7123")
    c = _enter(app)
    _login(c)
    s = c.get("/auth/status").get_json()
    assert s["username"] == USER and s["entrance"] == ENTRANCE and s["port"] == 7123


def test_a_damaged_entrance_value_is_ignored_and_never_matches_a_program_path(store):
    data = store.load()
    for bad in ("control", "a b", "", "x" * 65, None, 12345):
        data["entrance"] = bad
        store._save(data)
        assert store.entrance() == ""
    from wx_ilink_py.auth import RESERVED_ENTRANCES, generate_entrance, generate_username
    values = {generate_entrance() for _ in range(50)}
    assert len(values) == 50 and all(len(v) == 12 and v not in RESERVED_ENTRANCES and v.isalnum() for v in values)
    assert not (set("".join(values)) & set("01lo"))
    assert generate_username().startswith("violet_")


def test_the_entrance_does_not_shadow_an_existing_route(app):
    """Every first path segment the program serves must be reserved: an entrance with that name would hide it."""
    from wx_ilink_py.auth import RESERVED_ENTRANCES
    used = {rule.rule.strip("/").split("/")[0].lower() for rule in app.url_map.iter_rules()}
    used = {u for u in used if u and not u.startswith("<")}
    assert used <= RESERVED_ENTRANCES, sorted(used - RESERVED_ENTRANCES)


# ------------------------------------------------------------------------------------------------ violet_setup

def _setup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VIOLET_DB_PATH", str(tmp_path / "data" / "violet.db"))
    monkeypatch.setenv("VIOLET_ENV_FILE", str(tmp_path / "none.env"))


def test_a_new_install_gets_a_random_username_and_entrance_an_old_one_keeps_its_login(env_guard, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from tools import violet_setup
    from wx_ilink_py.auth import AdminStore
    first = violet_setup.init()["admin"]
    assert first["user"].startswith("violet_") and len(first["entrance"]) == 12 and first["password"]
    again = violet_setup.init()["admin"]
    assert again["user"] == first["user"] and again["entrance"] == first["entrance"] and "password" not in again
    # an install from before stage 2 (a password, no username / entrance): an upgrade adds nothing
    old = AdminStore(tmp_path / "old")
    old.set_password(PASSWORD)
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "old"))
    kept = violet_setup.init()["admin"]
    assert kept["user"] == "admin" and kept["entrance"] == "" and old.username() == ""


def test_account_info_reset_password_and_reset_entrance(env_guard, monkeypatch, tmp_path, capsys):
    _setup_env(monkeypatch, tmp_path)
    from tools import violet_setup
    from wx_ilink_py.auth import AdminStore
    made = violet_setup.init()["admin"]
    store = AdminStore(env_guard)
    store.set_password(PASSWORD)
    info = violet_setup.account("info")
    assert info == {"ok": True, "user": made["user"], "username_set": True, "entrance": made["entrance"],
                    "password_set": True, "must_change": False}
    version = store.session_version()
    reset = violet_setup.account("reset-password")
    assert len(reset["password"]) == 20 and reset["must_change"] and store.verify_password(reset["password"])
    assert store.session_version() > version and not store.verify_password(PASSWORD)
    moved = violet_setup.account("reset-entrance")
    assert moved["entrance"] != made["entrance"] and "password" not in moved
    assert violet_setup.account("reset-entrance", "my-own_entrance")["entrance"] == "my-own_entrance"
    assert violet_setup.account("reset-entrance", "")["entrance"] == ""
    assert violet_setup.main(["account", "reset-entrance", "--entrance", "control", "--json"]) == 1
    assert "安全入口" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])["error"]
    assert violet_setup.main(["account", "info", "--json"]) == 0
    assert "password" not in json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# ------------------------------------------------------------------------------------------------ installer

def _installed(tmp_path, port=7100, access="lan", service="none", host="0.0.0.0", tls=True):
    from tools import violet_installer as inst
    layout = inst.Layout(tmp_path / "root")
    layout.config.mkdir(parents=True)
    env = f"VIOLET_DASHBOARD_HOST={host}\nVIOLET_DASHBOARD_PORT={port}\n" + \
          ("VIOLET_DASHBOARD_TLS_CERT=/x/cert.pem\n" if tls else "")
    layout.env_file.write_text(env, encoding="utf-8")
    inst.save_state(layout, {"version": "0.2.0", "port": port, "access": access, "service": service})
    return inst, layout


def test_panel_urls_carry_the_entrance():
    from tools import violet_installer as inst
    assert inst.panel_urls("https", 7100, "0.0.0.0", "abcdefgh", lan=lambda: ["192.168.1.5"]) == \
        ["https://127.0.0.1:7100/abcdefgh", "https://192.168.1.5:7100/abcdefgh"]
    assert inst.panel_urls("http", 7100, "127.0.0.1", "", lan=lambda: ["192.168.1.5"]) == ["http://127.0.0.1:7100/control"]


def test_installer_account_runs_the_programs_own_code_and_adds_the_address(tmp_path, monkeypatch):
    inst, layout = _installed(tmp_path)
    monkeypatch.setattr(inst, "lan_addresses", lambda: ["10.0.0.8"])
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        out = {"ok": True, "user": "violet_x", "username_set": True, "entrance": "newentrance1", "password_set": True,
               "must_change": False}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out) + "\n", stderr="")
    result = inst.account(inst.Options(root=layout.root), "reset-entrance", "newentrance1", run=run)
    assert seen[0][-6:] == ["tools.violet_setup", "account", "reset-entrance", "--json", "--entrance", "newentrance1"]
    assert result["urls"] == ["https://127.0.0.1:7100/newentrance1", "https://10.0.0.8:7100/newentrance1"]
    text = inst.render_account(result, "reset-entrance")
    assert "https://10.0.0.8:7100/newentrance1" in text and "violet_x" in text and "密码" not in text.split("用户名")[1]
    inst.account(inst.Options(root=layout.root), "reset-entrance", "", run=run)
    assert seen[-1][-1] == "--no-entrance"

    def failing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout=json.dumps({"ok": False, "error": "ValueError: 安全入口要"}),
                                           stderr="")
    with pytest.raises(inst.InstallError, match="安全入口要"):
        inst.account(inst.Options(root=layout.root), "reset-entrance", "control", run=failing)


def test_installer_account_needs_an_install(tmp_path):
    from tools import violet_installer as inst
    with pytest.raises(inst.InstallError, match="没有安装"):
        inst.account(inst.Options(root=tmp_path / "nothing"), "info")


def test_set_port_rewrites_the_config_and_state(tmp_path, monkeypatch):
    inst, layout = _installed(tmp_path)
    monkeypatch.setattr(inst, "port_free", lambda port, host: True)
    monkeypatch.setattr(inst, "firewall_status", lambda: {"tool": "ufw", "active": True})
    result = inst.set_port(inst.Options(root=layout.root, port=8443))
    assert result["changed"] and result["port"] == 8443 and result["previous"] == 7100
    assert "ufw allow 8443/tcp" in result["firewall"]                     # suggested, not run (no --open-firewall)
    assert "VIOLET_DASHBOARD_PORT=8443" in layout.env_file.read_text(encoding="utf-8")
    assert inst.load_state(layout)["port"] == 8443
    assert inst.set_port(inst.Options(root=layout.root, port=8443))["changed"] is False
    with pytest.raises(inst.InstallError, match="1024"):
        inst.set_port(inst.Options(root=layout.root, port=80))
    with pytest.raises(inst.InstallError, match="指定新端口"):
        inst.set_port(inst.Options(root=layout.root))


def test_set_port_puts_the_old_port_back_when_the_service_does_not_come_up(tmp_path, monkeypatch):
    inst, layout = _installed(tmp_path, service="supervisor")
    monkeypatch.setattr(inst, "port_free", lambda port, host: True)
    monkeypatch.setattr(inst, "firewall_status", lambda: {"active": False})
    calls = []
    monkeypatch.setattr(inst.Service, "stop", lambda self: calls.append("stop"))
    monkeypatch.setattr(inst.Service, "start", lambda self: calls.append("start"))
    monkeypatch.setattr(inst, "wait_healthy", lambda *a, **k: False)
    with pytest.raises(inst.InstallError, match="已改回 7100"):
        inst.set_port(inst.Options(root=layout.root, port=8443, health_timeout=1))
    assert calls == ["stop", "start", "stop", "start"]
    assert "VIOLET_DASHBOARD_PORT=7100" in layout.env_file.read_text(encoding="utf-8")
    assert inst.load_state(layout)["port"] == 7100


def test_open_firewall_port_only_runs_with_permission():
    from tools import violet_installer as inst
    ran = []
    run = lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0)
    ufw = lambda: {"tool": "ufw", "active": True}
    assert inst.open_firewall_port(7100, "local", True, firewall=ufw, run=run) is None
    assert "ufw allow 7100/tcp" in inst.open_firewall_port(7100, "lan", False, firewall=ufw, run=run) and not ran
    assert inst.open_firewall_port(7100, "public", True, firewall=ufw, run=run) == "已在本机防火墙放行端口 7100"
    assert ran == [["ufw", "allow", "7100/tcp"]]


def test_the_install_summary_shows_the_entrance_and_username(tmp_path):
    from tools import violet_installer as inst
    text = inst.render_result({"fresh": True, "root": "/opt/violet", "urls": ["https://1.2.3.4:7100/abcdefgh2345"],
                               "admin": {"user": "violet_q7m2kx", "password": "Pw1" * 6 + "xy", "entrance": "abcdefgh2345"},
                               "port": 7100, "access": "public"})
    assert "https://1.2.3.4:7100/abcdefgh2345" in text and "安全入口：/abcdefgh2345" in text
    assert "用户名：violet_q7m2kx" in text and "reset-password" in text
