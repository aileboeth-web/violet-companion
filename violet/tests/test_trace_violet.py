"""Violet 0.7.5: the troubleshooting log (ai/trace.py) -- off by default, one line per step, content as the setting
allows, written even when the log level only keeps problems; the settings and the log page's filter."""

from __future__ import annotations

import logging
import os

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}


def test_off_by_default_and_one_readable_line_when_on(monkeypatch, caplog):
    from ai import trace
    monkeypatch.delenv("VIOLET_TRACE", raising=False)
    with caplog.at_level(logging.INFO, logger="violet.trace"):
        trace.step("收到消息", 内容="你好")
    assert not caplog.records and trace.enabled() is False
    monkeypatch.setenv("VIOLET_TRACE", "1")
    with caplog.at_level(logging.INFO, logger="violet.trace"):
        trace.step("工具计划", 方式="chat", 工具=["violet-web__web_search"], 原因=None, 说明={"a": 1})
    assert caplog.records[-1].getMessage() == '工具计划 · 方式=chat · 工具=[violet-web__web_search] · 说明={"a": 1}'


def test_content_modes(monkeypatch):
    from ai import trace
    long = "天" * 250 + "\n第二行"
    monkeypatch.setenv("VIOLET_TRACE_CONTENT", "short")
    short = trace.text(long)
    assert short.startswith("天" * 200) and "共 254 字" in short and "\n" not in short
    monkeypatch.setenv("VIOLET_TRACE_CONTENT", "full")
    assert trace.text(long).endswith("⏎第二行")
    monkeypatch.setenv("VIOLET_TRACE_CONTENT", "none")
    assert trace.text("秘密的话") == "<4 字，未记录内容>"
    assert "天气" not in trace.text({"query": "天气"})                       # tool arguments too


def test_tool_outcomes():
    from ai.trace import tool_outcome
    assert tool_outcome('{"ok": true, "results": []}') == ("ok", None)
    assert tool_outcome('{"ok": false, "error_code": "WEB_TURN_LIMIT", "message": "本轮联网次数已达上限"}') == \
        ("WEB_TURN_LIMIT", "本轮联网次数已达上限")
    assert tool_outcome("plain text") == ("ok", None)


def test_written_even_when_the_log_level_keeps_only_problems(monkeypatch):
    from ai import trace
    monkeypatch.setenv("VIOLET_TRACE", "1")
    root = logging.getLogger()
    old = root.level
    root.setLevel(logging.WARNING)
    try:
        assert logging.getLogger("violet.trace").isEnabledFor(logging.INFO)
    finally:
        root.setLevel(old)


def test_settings_group_and_log_page_filter(violetdb, tmp_path, monkeypatch):
    from ai.control_config import BY_KEY, GROUPS
    assert BY_KEY["VIOLET_TRACE"].group == "logs" and BY_KEY["VIOLET_TRACE"].default is False
    assert BY_KEY["VIOLET_TRACE_CONTENT"].default == "short" and BY_KEY["VIOLET_LOG_LEVEL"].group == "logs"
    assert GROUPS["logs"] == "日志与排查"
    log = tmp_path / "main.log"
    log.write_text("2026-10-28 21:13:00,000 INFO [violet.trace] [turn t-1] 收到消息 · 内容=今天杭州什么天气\n"
                   "2026-10-28 21:13:00,100 INFO [violet.runtime] [turn t-1] something else\n", encoding="utf-8")
    monkeypatch.setenv("VIOLET_LOG_FILE", str(log))
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    from wx_ilink_py import ILinkClient, create_app
    app = create_app(client=ILinkClient(data_dir=tmp_path / "data", auto_login=False), violetdb=violetdb,
                     proactive_scheduler=None, all_agents=[], admin_token=TOKEN)
    app.testing = True
    c = app.test_client()
    assert c.post("/auth/login", data={"token": TOKEN}, headers=SAME).status_code == 302
    monkeypatch.delenv("VIOLET_TRACE", raising=False)
    d = c.get("/api/control/logs?logger=violet.trace").get_json()["data"]
    assert d["trace_enabled"] is False and [e["message"] for e in d["entries"]] == ["收到消息 · 内容=今天杭州什么天气"]
    html = (__import__("pathlib").Path(__file__).resolve().parent.parent / "wx_ilink_py" / "templates" / "control.html"
            ).read_text(encoding="utf-8")
    assert 'id="log-trace"' in html
