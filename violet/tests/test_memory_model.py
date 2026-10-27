"""Phase 2 memory semantics: one test per behaviour change."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from tests.fakes import FakeLLM, default_responders, install_fake_llm


@pytest.fixture()
def agents(monkeypatch, violetdb):
    install_fake_llm(monkeypatch, FakeLLM(default_responders()))
    from ai.agents import IntentAgent, MemoryAgent
    return {"intent": IntentAgent(), "memory": MemoryAgent()}


@contextlib.contextmanager
def turn_context(**ctx):
    from ai.agents.base_agent import _CALL_CONTEXT
    token = _CALL_CONTEXT.set(ctx)
    try:
        yield
    finally:
        _CALL_CONTEXT.reset(token)


def call(agent, tool, **args):
    return json.loads(getattr(agent, f"_tool_{tool}")(args)) if tool != "query_memory" else getattr(agent, f"_tool_{tool}")(args)


def shift(db, table, column, row_id, **delta):
    value = (datetime.fromisoformat(db.now()) - timedelta(**delta)).isoformat()
    db.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, row_id))
    return value


# --- recall does not reinforce ---------------------------------------------------------

def test_recall_does_not_make_memory_more_important(agents, violetdb):
    memory_id = violetdb.add_event_memory("用户的服务器出现故障", category="event", kind="event", progress="ongoing")
    before = violetdb.get_event_memory(memory_id)
    for _ in range(5):
        result = agents["intent"]._tool_query_memory({"query": "服务器 故障"})
        assert result["memories"][0]["id"] == memory_id
    after = violetdb.get_event_memory(memory_id)
    assert after["forget_weight"] == before["forget_weight"]
    assert after["importance"] == before["importance"]
    assert after["recall_count"] == before["recall_count"] + 5      # bookkeeping still kept


def test_user_remention_adjusts_importance_only_with_new_information(violetdb):
    created = violetdb.save_event_memory("用户每天晨跑", kind="fact", importance=2)["memory"]
    same = violetdb.save_event_memory("用户每天晨跑", kind="fact", importance=1)
    assert same["action"] == "reinforced"
    assert same["memory"]["importance"] == 2 and same["memory"]["mention_count"] == 2
    stronger = violetdb.save_event_memory("用户每天晨跑", kind="fact", importance=4)
    assert stronger["memory"]["importance"] == 4
    assert stronger["memory"]["forget_weight"] >= created["forget_weight"]


def test_exact_short_query_finds_the_memory_that_contains_it(agents, violetdb):
    memory_id = violetdb.save_event_memory("用户的服务器故障已经修好了", kind="event", progress="done")["memory"]["id"]
    found = agents["intent"]._tool_query_memory({"query": "服务器"})["memories"]
    assert [m["id"] for m in found] == [memory_id]
    assert violetdb.text_similarity("服务器", "用户的服务器故障已经修好了") >= 0.8


# --- no merging by character overlap ------------------------------------------------------

def test_similar_text_is_never_merged_automatically(violetdb):
    original = violetdb.save_event_memory("用户的服务器出现故障", kind="event", progress="ongoing")["memory"]
    result = violetdb.save_event_memory("用户公司的服务器下周要迁移", kind="event", progress="planned")
    assert result["action"] == "needs_decision"
    assert [c["id"] for c in result["candidates"]] == [original["id"]]
    assert violetdb.fetch_one("SELECT COUNT(*) AS c FROM event_memories")["c"] == 1
    assert violetdb.get_event_memory(original["id"])["content"] == "用户的服务器出现故障"
    separate = violetdb.save_event_memory("用户公司的服务器下周要迁移", kind="event", progress="planned", new_event=True)
    assert separate["action"] == "created"
    assert violetdb.get_event_memory(original["id"])["progress"] == "ongoing"


def test_new_progress_is_applied_to_the_linked_event(agents, violetdb):
    original = violetdb.save_event_memory("用户的服务器出现故障", kind="event", progress="ongoing")["memory"]
    with turn_context(turn_id="t-fix", user_message="服务器已经修好了"):
        unlinked = call(agents["memory"], "save_memory", summary="用户的服务器已经修好了", category="event", progress="done")
        assert unlinked["action"] == "needs_decision"
        linked = call(agents["memory"], "save_memory", summary="用户的服务器故障已经修好", category="event",
                      progress="done", related_to=original["id"])
    assert linked["action"] == "updated" and linked["memory_id"] == original["id"]
    memory = violetdb.get_event_memory(original["id"])
    assert memory["progress"] == "done" and memory["content"] == "用户的服务器故障已经修好"
    revisions = violetdb.list_memory_revisions("event_memories", original["id"])
    assert revisions[-1]["old_content"] == "用户的服务器出现故障" and revisions[-1]["new_progress"] == "done"
    assert violetdb.fetch_one("SELECT COUNT(*) AS c FROM event_memories")["c"] == 1


def test_same_event_key_updates_the_open_event(violetdb):
    first = violetdb.save_event_memory("用户在准备期末考试", kind="event", progress="ongoing", event_key="final_exam")
    second = violetdb.save_event_memory("用户期末考试考完了一门", kind="event", progress="ongoing", event_key="final_exam")
    assert second["action"] == "updated_by_key" and second["memory"]["id"] == first["memory"]["id"]


# --- kinds, time, validity ----------------------------------------------------------------

def test_kinds_get_distinct_defaults(violetdb):
    fact = violetdb.save_event_memory("用户平时住校", kind="fact")["memory"]
    background = violetdb.save_event_memory("用户中午吃了麻辣烫", kind="background")["memory"]
    event = violetdb.save_event_memory("用户周五有期中考试", kind="event", progress="planned",
                                    event_time=(datetime.fromisoformat(violetdb.now()) + timedelta(days=2)).isoformat())["memory"]
    assert (fact["kind"], fact["progress"], fact["valid_until"]) == ("fact", "na", None)
    assert background["progress"] == "na" and background["valid_until"]
    assert event["progress"] == "planned" and event["valid_until"] > event["event_time"]


def test_relative_dates_become_absolute_times(agents, violetdb):
    from ai.memory_model import resolve_time
    now = datetime.fromisoformat("2026-10-11T20:00:00+08:00")
    assert resolve_time(date_hint="tomorrow", time_hint="早上", now=now) == "2026-10-12T08:00:00+08:00"
    assert resolve_time(date_hint="2026-10-20", now=now) == "2026-10-20T23:59:00+08:00"
    assert resolve_time(date_hint="明天", now=now, default_time="09:00") == "2026-10-12T09:00:00+08:00"
    with turn_context(turn_id="t1", user_message="明天下午三点面试"):
        saved = call(agents["memory"], "save_memory", summary="用户有一场面试", category="event",
                     progress="planned", event_date="tomorrow", event_clock="15:00")
    expected_day = (datetime.fromisoformat(violetdb.now()) + timedelta(days=1)).date().isoformat()
    assert saved["event_time"].startswith(expected_day + "T15:00")


def test_time_passing_never_marks_an_event_done(violetdb):
    memory = violetdb.save_event_memory("用户有一场面试", kind="event", progress="planned",
                                     event_time=(datetime.fromisoformat(violetdb.now()) + timedelta(hours=1)).isoformat())["memory"]
    shift(violetdb, "event_memories", "event_time", memory["id"], days=3)
    violetdb.refresh_event_states()
    after = violetdb.get_event_memory(memory["id"])
    assert after["progress"] == "unknown"
    assert violetdb.list_memory_revisions("event_memories", memory["id"])[-1]["reason"] == "event time passed without news"
    shift(violetdb, "event_memories", "valid_until", memory["id"], days=1)
    violetdb.refresh_event_states()
    assert violetdb.get_event_memory(memory["id"])["status"] == "expired"
    assert violetdb.get_event_memory(memory["id"])["progress"] == "unknown"


def test_long_term_facts_do_not_decay(violetdb):
    fact = violetdb.save_event_memory("用户养了一只狗", kind="fact")["memory"]
    event = violetdb.save_event_memory("用户这周加班", kind="event", progress="ongoing")["memory"]
    for _ in range(30):
        violetdb.decay_event_memories()
    assert violetdb.get_event_memory(fact["id"])["forget_weight"] == fact["forget_weight"]
    assert violetdb.get_event_memory(event["id"])["forget_weight"] < event["forget_weight"]


def test_legacy_rows_read_as_unknown_progress_without_rewriting(tmp_path):
    from database import VioletDatabase
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE event_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'general',
            happened_at TEXT, valid_until TEXT, status TEXT NOT NULL DEFAULT 'active',
            importance REAL NOT NULL DEFAULT 1.0, emotional_weight REAL NOT NULL DEFAULT 1.0,
            recall_count INTEGER NOT NULL DEFAULT 1, mention_count INTEGER NOT NULL DEFAULT 1,
            forget_weight REAL NOT NULL DEFAULT 1.0, source_message_ids TEXT, last_recalled_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO event_memories (content, category, created_at, updated_at)
        VALUES ('用户明天上午十点有面试，适合提前温柔鼓励', 'event', '2026-05-01T10:00:00+08:00', '2026-05-01T10:00:00+08:00');
    """)
    conn.commit()
    conn.close()
    db = VioletDatabase(db_path=path)
    try:
        row = db.list_event_memories()[0]
        assert row["content"] == "用户明天上午十点有面试，适合提前温柔鼓励"
        assert (row["kind"], row["progress"], row["status"]) == ("legacy", "unknown", "active")
    finally:
        db.close()


