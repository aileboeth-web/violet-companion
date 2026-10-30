"""2026-10-30 rework step 3 (personas): prompts without a built-in identity, persona x relationship, the AI that knows
it is one (real experiences, its abilities, its model; no invented life), a persona / relationship per channel."""

from __future__ import annotations

from datetime import datetime

import pytest

from tests.test_qq_social_violet import SAME, admin, db  # noqa: F401


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def test_the_default_prompts_no_longer_fix_who_she_is():
    from ai.agents.chat_agent import ChatAgent
    from ai.agents.proactive_agent import ProactiveAgent
    from ai.settings import PROJECT_ROOT
    chat = (PROJECT_ROOT / ChatAgent.default_prompt_file).read_text(encoding="utf-8")
    proactive = (PROJECT_ROOT / ProactiveAgent.default_prompt_file).read_text(encoding="utf-8")
    assert "女孩" not in chat and "撒娇" not in chat and "你们的关系" in chat and "你自己" in chat
    assert "女友" not in proactive and "你们的关系" in proactive


def test_relationship_default_set_and_kept_on_a_switch(db, data_dir):
    from ai import persona_templates as pt
    assert pt.relationship() == {"kind": "close", "label": "亲近（默认）", "call": ""}
    r = pt.set_relationship("daughter")
    assert r["kind"] == "daughter" and r["call"] == "爸爸"
    assert pt.set_relationship("daughter", "  老 妈 ")["call"] == "老 妈"
    with pytest.raises(ValueError):
        pt.set_relationship("boss")
    pt.switch(db, "calm", "keep")
    assert pt.relationship()["kind"] == "daughter"            # a persona switch keeps the relationship


def test_the_relationship_is_in_the_context(db, data_dir):
    from ai import persona_templates as pt
    from ai.context_engine import ContextEngine
    pt.set_relationship("creator")
    text = "\n".join(ContextEngine(db).build("chat", "你好").sections)
    assert "【你们的关系：开发者】" in text and "创造和开发你的人" in text


def test_ai_persona_knows_what_it_is_and_has_no_invented_life(db, data_dir, monkeypatch):
    from ai import persona_templates as pt
    from ai.context_engine import ContextEngine
    from ai.persona import PersonaCore
    monkeypatch.setenv("CHAT_LLM_MODEL", "deepseek-chat")
    pt.switch(db, "ai", "keep")
    core = PersonaCore.load()
    assert core.is_ai and "一个 AI" in core.render() and "deepseek-chat" in core.render() and "照实说是" in core.render()
    packet = ContextEngine(db).build("chat", "你今天干嘛了")
    names = list(packet.report["sections"])
    assert "ai_experience" in names and "ai_abilities" in names and "violet_life" not in names
    text = "\n".join(packet.sections)
    assert "你最近真实的经历" in text and "看懂发来的图片" in text
    from ai.life_engine import LifeEngine
    monkeypatch.setenv("VIOLET_LIFE_ENGINE", "1")
    assert LifeEngine(db).run_day(datetime(2026, 10, 30, 22, 0))["outcome"] == "ai_persona"


def test_ai_persona_consistency(db, data_dir):
    from ai import persona_templates as pt
    from ai.consistency import ConsistencyGuard
    from ai.persona import PersonaCore
    pt.switch(db, "ai", "keep")
    guard = ConsistencyGuard(db, core=PersonaCore.load())
    checks = lambda text: {v.check for v in guard.check_reply(text)}
    assert "forbidden_backstory" in checks("我刚吃了一碗面")
    assert "identity_conflict" in checks("我是真人啦")
    assert checks("我今天在群里和大家聊了很久") == set()             # a real experience, not a virtual-life claim


def test_calling_the_owner_dad_is_not_an_invented_family(db, data_dir):
    from ai import persona_templates as pt
    from ai.consistency import ConsistencyGuard
    from ai.persona import PersonaCore
    guard = ConsistencyGuard(db, core=PersonaCore.load())
    assert "forbidden_backstory" in {v.check for v in guard.check_reply("我爸爸说过这件事")}
    pt.set_relationship("daughter")                               # she calls the owner 爸爸
    assert "forbidden_backstory" not in {v.check for v in guard.check_reply("我爸爸说过这件事")}


def test_a_channel_with_its_own_persona_and_relationship(db, data_dir):
    from ai import persona_templates as pt
    from ai.channels.context import ChannelContext, channel_scope
    from ai.context_engine import ContextEngine
    from ai.persona import PersonaCore
    from ai.social.engine import _persona_text
    pt.set_channel("telegram", "ai", "creator")
    assert PersonaCore.load(channel="telegram").is_ai and not PersonaCore.load(channel="wechat").is_ai
    with channel_scope(ChannelContext("telegram", "telegram", "Telegram")):
        text = "\n".join(ContextEngine(db).build("chat", "你好").sections)
    assert "（AI）" in text and "【你们的关系：开发者】" in text
    assert "【你们的关系" not in "\n".join(ContextEngine(db).build("chat", "你好").sections)   # the default adds nothing
    pt.set_channel("napcat", None, "friend")
    assert "你和你的主人的关系：朋友" in _persona_text(db, "napcat")
    with pytest.raises(ValueError):
        pt.set_channel("napcat", "nope")
    with pytest.raises(ValueError):
        pt.set_channel("napcat", "custom")                         # no custom persona written yet
    pt.set_channel("telegram", None, None)
    assert "telegram" not in pt.channel_map()


def test_persona_page_routes(admin, data_dir):
    d = admin.get("/api/persona/templates").get_json()
    d = d.get("data", d)
    assert d["relationship"]["kind"] == "close" and {r["key"] for r in d["relationships"]} >= {"daughter", "creator"}
    assert any(t["key"] == "ai" for t in d["templates"]) and any(c["id"] == "wechat" for c in d["channels"])
    r = admin.post("/api/persona/relationship", json={"kind": "partner"}, headers=SAME).get_json()
    assert r.get("data", r)["kind"] == "partner"
    assert admin.post("/api/persona/relationship", json={"kind": "x"}, headers=SAME).status_code == 400
    r = admin.post("/api/persona/channel", json={"channel": "wechat", "template": "ai", "kind": "creator"}, headers=SAME)
    r = r.get_json()
    row = next(c for c in r.get("data", r)["channels"] if c["id"] == "wechat")
    assert row["template"] == "ai" and row["relationship"]["kind"] == "creator"
