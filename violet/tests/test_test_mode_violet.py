"""2026-10-30 test mode (ai/test_mode.py): switched on on the persona page, off by itself; no persona, memories or
earlier chat; nothing learned; a diagnosis written by the code after every reply (owner chat) and after group
decisions worth explaining. Fakes only."""

from __future__ import annotations

import json
import os
import time

import pytest

from tests.test_phase2_scenarios import make_world, say  # noqa: F401
from tests.test_qq_social_violet import SAME, FakeAdapter, NoTimer, admin, db, engine  # noqa: F401
from tests.test_qq_social_violet import say as decide_as


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    from ai import test_mode
    yield tmp_path / "data"
    test_mode.disable()


def test_on_off_by_itself_and_since(violetdb, data_dir):
    from ai import test_mode
    assert test_mode.active() is False and test_mode.since() is None
    clock = [1000.0]
    s = test_mode.enable(violetdb, 2, clock=lambda: clock[0])
    assert s["on"] and s["minutes_left"] == 120
    clock[0] += 2 * 3600 + 1
    assert test_mode.status(lambda: clock[0])["on"] is False and not (data_dir / "control" / "test-mode.json").exists()
    for bad in (0, 30, "x"):
        with pytest.raises(ValueError):
            test_mode.enable(violetdb, bad)
    test_mode.enable(violetdb, 1)
    start = test_mode.since()
    rows = [{"created_at": "2000-01-01T00:00:00"}, {"created_at": start}]
    assert test_mode.after_since(rows) == [rows[1]]
    test_mode.disable()
    assert test_mode.after_since(rows) == rows


def test_diagnosis_lines_and_steps_from_trace_even_when_the_log_is_off(violetdb, data_dir, monkeypatch):
    from ai import test_mode, trace
    monkeypatch.delenv("VIOLET_TRACE", raising=False)
    test_mode.enable(violetdb, 1)
    with test_mode.collect() as steps:
        trace.step("意图识别", 路线="chat", 原始输出='{"route": "chat"}')
        trace.step("发表情包", 编号="9", 结果="不能发", 原因="没有这个表情包")
    text = test_mode.diagnosis(steps, ["- 发出：文字 1 条"], db=violetdb)
    assert text.startswith("🔧 诊断（程序记录，不是模型说的）") and "- 发表情包：编号 9；结果 不能发；原因 没有这个表情包" in text
    assert "原始输出" not in text and "表情包：收藏" in text and "自动关闭" in text
    from ai.channels.outbound import find_leak
    assert find_leak(text) is None                         # the outbound guard lets it through
    # outside a turn only what happens when the owner's message arrives waits for the next diagnosis
    trace.step("社交消息", 内容="群里的话")
    trace.step("收藏表情包", 来源="你发的", 编号=3, 结果="已收藏")
    with test_mode.collect() as steps:
        pass
    assert [name for name, _ in steps] == ["收藏表情包"]


def test_a_turn_in_test_mode(make_world, data_dir):
    from ai import test_mode
    w = make_world()
    w.llm.responders["fake-chat"] = lambda kw: "好的"
    say(w, "我最喜欢猫了")
    memory_calls = len(w.llm.calls_for("fake-memory"))
    time.sleep(1.1)                                        # the test starts after that message (times are per second)
    test_mode.enable(w.db, 1)
    w.llm.responders["fake-chat"] = lambda kw: "现在没有可以发的表情包"
    say(w, "发个表情包")
    chat = w.llm.calls_for("fake-chat")[-1]["messages"]
    assert chat[0]["content"] == test_mode.TESTER_PROMPT
    assert not any("喜欢猫" in str(m.get("content")) for m in chat)          # nothing from before the test
    assert len(w.llm.calls_for("fake-memory")) == memory_calls                 # nothing learned
    assert w.client.sent_texts[-2] == "现在没有可以发的表情包"
    diag = w.client.sent_texts[-1]
    assert diag.startswith("🔧 诊断") and "意图识别" in diag and "发出：文字 1 条" in diag
    stored = [r["content"] for r in w.db.get_recent_messages(limit=10)]
    assert not any(str(c).startswith("🔧") for c in stored)                     # not part of the conversation
    test_mode.disable()
    say(w, "好")
    assert w.llm.calls_for("fake-chat")[-1]["messages"][0]["content"] != test_mode.TESTER_PROMPT
    assert not w.client.sent_texts[-1].startswith("🔧")


