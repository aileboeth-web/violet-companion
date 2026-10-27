"""Violet stage 3: WeChat QR login inside the channels page, the run log page, persona and life as a Control Center
page. Temp data dir, a fake WeChat client state, a hand-written log file. No network."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}
ROOT = Path(__file__).resolve().parent.parent


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
def made(env_guard, violetdb):
    from wx_ilink_py import ILinkClient, create_app
    client = ILinkClient(data_dir=env_guard, auto_login=False)
    app = create_app(client=client, violetdb=violetdb, proactive_scheduler=None, all_agents=[], admin_token=TOKEN)
    app.testing = True
    return app, client


def _admin(app):
    c = app.test_client()
    assert c.post("/auth/login", data={"token": TOKEN}, headers=SAME).status_code == 302
    return c


# ------------------------------------------------------------------------------------------------ WeChat login

def test_wechat_not_logged_in_then_logging_in_with_a_qr_code(made, monkeypatch):
    app, client = made
    admin = _admin(app)
    w = admin.get("/api/control/wechat").get_json()["data"]
    assert w["logged_in"] is False and w["logging_in"] is False and w["qr"] is None
    started = []
    monkeypatch.setattr(client, "start_login_async", lambda force_refresh=False: started.append(force_refresh))
    assert admin.post("/api/control/wechat/login", json={}, headers=SAME).get_json()["data"]["started"]
    assert started == [False]
    with client._lock:
        client.state.login_mode = "logging_in"
        client.state.qr_state.qrcode = "qr-token"
        client.state.qr_state.qrcode_img_content = "https://liteapp.weixin.qq.com/q/abc?qrcode=qr-token"
        client.state.qr_state.status = "scaned"
    w = admin.get("/api/control/wechat").get_json()["data"]
    assert w["logging_in"] and w["qr"]["status_text"] == "已扫码，请在手机上确认"
    assert w["qr"]["image"].startswith("data:image/svg+xml;base64,")      # drawn on the server without Pillow
    import base64
    assert b"<svg" in base64.b64decode(w["qr"]["image"].split(",", 1)[1])
    assert w["qr"]["url"].startswith("https://")


def test_wechat_logged_in_shows_only_a_masked_account_and_logout_needs_confirmation(made, monkeypatch):
    app, client = made
    admin = _admin(app)
    with client._lock:
        client.state.logged_in = True
        client.state.account_id = "wxid_1234567890abcdef"
        client.state.login_mode = "saved_credentials"
    w = admin.get("/api/control/wechat").get_json()["data"]
    assert w["logged_in"] and w["account"] == "wxid…cdef" and w["qr"] is None
    cleared = []
    monkeypatch.setattr(client, "clear_credentials", lambda: cleared.append(1))
    assert admin.post("/api/control/wechat/logout", json={}, headers=SAME).status_code == 400
    assert admin.post("/api/control/wechat/logout", json={"confirm": True}, headers=SAME).status_code == 200
    assert cleared == [1]


def test_the_new_endpoints_need_the_admin(made):
    app, _client = made
    c = app.test_client()
    for path in ("/api/control/wechat", "/api/control/logs", "/api/control/logs/download"):
        assert c.get(path, headers={"Accept": "application/json"}).status_code == 401, path
    assert c.post("/api/control/wechat/login", json={}, headers=SAME).status_code == 401


# ------------------------------------------------------------------------------------------------ run log

LOG = """2026-10-27 09:00:00,001 INFO [violet.main] started
2026-10-27 09:00:01,002 INFO [violet.channels] [turn t-abc123] message received
2026-10-27 09:00:02,003 WARNING [violet.mcp] [turn t-abc123] MCP server phone unavailable at https://relay.example/mcp?key=s3cr3tvalue
2026-10-27 09:00:03,004 ERROR [violet.agents] [turn t-abc123] model call failed with Authorization: Bearer abcdefghijklmnop
Traceback (most recent call last):
  File "ai/agents/base_agent.py", line 10, in call
    raise RuntimeError("boom sk-live-THISISTHEREALKEY123")
