"""2026-11-03 节日祝福 (ai/holidays.py, ProactiveScheduler._holiday_greeting)."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from tests.fakes import far_quiet_hours


def test_lunar_dates_match_the_calendar():
    from ai.holidays import lunar_to_solar
    assert [str(lunar_to_solar(y, 1, 1)) for y in (2026, 2027, 2028, 2031, 2033)] == \
        ["2026-02-17", "2027-02-06", "2028-01-26", "2031-01-23", "2033-01-31"]
    assert str(lunar_to_solar(2026, 7, 7)) == "2026-08-19" and str(lunar_to_solar(2026, 8, 15)) == "2026-09-25"
    assert str(lunar_to_solar(2028, 8, 15)) == "2028-10-03"          # 2028 has a leap 5th month
    assert lunar_to_solar(2050, 1, 1) is None


def test_which_holidays_each_relationship_gets():
    from ai.holidays import for_owner, on_day
    qixi, spring, eve = date(2026, 8, 19), date(2026, 2, 17), date(2026, 2, 16)
    assert [h.name for h in for_owner(qixi, "partner", None, [])] == ["七夕"]
    assert for_owner(qixi, "friend", None, []) == [] and for_owner(qixi, "close", None, []) == []
    assert [h.name for h in for_owner(spring, "friend", None, [])] == ["春节"]
    assert [h.name for h in on_day(eve)] == ["除夕"]
    assert [h.name for h in for_owner(date(2026, 5, 10), "daughter", "妈妈", [])] == ["母亲节"]
    assert for_owner(date(2026, 5, 10), "daughter", "爸爸", []) == []
    assert [h.name for h in for_owner(date(2026, 6, 21), "daughter", None, [])] == ["父亲节"]
    assert [h.name for h in for_owner(date(2026, 10, 24), "creator", None, [])] == ["程序员节"]
    assert for_owner(date(2026, 10, 24), "partner", None, []) == []
    # optional ones only when the owner cares; no 清明 at all
    assert for_owner(date(2026, 10, 31), "partner", None, ["喜欢看动漫"]) == []
    assert [h.name for h in for_owner(date(2026, 10, 31), "friend", None, ["每年万圣节都去 cos"])] == ["万圣节"]
    assert all(h.name != "清明" for d in range(1, 31) for h in on_day(date(2026, 4, d)))
    # the owner's birthday / a partner's anniversary from what she knows
    texts = ["identity.background 生日是3月5日", "我们在一起的纪念日是 5.20"]
    assert [h.name for h in for_owner(date(2027, 3, 5), "friend", None, texts)] == ["主人的生日"]
    assert [h.name for h in for_owner(date(2027, 5, 20), "partner", None, texts)] == ["你们的纪念日", "520"]
    assert for_owner(date(2027, 5, 20), "friend", None, texts) == []


class FakeChat:
    def __init__(self, text="七夕快乐呀，今天想你了"):
        self.text, self.calls = text, []

    def run(self, message, call_context=None):
        self.calls.append(call_context)
        return {"content": self.text}


@pytest.fixture()
def sched(monkeypatch, violetdb, tmp_path):
    cfg = tmp_path / "proactive.json"
    cfg.write_text(json.dumps({"enabled": True, "quiet_hours": far_quiet_hours(), "daily_message_limit": 1}), encoding="utf-8")
    monkeypatch.setenv("VIOLET_HOLIDAY_GREETING", "1")
    from ai import holidays
    from ai.proactive_scheduler import ProactiveScheduler, now_china
    monkeypatch.setattr(holidays, "today_for_channel", lambda db, day, channel: (
        [holidays.Holiday("qixi", "七夕", "partner")], {"kind": "partner", "label": "恋人", "call": None}))
    sent = []
    chat = FakeChat()
    s = ProactiveScheduler(None, chat, None, send_messages=lambda m, e=None: sent.append((m, e)) or {"sent_messages": m},
                           config_path=cfg)
    return type("S", (), dict(s=s, sent=sent, chat=chat, now=now_china(), db=violetdb))


def test_one_greeting_per_holiday_outside_the_daily_cap(sched):
    assert sched.s._holiday_greeting(sched.now) is True
    assert [m["content"] for m in sched.sent[0][0]] == ["七夕快乐呀，今天想你了"]
    assert "今天是七夕" in sched.chat.calls[0]["proactive_note"]
    assert sched.s._holiday_greeting(sched.now) is False and len(sched.sent) == 1      # once
    assert sched.s._today_sent_count(sched.now) == 0                                     # not in the daily cap
    ev = sched.db.fetch_one("SELECT event_type, status FROM proactive_events ORDER BY id DESC LIMIT 1")
    assert (ev["event_type"], ev["status"]) == ("holiday_greeting", "sent")


def test_no_separate_greeting_when_the_owner_talked_first_or_the_switch_is_off(sched, monkeypatch):
    sched.db.add_user_message("text", "早")
    assert sched.s._holiday_greeting(sched.now) is False and sched.sent == []
    assert sched.db.get_proactive_setting("holiday_greeted:" + sched.now.date().isoformat()) == "chat"
    sched.db.set_proactive_setting("holiday_greeted:" + sched.now.date().isoformat(), "")
    monkeypatch.setenv("VIOLET_HOLIDAY_GREETING", "0")
    assert sched.s._holiday_greeting(sched.now) is False and sched.chat.calls == []


def test_a_declined_greeting_is_tried_twice_then_dropped(sched):
    sched.chat.text = "[NO_REPLY]"
    assert sched.s._holiday_greeting(sched.now) is False
    assert sched.s._holiday_greeting(sched.now) is False
    assert sched.s._holiday_greeting(sched.now) is False
    assert len(sched.chat.calls) == 2 and sched.sent == []


def test_the_chat_context_names_todays_holiday(sched, monkeypatch):
    from ai.context_engine import ContextEngine
    line = ContextEngine(sched.db)._holiday_line()
    assert line.startswith("今天是七夕（你们的关系：恋人）") and "可以自然地说一句" in line
    sched.s._holiday_greeting(sched.now)
    assert "今天已经祝福过了" in ContextEngine(sched.db)._holiday_line()
    monkeypatch.setenv("VIOLET_HOLIDAY_GREETING", "0")
    assert ContextEngine(sched.db)._holiday_line() == ""
