"""2026-11-03 (user: 冷漠、惜字如金、完全感受不到情绪 -- WeChat with the Eve persona, relationship 恋人)."""

from __future__ import annotations


def test_a_chosen_relationship_is_not_overridden_by_the_guessed_stage(monkeypatch, violetdb):
    from ai import persona_templates
    from ai.context_engine import ContextEngine
    violetdb.execute("INSERT INTO messages (role, direction, message_type, content, created_at) VALUES "
                     "('user', 'incoming', 'text', '好肉麻啊你', '2026-09-01T10:00:00+08:00')")
    violetdb.add_user_message("text", "在干嘛")
    monkeypatch.setattr(persona_templates, "relationship", lambda channel: {"kind": "close", "label": "亲近（默认）", "call": None})
    default = "\n".join(ContextEngine(violetdb)._relationship_section().lines)
    assert "保持轻松友好的距离" in default                      # the default relationship still follows the log
    monkeypatch.setattr(persona_templates, "relationship", lambda channel: {"kind": "partner", "label": "恋人", "call": "宝宝"})
    chosen = "\n".join(ContextEngine(violetdb)._relationship_section().lines)
    assert "保持轻松友好的距离" not in chosen and "第一次聊天在" in chosen


def test_the_owners_own_bad_day_is_not_scolding_and_an_upset_mood_is_not_cold(violetdb):
    from ai import mood
    assert not mood.SCOLD.search("今天工作好烦") and not mood.SCOLD.search("烦死了")
    assert mood.SCOLD.search("你好烦") and mood.SCOLD.search("烦死你了") and mood.SCOLD.search("笨蛋")
    mood.feel(violetdb, "你好烦啊笨蛋")
    mood.feel(violetdb, "你好烦啊笨蛋")
    line = mood.note(violetdb)
    assert "不高兴" in line and "短一点" not in line and "淡一点" not in line and "不冷着对方" in line
    assert "话也会比平时多" in line                             # upset = more words, not fewer


def test_the_preferences_list_says_them_in_words():
    import os
    js = open(os.path.join(os.path.dirname(__file__), "..", "wx_ilink_py", "static", "control.js"), encoding="utf-8").read()
    assert 'reply_length: { short: "喜欢简短的回复"' in js and "prefText(r).includes(q)" in js
    from ai.profile_schema import PREF_LABELS
    assert PREF_LABELS["reply_length"]["short"] == "喜欢简短的回复"          # same words as the page
