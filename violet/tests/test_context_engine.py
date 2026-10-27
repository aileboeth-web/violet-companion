"""Phase 3 context engine rules (fictional data only)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def engine(violetdb):
    from ai.context_engine import ContextEngine
    return ContextEngine(violetdb)


def say_fact(engine, key, value, words):
    return engine.profile.facts.remember(key, value, evidence=words, user_message=words)["fact"]


def sections(packet):
    # user-side sections only; Violet's own persona/life sections are covered by test_persona_life.py
    return "\n".join(s for s in packet.sections if not s.startswith("【Violet"))


def test_profile_is_selected_by_relevance_not_dumped(engine):
    say_fact(engine, "address_name", "小满", "叫我小满")
    say_fact(engine, "identity.status", "大学生", "我是大学生")
    say_fact(engine, "residence.usual", "平时住学校宿舍", "我平时住校")
    say_fact(engine, "interest", "打羽毛球", "我喜欢打羽毛球")
    unrelated = sections(engine.build("chat", "今天的晚霞好好看"))
    assert "希望被称呼为：小满" in unrelated                       # always relevant
    assert "大学生" not in unrelated and "宿舍" not in unrelated and "羽毛球" not in unrelated
    related = engine.build("chat", "下午还有两节课，宿舍又停水")
    text = sections(related)
    assert "身份：大学生" in text and "平时住：平时住学校宿舍" in text and "用户亲口说" not in text   # natural: no source tags
    assert "羽毛球" not in text
    assert len(related.report["fact_ids"]) == 3


def test_usual_residence_is_never_the_current_location(engine):
    say_fact(engine, "residence.usual", "平时住学校宿舍", "我平时住校")
    # asked where they are now: the usual residence is NOT offered as an answer
    text = sections(engine.build("chat", "猜猜我现在在哪"))
    assert "此刻在哪里、在做什么：不知道（不要根据常住地或生活规律推断）" in text
    assert "平时住学校宿舍" not in text
    # talking about dorm life: the fact is used, with the usual-is-not-now note
    text = sections(engine.build("chat", "宿舍今天好吵"))
    assert "平时住：平时住学校宿舍" in text and "不代表用户此刻在哪里" in text and "不知道" in text
    engine.profile.situations.observe("location", "家里", "user_stated", evidence="今天回家了", user_message="今天回家了", until="today")
    text = sections(engine.build("chat", "猜猜我现在在哪"))
    assert "此刻所在：家里（对方说的" in text and "不知道" not in text


def test_expired_situation_is_not_used(engine, violetdb):
    s = engine.profile.situations.observe("activity", "在图书馆自习", "user_stated", evidence="在图书馆自习",
                                          user_message="在图书馆自习", until="2h")["situation"]
    assert "在图书馆自习" in sections(engine.build("chat", "好困"))
    violetdb.execute("UPDATE user_situations SET expires_at = ? WHERE id = ?",
                  ((datetime.fromisoformat(violetdb.now()) - timedelta(minutes=5)).isoformat(), s["id"]))
    text = sections(engine.build("chat", "你猜我现在在干嘛"))
    assert "在图书馆自习" not in text and "不知道" in text


def test_candidates_are_never_presented_as_facts(engine):
    engine.profile.facts.remember("identity.status", "研究生", evidence="猜的", user_message="今天在实验室")
    assert "研究生" not in sections(engine.build("chat", "实验室好冷"))


def test_disabled_values_are_masked_everywhere(engine, violetdb):
    fact = say_fact(engine, "study.school", "星河大学", "我在星河大学")
    violetdb.add_user_message("text", "我在星河大学")
    violetdb.add_assistant_message("text", "星河大学挺好的")
    engine.profile.facts.set_enabled(fact["id"], False)
    packet = engine.build("chat", "学校今天停电", {"recalled_memories": [
        {"id": 99, "content": "用户在星河大学参加社团", "kind": "event", "progress": "ongoing", "status": "active"}]})
    everything = sections(packet) + "".join(h["content"] for h in packet.history)
    assert "星河大学" not in everything and "[已停用的信息]" in everything


def test_disabled_event_memory_is_dropped_from_recall(engine, violetdb):
    memory_id = violetdb.add_event_memory("用户和室友闹了点矛盾", kind="event", progress="ongoing")
    violetdb.set_event_memory_enabled(memory_id, False)
    recalled = [{"id": memory_id, "content": "用户和室友闹了点矛盾", "kind": "event", "progress": "ongoing", "status": "active"}]
    assert "室友闹了点矛盾" not in sections(engine.build("chat", "室友", {"recalled_memories": recalled}))
    assert violetdb.search_event_memories("室友 矛盾") == []
    assert [m["id"] for m in violetdb.search_event_memories("室友 矛盾", include_disabled=True)] == [memory_id]


def test_history_is_dialogue_not_a_system_summary(engine, violetdb):
    violetdb.add_user_message("text", "今天好累")
    violetdb.add_assistant_message("text", "怎么啦")
    packet = engine.build("chat", "考了一天试")
    assert packet.history == [{"role": "user", "content": "今天好累"}, {"role": "assistant", "content": "怎么啦"}]
    assert "最近聊天记录" not in sections(packet) and "今天好累" not in sections(packet)


def test_budget_trims_low_priority_items_first(engine, violetdb, monkeypatch):
    say_fact(engine, "address_name", "小满", "叫我小满")
    for i in range(30):
        violetdb.upsert_core_memory(f"legacy_{i}", "other", f"旧系统里的一条很长很长的记忆内容，编号{i}，" + "填充" * 20)
    # phase 8.9.3: persona 1.2.0 renders ~810 chars (1.1.0: ~670) and is never trimmed; the budget leaves room
    # above it so the test still shows what goes first (legacy) and what stays (the address name)
    monkeypatch.setenv("VIOLET_CONTEXT_MAX_CHARS", "950")
    monkeypatch.setenv("VIOLET_CONTEXT_MAX_LEGACY", "30")
    packet = engine.build("chat", "早")
    assert packet.report["trimmed_items"] > 0
    text = sections(packet)
    assert "希望被称呼为：小满" in text
    assert sum(len(s) for s in packet.sections) < 1300
    assert packet.report["approx_tokens"] > 0


def test_known_keys_listed_without_values(engine):
    say_fact(engine, "study.school", "星河大学", "我在星河大学")
    text = sections(engine.build("chat", "早"))
    assert "【已经了解过的方面（不要再问这些）】\n- 学校" in text


def test_base_info_has_the_time_and_no_location_source(engine):
    # Violet: the upstream vehicle-location feature is gone; nothing in the context claims to know where anyone is
    text = sections(engine.build("chat", "早"))
    assert "当前时间" in text
    assert "车辆" not in text and "用户当前位置" not in text


def test_preferences_and_boundaries_carry_their_origin(engine):
    engine.profile.preferences.set_explicit("reply_length", "short", evidence="回我短一点", user_message="回我短一点就行")
    for _ in range(4):
        engine.profile.preferences.observe("follow_up_questions", "less", "engagement")
    say_fact(engine, "boundary", "不想聊考研的事", "别跟我聊考研")
    text = sections(engine.build("chat", "早"))
    assert "喜欢简短的回复（用户明确说过）：回复短一点" in text
    assert "不喜欢被连续追问（从最近的聊天看出来的倾向，不一定准）" in text
    assert "边界：不想聊考研的事（对方亲口说的）" in text
