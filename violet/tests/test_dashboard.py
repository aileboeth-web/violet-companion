"""Dashboard: every data/admin route requires auth; token is not hardcoded."""

from __future__ import annotations

import json
import os

import pytest

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]

PROTECTED_GET = ["/", "/memories", "/api/status", "/api/memories", "/proactive/status",
                 "/api/pipeline-status", "/api/health"]
PROTECTED_POST = ["/reload-env", "/proactive/test", "/proactive/schedule-now", "/shutdown"]


class StubScheduler:
    def __init__(self):
        self.calls = []

    def force_run(self, send=False, event_context_patch=None, force_task=None):
        self.calls.append(send)
        return {"event_id": 1, "proactive_decision": {"should_act": False}, "chat": None}

    def status_snapshot(self):
        return {"enabled": True}

    def reload(self):
        pass


class StubRuntime:
    def health(self):
        return {"poll_thread_alive": True}


@pytest.fixture()
def make_app(tmp_path, violetdb):
    from wx_ilink_py import ILinkClient, create_app

    def make(admin_token=TOKEN, data_dir=None):
        client = ILinkClient(data_dir=data_dir or (tmp_path / "data"), auto_login=False)
        scheduler = StubScheduler()
        app = create_app(client=client, violetdb=violetdb, proactive_scheduler=scheduler, all_agents=[],
                         runtime=StubRuntime(), admin_token=admin_token)
        app.testing = True
        return app, client, scheduler
    return make


