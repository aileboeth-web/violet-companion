"""0.7.49 (user: Eve 的记忆没迁移到位导致她出现问题; 条数最多只有四条，原本 Eve 可以发十多条; 搜索要把字词高亮).

- Eve's core memories are shown as Eve showed them: labelled by key, "优先遵守", up to 20.
- Eve's own event records ("可称呼宝宝或老公") say whose words they are: notes for HER about the owner.
- Lines one side sent in a row reach the model as ONE message (it saw "one reply = one line" before).
- The memories page marks the searched words.
"""

from __future__ import annotations

from pathlib import Path

from ai.agents.chat_agent import merge_runs
from ai.chat import format_recalled_memories
from ai.context_engine import LEGACY_CORE_HEADER, legacy_core_label

ROOT = Path(__file__).resolve().parents[1]


def test_legacy_core_labels_follow_eve_keys():
    assert legacy_core_label("reply_style", "reply_style") == "对方喜欢你怎么回消息"
    assert legacy_core_label("nickname_preference", "nickname") == "你怎么称呼对方"
    assert legacy_core_label("user_nickname_v2", None) == "你怎么称呼对方"          # a key Eve's model made up
    assert legacy_core_label("whatever", "boundary") == "对方的雷区（不要这样说）"   # falls back to the category
    assert legacy_core_label("whatever", "general") == ""
    assert "优先遵守" in LEGACY_CORE_HEADER and "未逐条确认" not in LEGACY_CORE_HEADER
    assert "你怎么对对方" in LEGACY_CORE_HEADER


def test_recalled_eve_record_says_who_calls_whom():
    eve = {"id": -3, "kind": "legacy", "progress": "unknown", "status": "active",
           "content": "用户睡前先垫一垫肚子，然后准备睡觉休息；夜间互动适度收敛，可称呼“宝宝”或“老公”。"}
    text = format_recalled_memories([eve])
    assert "可称呼宝宝”= 你叫对方宝宝" in text and "“用户”就是对方" in text
    own = {"id": 5, "kind": "event", "progress": "planned", "status": "active", "content": "下周三面试"}
    assert "“用户”就是对方" not in format_recalled_memories([own])          # Violet's own records need no note


def test_lines_in_a_row_are_one_message(monkeypatch):
    history = [
        {"role": "user", "content": "我要睡了"},
        {"role": "assistant", "content": "晚安臭宝"},
        {"role": "assistant", "content": "被子盖好"},
        {"role": "system", "content": "【聊天记录：这里隔了5小时】"},
        {"role": "assistant", "content": "醒了没"},
        {"role": "user", "content": "醒了"},
        {"role": "user", "content": "好困"},
    ]
    merged = merge_runs(history)
    assert [m["role"] for m in merged] == ["user", "assistant", "system", "assistant", "user"]
    assert merged[1]["content"] == "晚安臭宝\n被子盖好"
    assert merged[4]["content"] == "醒了\n好困"
    assert history[1]["content"] == "晚安臭宝"                               # the input is not changed
    monkeypatch.setenv("VIOLET_CONTEXT_MERGE_RUNS", "0")
    assert merge_runs(history) == history


def test_memories_page_marks_the_searched_words():
    js = (ROOT / "wx_ilink_py" / "static" / "control.js").read_text(encoding="utf-8")
    css = (ROOT / "wx_ilink_py" / "static" / "control.css").read_text(encoding="utf-8")
    assert "function marked(text, q)" in js and 'h("mark", { class: "hit"' in js
    assert "marked(m.content, memQuery())" in js                              # core + event memories
    assert "marked(text, memQuery())" in js                                   # profile / situations / preferences
    assert "marked(r.content, q)" in js                                       # reminders
    assert "mark.hit{" in css
    assert "`重要度 ${num(m.importance)}`" in js                              # 2.5999999999999996 -> 2.6