RuntimeError: boom sk-live-THISISTHEREALKEY123
2026-10-27 09:00:04,005 DEBUG [violet.web] cache hit
2026-10-27 09:00:05,006 INFO [violet.channels] telegram bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd connected
"""


@pytest.fixture()
def logfile(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "main.log"
    path.parent.mkdir()
    path.write_text(LOG, encoding="utf-8")
    monkeypatch.setenv("VIOLET_LOG_FILE", str(path))
    monkeypatch.setenv("LLM_API_KEY", "sk-live-THISISTHEREALKEY123")
    return path


def test_records_newest_first_with_tracebacks_grouped(logfile):
    from ai.log_viewer import read
    d = read(level="DEBUG")
    assert d["available"] and [e["level"] for e in d["entries"]] == ["INFO", "DEBUG", "ERROR", "WARNING", "INFO", "INFO"]
    error = d["entries"][2]
    assert error["turn"] == "t-abc123" and error["logger"] == "violet.agents" and len(error["detail"]) == 4
    assert d["counts"] == {"DEBUG": 1, "INFO": 3, "WARNING": 1, "ERROR": 1, "CRITICAL": 0}


def test_every_secret_is_removed(logfile):
    from ai.log_viewer import download_text, read
    text = repr(read(level="DEBUG")) + download_text()
    for secret in ("s3cr3tvalue", "abcdefghijklmnop", "THISISTHEREALKEY123", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"):
        assert secret not in text, secret
    assert "<redacted>" in text and "key=<redacted>" in text and "Bearer <redacted>" in text


def test_filters(logfile):
    from ai.log_viewer import read
    assert [e["level"] for e in read(level="WARNING")["entries"]] == ["ERROR", "WARNING"]
    assert [e["logger"] for e in read(level="DEBUG", turn="t-abc123")["entries"]] == \
        ["violet.agents", "violet.mcp", "violet.channels"]
    assert len(read(level="INFO", query="RuntimeError")["entries"]) == 1           # found in the traceback
    assert read(level="INFO", query="redacted")["entries"]                          # searches the redacted text
    assert read(level="INFO", query="s3cr3tvalue")["entries"] == []                 # never the secret itself
    assert len(read(level="DEBUG", limit=2)["entries"]) == 2 and read(level="DEBUG", limit=2)["truncated"]


def test_only_the_newest_part_is_read_including_the_rotated_file(logfile, monkeypatch):
    import ai.log_viewer as lv
    rotated = logfile.with_name("main.log.1")
    rotated.write_text("2026-10-26 23:59:59,000 INFO [violet.main] from yesterday\n", encoding="utf-8")
    assert lv.read(level="DEBUG")["entries"][-1]["message"] == "from yesterday"
    monkeypatch.setattr(lv, "TAIL_BYTES", 200)
    d = lv.read(level="DEBUG")
    assert d["older_not_shown"] and d["window_bytes"] <= 200
    assert all(e["time"] for e in d["entries"])                                    # the cut first line is dropped


def test_no_log_file(monkeypatch, tmp_path):
    from ai.log_viewer import read
    monkeypatch.delenv("VIOLET_LOG_FILE", raising=False)
    assert read()["available"] is False and "VIOLET_LOG_FILE" in read()["reason"]
    monkeypatch.setenv("VIOLET_LOG_FILE", str(tmp_path / "none.log"))
    assert read()["available"] is False


def test_channel_and_stored_secrets_are_known(env_guard, logfile):
    from ai.channels.manager import ChannelStore
    from ai.control_config import ControlStore
    from ai.log_viewer import known_secrets
    ControlStore(env_guard).save({"LLM_API_KEY": "sk-stored-in-the-dashboard-01"}, "test")
    store = ChannelStore(env_guard)
    store._write(store.secrets_path, {"channels": {"tg": {"bot_token": "123:telegram-secret-token-xyz"}}}, secret=True)
    store._write(store.path, {"channels": {"tg": {"type": "telegram"}}})
    secrets = known_secrets(env_guard)
    assert "sk-stored-in-the-dashboard-01" in secrets and "123:telegram-secret-token-xyz" in secrets


def test_log_api_and_download(made, logfile):
    app, _client = made
    admin = _admin(app)
    d = admin.get("/api/control/logs?level=WARNING&turn=t-abc123").get_json()["data"]
    assert [e["level"] for e in d["entries"]] == ["ERROR", "WARNING"]
    dl = admin.get("/api/control/logs/download")
    assert dl.status_code == 200 and "attachment" in dl.headers["Content-Disposition"]
    assert "THISISTHEREALKEY123" not in dl.get_data(as_text=True) and "message received" in dl.get_data(as_text=True)


# ------------------------------------------------------------------------------------------------ pages

def test_persona_is_a_control_center_page_and_login_lands_there(made):
    app, _client = made
    admin = _admin(app)
    r = admin.get("/persona")
    assert r.status_code == 302 and r.headers["Location"].endswith("/control#persona")
    assert app.test_client().post("/auth/login", data={"token": TOKEN}, headers=SAME).headers["Location"] == "/control"


def test_the_page_has_the_new_tabs_and_scripts_build_no_html():
    html = (ROOT / "wx_ilink_py" / "templates" / "control.html").read_text(encoding="utf-8")
    for tab in ("persona", "logs"):
        assert f'data-tab="{tab}"' in html and f'id="tab-{tab}"' in html
    assert '<script src="/static/control_persona.js"></script>' in html and "/static/control_logs.js" in html
    assert 'href="/persona"' not in html
    for name in ("control_persona.js", "control_logs.js", "control_channels.js"):
        js = (ROOT / "wx_ilink_py" / "static" / name).read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert sink not in js, (name, sink)
    channels = (ROOT / "wx_ilink_py" / "static" / "control_channels.js").read_text(encoding="utf-8")
    assert "/api/control/wechat" in channels and 'href: "/"' not in channels
    assert not (ROOT / "wx_ilink_py" / "templates" / "persona.html").exists()
