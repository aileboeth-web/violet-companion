"""2026-11-03 手机近况 (ai/phone_state.py): read by the code, one line, to the proactive run and (switch) replies."""

from __future__ import annotations

import json
import threading
import time

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from tests.mcp_test_server import free_port

SCREEN = {"screenOn": True, "keyguardLocked": False, "stateDurationSeconds": 3000}
FG = {"state": "current", "app": "哔哩哔哩", "package": "tv.danmaku.bili", "age_seconds": 1500}
BATTERY = {"percent": 43, "charging": False}
USAGE = {"apps": [{"appName": "哔哩哔哩", "foregroundMs": 6_600_000}, {"appName": "微信", "foregroundMs": 3_600_000},
                  {"appName": "王者荣耀", "foregroundMs": 2_400_000}], "totalForegroundMs": 15_120_000}


def test_compose_one_line_and_leave_out_what_is_missing():
    from ai.phone_state import compose
    text = compose({"get_screen_state": SCREEN, "get_foreground_app": FG, "battery": BATTERY,
                    "get_app_usage_stats": USAGE}, "14:05")
    first = text.split("\n")[0]
    assert first == ("【主人手机此刻（14:05 读取）】屏幕亮着、已解锁；正在用「哔哩哔哩」（约 25 分钟）；电量 43%，没在充电；"
                     "今天用手机约 4 小时 12 分钟：哔哩哔哩 1 小时 50 分钟、微信 1 小时、王者荣耀 40 分钟")
    assert "不要让对方觉得被监视" in text
    off = compose({"get_screen_state": {"screenOn": False, "stateDurationSeconds": 7800}}, "03:10").split("\n")[0]
    assert off == "【主人手机此刻（03:10 读取）】屏幕熄灭了约 2 小时 10 分钟"
    assert compose({}, "03:10") == ""
    # usage access missing on the phone: no app, no times -- nothing guessed
    assert "正在用" not in compose({"get_screen_state": SCREEN, "get_foreground_app": {"state": "permission_denied"}}, "1")


def test_the_life_state_tools_of_mcp_1_4_1():
    from datetime import datetime, timedelta, timezone
    from ai.phone_state import compose
    tz = timezone(timedelta(hours=8))
    now = datetime(2026, 11, 3, 22, 0, tzinfo=tz)

    def stamp(dt):
        return {"epochMs": 0, "iso": dt.isoformat()}
    data = {
        "now_playing": {"playing": True, "sessions": [{"app": "网易云音乐", "title": "晴天", "artist": "周杰伦", "playing": True}]},
        "audio_output": {"headphonesOrBluetooth": True},
        "network_state": {"type": "wifi", "place": "家"},
        "step_count": {"stepsToday": 6234},
        "next_alarm": {"hasAlarm": True, "time": stamp(now + timedelta(hours=9, minutes=30))},
        "calendar_events": {"events": [
            {"title": "已经结束的会", "allDay": False, "start": stamp(now - timedelta(hours=3)), "end": stamp(now - timedelta(hours=2))},
            {"title": "组会", "allDay": False, "start": stamp(now + timedelta(hours=17)), "end": stamp(now + timedelta(hours=18))},
            {"title": "妈妈生日", "allDay": True, "start": stamp(datetime(2026, 11, 4, tzinfo=tz)),
             "end": stamp(datetime(2026, 11, 5, tzinfo=tz))}]},
    }
    line = compose(data, "22:00", now).split("\n")[0]
    assert line == ("【主人手机此刻（22:00 读取）】在听「晴天」（周杰伦）；戴着耳机 / 连着蓝牙音频；连着「家」的 Wi-Fi；"
                    "今天走了 6234 步；下一个闹钟 明天 07:30；日程：明天 15:00 组会、明天全天 妈妈生日")
    # nothing playing, cellular, an alarm days away: no guessing, no far-away alarm
    quiet = compose({"now_playing": {"playing": False, "sessions": []}, "network_state": {"type": "cellular"},
                     "next_alarm": {"hasAlarm": True, "time": stamp(now + timedelta(days=3))}}, "22:00", now)
    assert quiet.split("\n")[0] == "【主人手机此刻（22:00 读取）】用的是流量（没连 Wi-Fi）"