# --- commitments ---------------------------------------------------------------------------

@pytest.mark.parametrize("evidence,message,ok,reason", [
    ("明天提醒我交作业", "明天提醒我交作业哦", True, "explicit_request"),
    ("到时候问我一下结果", "周五面试，到时候问我一下结果", True, "explicit_request"),
    ("我今天去面试了", "我今天去面试了", False, "not_an_explicit_request"),
    ("不用提醒我了", "不用提醒我了", False, "request_is_negated"),
    ("明天提醒我交作业", "我今天去面试了", False, "evidence_not_in_user_message"),
    ("", "提醒我", False, "missing_evidence"),
])
def test_explicit_request_check(evidence, message, ok, reason):
    from ai.memory_model import check_explicit_request
    assert check_explicit_request(evidence, message) == (ok, reason)


def test_commitment_tool_requires_explicit_authorisation(agents, violetdb):
    with turn_context(turn_id="t-share", user_message="我今天去面试了，有点紧张"):
        rejected = call(agents["memory"], "create_commitment", content="问面试结果", evidence="我今天去面试了",
                        due_date="tomorrow")
    assert rejected["action"] == "rejected" and violetdb.list_commitments() == []
    with turn_context(turn_id="t-ask", user_message="明天早上提醒我交实验报告"):
        created = call(agents["memory"], "create_commitment", content="交实验报告", evidence="明天早上提醒我交实验报告",
                       due_date="tomorrow", due_clock="早上")
        again = call(agents["memory"], "create_commitment", content="交实验报告", evidence="明天早上提醒我交实验报告",
                     due_date="tomorrow", due_clock="早上")
    assert created["ok"] and again["action"] == "already_saved"
    rows = violetdb.list_commitments()
    assert len(rows) == 1 and rows[0]["status"] == "pending" and rows[0]["due_at"].endswith("T08:00:00+08:00")