def bearer(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("path", PROTECTED_GET)
def test_get_routes_reject_anonymous(make_app, path):
    app, _, _ = make_app()
    resp = app.test_client().get(path, headers={"Accept": "application/json"})
    assert resp.status_code == 401


def test_html_navigation_redirects_to_login(make_app):
    app, _, _ = make_app()
    resp = app.test_client().get("/memories", headers={"Accept": "text/html"})
    assert resp.status_code == 302 and resp.headers["Location"].startswith("/auth/login")


@pytest.mark.parametrize("path", PROTECTED_POST)
def test_admin_actions_reject_anonymous_and_have_no_side_effects(make_app, monkeypatch, path):
    killed = []
    monkeypatch.setattr(os, "kill", lambda *a: killed.append(a))
    app, client, scheduler = make_app()
    client.credentials_path.write_text(json.dumps({"token": "x", "base_url": "http://127.0.0.1:9", "account_id": "a"}), encoding="utf-8")
    resp = app.test_client().post(path, json={"send": True})
    assert resp.status_code == 401
    assert killed == [] and scheduler.calls == []
    assert client.credentials_path.exists(), "logout must not run without auth"


def test_wrong_token_rejected_and_right_token_accepted(make_app):
    app, _, _ = make_app()
    c = app.test_client()
    assert c.get("/api/memories", headers=bearer("nope")).status_code == 401
    resp = c.get("/api/memories", headers=bearer())
    assert resp.status_code == 200
    assert {"msg_stats", "recent_messages", "core_memories", "event_memories", "proactive_events"} <= set(resp.get_json())


def test_browser_login_and_csrf_protection(make_app):
    app, _, _ = make_app()
    c = app.test_client()
    bad = c.post("/auth/login", data={"token": "wrong"})
    assert bad.status_code == 401
    ok = c.post("/auth/login", data={"token": TOKEN, "next": "/memories"})
    assert ok.status_code == 302 and ok.headers["Location"] == "/memories"
    assert "HttpOnly" in ok.headers["Set-Cookie"] and "SameSite=Strict" in ok.headers["Set-Cookie"]
    assert c.get("/api/memories").status_code == 200
    # cookie-authenticated POST without a same-origin header is refused (CSRF)
    assert c.post("/reload-env").status_code == 403
    assert c.post("/reload-env", headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/reload-env", headers={"Origin": "http://localhost"}).status_code == 200
    c.post("/auth/logout", headers={"Origin": "http://localhost"})
    assert c.get("/api/memories").status_code == 401


def test_open_redirect_is_blocked(make_app):
    app, _, _ = make_app()
    resp = app.test_client().post("/auth/login", data={"token": TOKEN, "next": "//evil.example/x"})
    assert resp.headers["Location"] == "/control"          # stage 3: the default page is the Control Center


def test_public_endpoints_leak_nothing(make_app):
    app, _, _ = make_app()
    c = app.test_client()
    # phase 10.4: the service name only (the installer tells Violet from another program on the port); no version, no state
    assert c.get("/healthz").get_json() == {"ok": True, "service": "violet"}
    assert c.get("/static/control.js").status_code == 200


def test_the_old_pages_are_gone_and_lead_to_the_control_center(make_app):
    """0.7.1 (user: "把旧面板都删了吧"): the old dashboard and its pages; their addresses redirect."""
    app, _, _ = make_app()
    c = app.test_client()
    for path, target in (("/", "/control"), ("/memories", "/control#memories"), ("/web", "/control#tools"),
                         ("/images", "/control#settings"), ("/reconciliation", "/control#diagnostics")):
        resp = c.get(path, headers=bearer())
        assert resp.status_code == 302 and resp.headers["Location"] == target, path
    assert c.post("/login", headers=bearer()).status_code in (404, 405)
    assert c.get("/static/dashboard.js").status_code == 404
    html = c.get("/control", headers=bearer()).get_data(as_text=True)
    assert "旧版面板" not in html


def test_manual_proactive_send_disabled_by_default(make_app, monkeypatch):
    app, _, scheduler = make_app()
    c = app.test_client()
    assert c.post("/proactive/test", json={"send": False}, headers=bearer()).status_code == 200
    assert c.post("/proactive/test", json={"send": True}, headers=bearer()).status_code == 403
    assert scheduler.calls == [False]
    monkeypatch.setenv("VIOLET_ALLOW_MANUAL_PROACTIVE_SEND", "1")
    assert c.post("/proactive/test", json={"send": True}, headers=bearer()).status_code == 200


def test_token_is_generated_per_install_when_env_missing(make_app, monkeypatch, tmp_path):
    monkeypatch.delenv("VIOLET_ADMIN_TOKEN", raising=False)
    from wx_ilink_py.auth import resolve_admin_token
    t1, src1 = resolve_admin_token(tmp_path / "a")
    t2, _ = resolve_admin_token(tmp_path / "b")
    again, _ = resolve_admin_token(tmp_path / "a")
    assert t1 != t2 and t1 == again and len(t1) >= 32
    assert src1.endswith("admin_token")
    app, _, _ = make_app(admin_token=None, data_dir=tmp_path / "a")
    assert app.test_client().get("/api/memories", headers=bearer(t1)).status_code == 200


def test_no_token_literal_in_source():
    root = os.path.join(os.path.dirname(__file__), "..", "wx_ilink_py")
    for name in ("auth.py", "routes.py"):
        text = open(os.path.join(root, name), encoding="utf-8").read()
        assert TOKEN not in text


# --- main.py wiring smoke test -------------------------------------------------------

def test_main_module_wiring(monkeypatch, tmp_path, violetdb):
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "main-data"))
    import importlib
    import main
    main = importlib.reload(main)
    assert main._dashboard_bind() == ("127.0.0.1", 7100)
    c = main.app.test_client()
    assert c.get("/api/health", headers={"Accept": "application/json"}).status_code == 401
    health = c.get("/api/health", headers=bearer()).get_json()
    assert health["poll_thread_alive"] is False            # nothing was started by importing
    for name in ("Eve_Message", "Eve_File", "mainLoop", "_send_pipeline_messages", "reload_debounce_config"):
        assert callable(getattr(main, name))
    assert main.proactive_scheduler._pipeline_lock is main.runtime.pipeline_lock


def test_real_wechat_client_cannot_reach_network_in_tests(tmp_path):
    from wx_ilink_py import ILinkClient
    data = tmp_path / "wx"
    data.mkdir()
    (data / "credentials.json").write_text(json.dumps({
        "token": "fake", "base_url": "https://ilinkai.weixin.qq.com", "account_id": "fake", "user_id": "fake-user",
    }), encoding="utf-8")
    client = ILinkClient(data_dir=data, auto_login=False)
    with pytest.raises(Exception) as info:
        client.send_text("this must never be delivered")
    assert "attempted" in str(info.value) or "NetworkBlocked" in repr(info.value) or "resolve" in str(info.value)