class PhoneServer:
    def __init__(self):
        server = FastMCP("phone", stateless_http=True)
        self.calls = []
        ro = ToolAnnotations(readOnlyHint=True)

        def tool(name, value):
            def fn(**kwargs) -> str:
                self.calls.append(name)
                return json.dumps(value, ensure_ascii=False)
            fn.__name__ = name
            server.tool(name=name, annotations=ro)(lambda: fn())

        tool("get_screen_state", SCREEN)
        tool("get_foreground_app", FG)
        tool("battery", BATTERY)

        @server.tool(annotations=ro)
        def get_app_usage_stats(range: str = "today", limit: int = 20) -> str:    # noqa: A002 -- the phone's name
            self.calls.append(f"get_app_usage_stats:{range}:{limit}")
            return json.dumps(USAGE, ensure_ascii=False)

        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self._server = uvicorn.Server(uvicorn.Config(server.streamable_http_app(), host="127.0.0.1", port=self.port,
                                                     log_level="warning", lifespan="on"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            assert time.monotonic() < deadline
            time.sleep(0.05)

    def stop(self):
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture()
def phone(monkeypatch):
    server = PhoneServer()
    monkeypatch.setenv("VIOLET_PHONE_CONTEXT", "1")
    monkeypatch.setenv("MCP_CONNECT_TIMEOUT_SECONDS", "3")
    import ai.chat
    from ai import mcp as mcp_module, phone_state
    mcp_module.reset_registry()
    monkeypatch.setattr(ai.chat, "get_mcp_servers", lambda: [{"name": "violet-mcp", "transport": "streamable_http",
                                                               "url": server.url}])
    phone_state.clear_cache()
    yield server
    phone_state.clear_cache()
    server.stop()


def test_the_line_comes_from_the_phone_and_is_cached(phone):
    from ai import phone_state
    text = phone_state.line("proactive")
    assert "正在用「哔哩哔哩」" in text and "电量 43%" in text and "今天用手机约 4 小时 12 分钟" in text
    assert "get_app_usage_stats:today:5" in phone.calls
    n = len(phone.calls)
    assert phone_state.line("proactive") == text and len(phone.calls) == n          # two minutes: no second read


def test_switches_and_an_offline_phone(phone, monkeypatch):
    from ai import phone_state
    assert phone_state.line("chat") == ""                                           # replies: off by default
    monkeypatch.setenv("VIOLET_PHONE_CONTEXT", "0")
    assert phone_state.line("proactive") == "" and phone.calls == []
    monkeypatch.setenv("VIOLET_PHONE_CONTEXT", "1")
    phone.stop()
    phone_state.clear_cache()
    assert phone_state.line("proactive") == ""                                      # unreachable: no line, no error


def _event(seq, kind, at, **detail):
    return {"seq": seq, "type": kind, "at": {"epochMs": 0, "iso": at.isoformat()}, "detail": detail}


def test_phone_events_moments_and_recent_lines(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from ai import phone_events
    phone_events.reset()
    tz = timezone(timedelta(hours=8))
    now = datetime(2026, 11, 4, 7, 15, tzinfo=tz)
    phone_events.take([
        _event(1, "screen_off", now - timedelta(hours=8)),                                      # too old, too dull
        _event(2, "unlock", now - timedelta(minutes=3), screenOffMinutes=430),
        _event(3, "charger_disconnected", now - timedelta(minutes=1)),
    ], now)
    assert phone_events.recent_text(now) == "最近的动静：07:12 解锁手机（之前熄屏约 7 小时）、07:14 拔了充电器"
    moment = phone_events.pending_moment(now)
    assert moment.startswith("主人刚起床：熄屏约 7 小时后，07:12 第一次解锁手机") and "不要说你看到了手机" in moment
    assert phone_events.pending_moment(now + timedelta(minutes=30)) is None                     # 20 minutes only
    phone_events.reset()
    # an afternoon unlock or a short screen-off is no moment; arriving home is
    phone_events.take([_event(4, "unlock", now.replace(hour=15), screenOffMinutes=500)], now.replace(hour=15, minute=1))
    assert phone_events.pending_moment(now.replace(hour=15, minute=1)) is None
    phone_events.take([_event(5, "place_arrived", now.replace(hour=19), place="家")], now.replace(hour=19, minute=1))
    assert "刚到「家」" in phone_events.pending_moment(now.replace(hour=19, minute=1))
    phone_events.reset()


def test_phone_events_poll_uses_the_cursor_and_survives_a_phone_restart(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from ai import phone_events, phone_state
    phone_events.reset()
    monkeypatch.setenv("VIOLET_PHONE_EVENTS", "1")
    now = datetime(2026, 11, 4, 7, 15, tzinfo=timezone(timedelta(hours=8)))
    asked = []
    answers = {"A": [_event(1, "charger_connected", now), _event(2, "unlock", now, screenOffMinutes=5)],
               "B": [_event(1, "headphones_connected", now)]}
    phone = {"epoch": "A"}

    def fake_read(calls):
        after = calls["phone_events"]["after"]
        asked.append((phone["epoch"], after))
        events = [e for e in answers[phone["epoch"]] if e["seq"] > after]
        return {"phone_events": {"epoch": phone["epoch"], "latestSeq": len(answers[phone["epoch"]]), "events": events}}
    monkeypatch.setattr(phone_state, "_read", fake_read)
    assert phone_events.poll(now, force=True) == 2
    assert phone_events.poll(now, force=True) == 0 and asked[-1] == ("A", 2)              # only what is new
    phone["epoch"] = "B"                                                                    # the phone restarted
    assert phone_events.poll(now, force=True) == 1 and asked[-1] == ("B", 0)
    assert "戴上耳机" in phone_events.recent_text(now)
    monkeypatch.setattr(phone_state, "_read", lambda calls: {})                             # no such tool
    assert phone_events.poll(now, force=True) == 0 and phone_events.poll(now) == 0          # paused, not retried
    phone_events.reset()


def test_a_moment_makes_the_decision_run_now(monkeypatch, violetdb, tmp_path):
    import json as _json
    from datetime import timedelta
    from tests.fakes import far_quiet_hours
    from ai import phone_events
    from ai.proactive_scheduler import ProactiveScheduler, now_china
    cfg = tmp_path / "p.json"
    cfg.write_text(_json.dumps({"enabled": True, "quiet_hours": far_quiet_hours(), "daily_message_limit": 3,
                                "min_minutes_after_user_message": 0, "min_minutes_between_proactive_messages": 0}),
                   encoding="utf-8")
    monkeypatch.setenv("VIOLET_PHONE_EVENTS", "1")
    s = ProactiveScheduler(None, None, None, send_messages=lambda m, e=None: None, config_path=cfg)
    current = now_china()
    violetdb.add_user_message("text", "晚安")
    s._set_next_proactive_at(current + timedelta(hours=3))
    assert s._is_due(current) == (False, "not due yet")
    assert s._is_due(current, ignore_next_at=True)[0] is True
    phone_events.reset()
    phone_events._state["moment"] = (current, "主人刚起床")
    ran = []
    import ai.proactive_scheduler as ps
    monkeypatch.setattr(ps, "run_proactive_pipeline", lambda event_context, **kw: ran.append(event_context) or {})
    monkeypatch.setattr(s, "_handle_pipeline_result", lambda *a, **k: None)
    s._tick_locked()
    assert ran and ran[0]["phone_moment"] == "主人刚起床" and phone_events.pending_moment(current) is None
    phone_events.reset()


def test_the_proactive_run_and_a_reply_carry_the_line(monkeypatch):
    from ai import phone_state
    from ai.proactive_scheduler import ProactiveScheduler
    monkeypatch.setattr(phone_state, "line", lambda kind="proactive": f"【主人手机此刻】{kind}")
    ctx = {}
    ProactiveScheduler._add_phone(ctx)
    assert ctx["phone_now"] == "【主人手机此刻】proactive"
    from ai.chat import _phone_line_for_chat
    assert _phone_line_for_chat() == "【主人手机此刻】chat"
    from tests.fakes import FakeLLM, default_responders, install_fake_llm
    llm = install_fake_llm(monkeypatch, FakeLLM(default_responders("嗯")))
    from ai.agents.chat_agent import ChatAgent
    ChatAgent().run("早", call_context={"phone_note": "【主人手机此刻】测试"})
    sent = [m.get("content") for c in llm.calls for m in c["messages"] if isinstance(m, dict)]
    assert "【主人手机此刻】测试" in sent