def test_commitment_can_be_cancelled_by_the_user(agents, violetdb):
    with turn_context(turn_id="t-a", user_message="明天提醒我买牛奶"):
        cid = call(agents["memory"], "create_commitment", content="买牛奶", evidence="明天提醒我买牛奶",
                   due_date="tomorrow")["commitment_id"]
    with turn_context(turn_id="t-b", user_message="牛奶我买好了，不用提醒了"):
        assert call(agents["memory"], "cancel_commitment", commitment_id=cid, evidence="其他人说的")["action"] == "rejected"
        assert call(agents["memory"], "cancel_commitment", commitment_id=cid, evidence="不用提醒了")["action"] == "cancelled"
    assert violetdb.get_commitment(cid)["status"] == "cancelled"


# --- corrections ---------------------------------------------------------------------------

def test_correction_replaces_content_and_keeps_history(agents, violetdb):
    wrong = violetdb.save_event_memory("用户养了一只猫", kind="fact", confidence=0.6)["memory"]
    with turn_context(turn_id="t-c", user_message="我养的是狗不是猫"):
        result = call(agents["memory"], "correct_memory", memory_id=wrong["id"], corrected_summary="用户养了一只狗",
                      reason="我养的是狗不是猫")
    assert result["action"] == "corrected"
    memory = violetdb.get_event_memory(wrong["id"])
    assert memory["content"] == "用户养了一只狗" and memory["confidence"] == 1.0
    revision = violetdb.list_memory_revisions("event_memories", wrong["id"])[-1]
    assert (revision["action"], revision["old_content"], revision["source"]) == ("correct", "用户养了一只猫", "user_correction")


