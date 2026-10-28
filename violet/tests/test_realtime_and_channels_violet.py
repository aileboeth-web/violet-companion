"""Violet 0.7.5: WeChat not logged in while another channel works is not a problem; the overview shows every channel
and the messages of all channels (user, with QQ bound: "这里好像还是在优待微信")."""

from __future__ import annotations

import os

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}


class _Manager:
    def __init__(self, rows):
        self.rows = rows

    def describe(self):
        return self.rows


def _row(cid, ctype, state, bound=1):
    return {"id": cid, "type": ctype, "label": {"wechat": "微信", "qq": "QQ"}[cid],
            "status": {"state": state, "detail": ""}, "peers": {"bound": bound, "pending": 0, "blocked": 0}}


def test_unused_wechat_is_not_a_problem_when_another_channel_works():
    from ai.diagnostics import _channel_findings
    runtime = type("R", (), {"channels": _Manager([_row("wechat", "wechat_ilink", "needs_login"),
                                                   _row("qq", "qqbot", "connected")])})()
    findings = _channel_findings(runtime)
    assert all(f["level"] == "ok" for f in findings), findings
    assert any("没有在用" in f["message"] for f in findings)
    alone = type("R", (), {"channels": _Manager([_row("wechat", "wechat_ilink", "needs_login")])})()
    assert any(f["level"] == "error" for f in _channel_findings(alone))      # nothing works: still a problem


def test_overview_lists_channels_and_counts_all_messages(violetdb, tmp_path, monkeypatch):
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    from wx_ilink_py import ILinkClient, create_app
    runtime = type("R", (), {"channels": _Manager([_row("wechat", "wechat_ilink", "needs_login"),
                                                   _row("qq", "qqbot", "connected")]),
                             "health": lambda self: {}})()
    app = create_app(client=ILinkClient(data_dir=tmp_path / "data", auto_login=False), violetdb=violetdb,
                     proactive_scheduler=None, all_agents=[], admin_token=TOKEN, runtime=runtime)
    app.testing = True
    for role, direction in (("user", "incoming"), ("user", "incoming"), ("assistant", "outgoing")):
        violetdb.execute("INSERT INTO messages (role, direction, message_type, content, created_at) VALUES (?, ?, 'text', 'x', ?)",
                         (role, direction, violetdb.now()))
    c = app.test_client()
    assert c.post("/auth/login", data={"token": TOKEN}, headers=SAME).status_code == 302
    d = c.get("/api/control/overview").get_json()["data"]
    assert {"label": "QQ", "type": "qqbot", "state": "connected"} in d["channels"]
    assert d["message_totals"] == {"received": 2, "sent": 1}
