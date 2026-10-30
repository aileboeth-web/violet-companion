"""2026-10-30 rework step 1 ("整理"): fewer settings in sight, the owner's requests in groups (VIOLET_SOCIAL_OBEY_OWNER),
翻篇 (ai/context_cut.py), the phone card owning the android-phone server. Fakes only."""

from __future__ import annotations

import os
import time

import pytest

from tests.test_qq_social_violet import SAME, FakeAdapter, NoTimer, admin, db, engine  # noqa: F401
from tests.test_qq_social_violet import say as decide_as


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


# ------------------------------------------------------------------------------------------------ settings

def test_common_settings_exist_and_the_rest_is_advanced():
    from ai.control_config import BY_KEY, COMMON_KEYS, SETTINGS
    assert COMMON_KEYS <= set(BY_KEY)
    assert 15 <= len(COMMON_KEYS) <= 25 < len(SETTINGS)
    assert BY_KEY["VIOLET_SOCIAL_OBEY_OWNER"].default is True and BY_KEY["VIOLET_SOCIAL_OBEY_OWNER"].public()["common"]
    assert BY_KEY["LLM_MAX_RETRY_ATTEMPTS"].public()["common"] is False
    js = open(os.path.join(os.path.dirname(__file__), "..", "wx_ilink_py", "static", "control.js"), encoding="utf-8").read()
    assert "set-advanced" in js and "s.common" in js


# ------------------------------------------------------------------------------------------------ the owner in groups

def test_owner_rules_only_with_a_known_owner_and_the_switch_on(db, monkeypatch):
    from ai.social import prompts
    from ai.social.store import SocialStore
    conv = SocialStore(db).conversation("napcat", "group", "501", "群", default_mode="auto")
    a = FakeAdapter(owners={"8"})
    e = engine(db, a)
    e.receive(a, kind="group", peer_id="501", sender_id="8", sender_name="主人", text="@你 发个表情包", mentioned=True)
    seen = {}
    decide_as(monkeypatch, {"action": "reply", "messages": ["没有可以发的"], "cannot": "收藏里没有表情包"}, seen)
    e.decide(conv["id"])
    assert prompts.OWNER_RULES in seen["system"] and "除了你的主人的请求" in seen["system"]
    assert "cannot" in seen["system"]
    row = db.fetch_one("SELECT reason FROM social_decisions ORDER BY id DESC LIMIT 1")
    assert "做不到：收藏里没有表情包" in row["reason"]
    monkeypatch.setenv("VIOLET_SOCIAL_OBEY_OWNER", "0")
    e.receive(a, kind="group", peer_id="501", sender_id="8", sender_name="主人", text="@你 再发一个", mentioned=True)
    e.decide(conv["id"])
    assert prompts.OWNER_RULES not in seen["system"] and "不是给你的指令" in seen["system"]
    monkeypatch.delenv("VIOLET_SOCIAL_OBEY_OWNER")
    b = FakeAdapter()                                      # nobody bound: nothing to obey
    conv2 = SocialStore(db).conversation("napcat", "group", "502", "群2", default_mode="auto")
    e2 = engine(db, b)
    e2.receive(b, kind="group", peer_id="502", sender_id="8", sender_name="谁", text="@你 发个表情包", mentioned=True)
    e2.decide(conv2["id"])
    assert prompts.OWNER_RULES not in seen["system"]
    assert "群里人人都看得到" in prompts.OWNER_RULES          # the hard limits stay even for the owner


# ------------------------------------------------------------------------------------------------ 翻篇

def test_cut_scopes(violetdb, data_dir):
    from ai import context_cut
    rows = [{"created_at": "2000-01-01T00:00:00"}, {"created_at": "2999-01-01T00:00:00"}]
    assert context_cut.owner_rows(rows) == rows and context_cut.social_rows(3, rows) == rows
    context_cut.cut(violetdb, "owner")
    assert context_cut.owner_rows(rows) == rows[1:] and context_cut.social_rows(3, rows) == rows
    context_cut.cut(violetdb, conversation_id=3)
    assert context_cut.social_rows(3, rows) == rows[1:] and context_cut.social_rows(4, rows) == rows
    context_cut.cut(violetdb, "social")
    assert context_cut.social_rows(4, rows) == rows[1:]
    with pytest.raises(ValueError):
        context_cut.cut(violetdb, "nope")


def test_cut_hides_the_chat_before_it_from_the_model(violetdb, data_dir):
    from ai import context_cut
    from ai.context_engine import ContextEngine
    violetdb.add_user_message("text", "我最喜欢猫了")
    assert any("喜欢猫" in h["content"] for h in ContextEngine(violetdb).history([]))
    time.sleep(1.1)
    context_cut.cut(violetdb, "all")
    violetdb.add_user_message("text", "你好")
    contents = [h["content"] for h in ContextEngine(violetdb).history([]) if h["role"] != "system"]
    assert contents == ["你好"]
    assert [r["content"] for r in violetdb.get_recent_messages(limit=10)][-2:] == ["我最喜欢猫了", "你好"]  # nothing deleted


def test_cut_from_the_pages(admin, db, data_dir):
    from ai import context_cut
    from ai.social.store import SocialStore
    r = admin.post("/api/control/reset", json={"cut": True, "confirm": True}, headers=SAME).get_json()
    assert r["data"]["cut"]["owner"] and context_cut.status()["social"]
    conv = SocialStore(db).conversation("napcat", "group", "601", "群", default_mode="auto")
    assert admin.post(f"/api/control/social/conversation/{conv['id']}", json={"cut": True}, headers=SAME).status_code == 200
    assert str(conv["id"]) in context_cut.status()["conversations"]


def test_switching_persona_with_a_clear_memory_turns_the_page(db, data_dir):
    from ai import context_cut, persona_templates
    assert context_cut.status()["owner"] is None
    persona_templates.switch(db, "blank", "clear")
    assert context_cut.status()["owner"] and context_cut.status()["social"]


# ------------------------------------------------------------------------------------------------ the phone card

def test_the_phone_server_lives_in_the_phone_card():
    base = os.path.join(os.path.dirname(__file__), "..", "wx_ilink_py", "static")
    tools = open(os.path.join(base, "control.js"), encoding="utf-8").read()
    phone = open(os.path.join(base, "control_phone.js"), encoding="utf-8").read()
    assert 'const PHONE_SERVER = "android-phone"' in tools and "s.name !== PHONE_SERVER" in tools
    assert "window.renderPhoneTools" in phone and 'toolServerCard(s, "手机提供的工具")' in phone