def test_retracted_memory_is_never_recalled(agents, violetdb):
    memory = violetdb.save_event_memory("用户有个姐姐叫小雨", kind="fact")["memory"]
    with turn_context(turn_id="t-r", user_message="我没有姐姐，你记错人了"):
        call(agents["memory"], "retract_memory", memory_id=memory["id"], reason="我没有姐姐")
    assert agents["intent"]._tool_query_memory({"query": "姐姐 小雨"})["memories"] == []


# --- context formatting ------------------------------------------------------------------

def test_recalled_memories_are_labelled_not_templated(violetdb):
    from ai.chat import format_recalled_memories
    text = format_recalled_memories([
        {"id": 1, "content": "用户的服务器出现故障", "kind": "event", "progress": "unknown", "status": "active"},
        {"id": 2, "content": "用户养了一只狗", "kind": "fact", "progress": "na", "status": "active"},
        {"id": 3, "content": "旧事", "kind": "legacy", "progress": "unknown", "status": "deleted"},
    ])
    assert "[事件 / 进展未知] 用户的服务器出现故障" in text
    assert "[长期事实] 用户养了一只狗" in text
    assert "旧事" not in text
    for template in ("你之前说过", "我隐约记得", "我有点印象"):
        assert template not in text


# --- engagement / topic fatigue -----------------------------------------------------------

@pytest.mark.parametrize("text,engaged", [
    ("嗯", False), ("哦哦", False), ("好的", False), ("还行吧", False), ("哈哈哈", False), ("。。。", False),
    ("还没修好，烦死了", True), ("今天去图书馆看书了", True),
])
def test_engagement_classifier(text, engaged):
    from ai.memory_model import is_engaged_reply
    assert is_engaged_reply(text) is engaged


def test_topic_cools_after_unengaged_raises_and_resets_when_user_brings_it_up(violetdb):
    from ai.conversation_state import TopicTracker
    tracker = TopicTracker(violetdb)
    memory_id = violetdb.save_event_memory("用户在学吉他", kind="fact")["memory"]["id"]
    for reply in ("嗯", "哦"):
        tracker.record_eve_topic("guitar", [memory_id])
        tracker.evaluate_user_turn(reply)
    key = f"memory:{memory_id}"
    assert tracker.topic_state(key)["cooling"] and tracker.topic_state("guitar")["cooling"]
    assert {c["topic_key"] for c in tracker.cooling_topics()} == {"guitar", key}
    tracker.record_user_topic(memory_id=memory_id)
    assert not tracker.topic_state(key)["cooling"]


def test_engaged_reply_keeps_topic_warm(violetdb):
    from ai.conversation_state import TopicTracker
    tracker = TopicTracker(violetdb)
    for reply in ("嗯", "对呀今天又练了一个小时"):
        tracker.record_eve_topic("guitar")
        tracker.evaluate_user_turn(reply)
    assert not tracker.topic_state("guitar")["cooling"]
    assert tracker.cold_streak() == 0