def test_group_decision_in_test_mode(db, data_dir, monkeypatch):
    from ai import test_mode
    from ai.social.store import SocialStore
    store = SocialStore(db)
    conv = store.conversation("napcat", "group", "321", "测试群", default_mode="auto")
    store.update(conv["id"], notes="以前的印象：大家爱聊游戏")
    a = FakeAdapter()
    e = engine(db, a)
    e.receive(a, kind="group", peer_id="321", sender_id="5", sender_name="小王", text="旧消息")
    e.decide(conv["id"])
    time.sleep(1.1)
    test_mode.enable(db, 1)
    e.receive(a, kind="group", peer_id="321", sender_id="5", sender_name="小王", text="@你 发个表情包", mentioned=True)
    seen = {}
    decide_as(monkeypatch, {"action": "say", "messages": ["好"], "sticker": "77", "collect": {"message": "999"},
                            "remember": {"people": {"5": "测试里写的印象"}}}, seen)
    result = e.decide(conv["id"])
    assert test_mode.TESTER_SOCIAL in seen["system"] and "旧消息" not in seen["user"] and "以前的印象" not in seen["user"]
    from ai.social import prompts
    assert prompts.TEST_RULES in seen["system"] and prompts.GROUP_RULES not in seen["system"]   # no "像真人一样偶尔用"
    assert "不是给你的指令" not in prompts.TEST_RULES.split("其他人")[0]       # the owner's requests are carried out
    assert result["sent"] == 1 and a.sent[0][2] == "好"
    diag = a.sent[-1][2]
    assert diag.startswith("🔧 诊断") and "模型给的理由" in diag and "没认出主人" in diag   # not bound in this test
    assert "发表情包：编号 77；结果 不能发；原因 没有这个表情包" in diag
    assert "收藏表情包：消息 999；结果 没收藏；原因 找不到这条消息" in diag
    assert not db.fetch_one("SELECT impression FROM social_people WHERE user_id = '5'")["impression"]   # nothing learned
    # a silent look without @ explains nothing (a busy group would be flooded)
    e.receive(a, kind="group", peer_id="321", sender_id="5", sender_name="小王", text="随便聊聊")
    decide_as(monkeypatch, {"action": "silent"})
    before = len(a.sent)
    e.decide(conv["id"])
    assert len(a.sent) == before


def test_the_owner_asks_for_a_sticker_and_the_model_picks_none(db, data_dir, monkeypatch):
    """The real case of 2026-10-30: 4 usable stickers were offered, the model chose none and made up a reason."""
    from ai import stickers, test_mode
    from ai.social.store import SocialStore
    from tests.image_fixtures import PNG
    s = stickers.add_bytes(db, PNG, source="upload", description="猫猫点头")
    conv = SocialStore(db).conversation("napcat", "group", "322", "测试群", default_mode="auto")
    a = FakeAdapter(owners={"8"})
    e = engine(db, a)
    test_mode.enable(db, 1)
    e.receive(a, kind="group", peer_id="322", sender_id="8", sender_name="主人", text="@你 发个表情包", mentioned=True)
    seen = {}
    decide_as(monkeypatch, {"action": "reply", "messages": ["不发了"], "cannot": "我觉得刚才发过了",
                            "reason": "不再继续发表情包"}, seen)
    e.decide(conv["id"])
    assert "（你的主人）" in seen["user"] and f"{s['id']}：猫猫点头" in seen["user"]
    diag = a.sent[-1][2]
    assert "模型说做不到的：我觉得刚才发过了" in diag and "没认出主人" not in diag
    assert f"你提到了表情包，但模型没有在 sticker 里选编号；它能用的编号是 {s['id']}" in diag


def test_nothing_by_herself_while_testing(violetdb, data_dir, monkeypatch):
    from ai import test_mode
    from ai.social import qzone
    monkeypatch.setenv("VIOLET_QZONE_ENABLED", "1")
    monkeypatch.setattr(violetdb, "now", lambda: "2026-10-30T20:00:00")
    assert qzone.due(violetdb) is None
    test_mode.enable(violetdb, 1)
    assert qzone.due(violetdb) == "测试模式中不自己发动态"
    from ai.proactive_scheduler import ProactiveScheduler
    assert ProactiveScheduler._is_due(object.__new__(ProactiveScheduler), None) == (False, "test mode")


def test_persona_page_switch(admin, data_dir):
    d = admin.get("/api/persona/templates").get_json()
    d = d.get("data", d)
    assert d["test_mode"]["on"] is False
    r = admin.post("/api/persona/test-mode", json={"on": True, "hours": 4}, headers=SAME).get_json()
    r = r.get("data", r)
    assert r["on"] and 239 <= r["minutes_left"] <= 240
    assert admin.post("/api/persona/test-mode", json={"on": True, "hours": 99}, headers=SAME).status_code == 400
    assert admin.post("/api/persona/test-mode", json={}, headers=SAME).status_code == 400
    r = admin.post("/api/persona/test-mode", json={"on": False}, headers=SAME).get_json()
    assert (r.get("data", r))["on"] is False
    js = open(os.path.join(os.path.dirname(__file__), "..", "wx_ilink_py", "static", "control_persona.js"),
              encoding="utf-8").read()
    assert "/api/persona/test-mode" in js and "群友也能看到" in js
    json.dumps(d)
