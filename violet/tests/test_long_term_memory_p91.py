"""Phase 9.1: eight weeks of chat through the real runtime (offline).

Fictional user, isolated DB, fake WeChat, fake SDK, scenario clock, nightly decay, and the real Control Center routes for
the dashboard edits. The fake memory model makes the tool calls a careful model would make; the fake Intent model
NEVER calls query_memory (the worst case of R-M2). What is checked is what really reaches the Chat request -- the
memory lines, their state labels and what is absent -- and what is stored. Whether a real model then words it
naturally is not provable offline (see the real-model plan in tools/README.md §22).
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from tests.fakes import completion
from tests.test_control_center_p90 import SAME, admin, app, env_guard, made  # noqa: F401  (fixtures)
from tests.test_phase2_scenarios import make_world, say  # noqa: F401  (fixture)

START = datetime.fromisoformat("2026-11-02T20:00:00+08:00")       # a Monday
INTERVIEW = "用户11月11日下午三点有一场实习面试，有点紧张"
MOVIE = "用户和Eve约好面试结束后一起云看电影《海上钢琴师》"
PET = "用户养了一只柯基，叫豆豆"
BREAKUP = "用户上个月刚和前任分手，现在好多了"


@pytest.fixture()
def clock(monkeypatch):
    from tools.harness import ScenarioClock, install_clock, uninstall_clock
    c = ScenarioClock(START)
    saved = install_clock(c)
    yield c
    uninstall_clock(saved)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


class MemoryModel:
    """Deterministic memory "model": per user message, the tool calls to make, round by round.

    A step is a list of tool calls or a callable (tool results so far -> list of tool calls or None)."""

    def __init__(self, db):
        self.db = db
        self.plan: list[tuple[str, list]] = []
        self.results: list[dict] = []

    def find(self, needle: str) -> int:
        row = self.db.fetch_one("SELECT id FROM event_memories WHERE content LIKE ? AND status != 'deleted' "
                                "ORDER BY id DESC LIMIT 1", (f"%{needle}%",))
        assert row, f"no memory containing {needle!r}"
        return row["id"]

    def __call__(self, kw):
        messages = kw["messages"]
        last = max(i for i, m in enumerate(messages) if m["role"] == "user")
        said = messages[last]["content"].split("\nViolet 实际回复：")[0]
        after = messages[last + 1:]
        results = [json.loads(m["content"]) for m in after if m["role"] == "tool"]
        self.results.extend(results[-1:] if results else [])
        rounds = sum(1 for m in after if m["role"] == "assistant")
        for trigger, steps in self.plan:
            if trigger in said and rounds < len(steps):
                step = steps[rounds]
                calls = step(results) if callable(step) else step
                if calls:
                    return completion(tool_calls=calls)
        return "整理完了"


def save(**args):
    return {"name": "save_memory", "arguments": {"category": "general", **args}}


def new_event_if_asked(**args):
    """Second round: a needs_decision answer is decided as "another event" (the model saw the candidates)."""
    def step(results):
        if results and results[-1].get("action") == "needs_decision":
            return [save(**args, new_event=True)]
        return None
    return step


def recall_lines(system: str, memory_id: int) -> list[str]:
    return [line for line in system.splitlines() if line.startswith(f"- id={memory_id};")]


def test_eight_weeks_of_memory_through_the_real_pipeline(make_world, clock, admin):
    w = make_world()
    mem = MemoryModel(w.db)
    w.llm.responders["fake-memory"] = mem
    chats: list[tuple[str, str]] = []

    def chat(kw):
        user = next(m["content"] for m in reversed(kw["messages"]) if m["role"] == "user")
        system = "\n".join(m.get("content") or "" for m in kw["messages"] if m["role"] == "system")
        chats.append((user, system))
        return "嗯嗯，我记着呢"
    w.llm.responders["fake-chat"] = chat

    def turn(text: str) -> str:
        before = len(chats)
        say(w, text)
        return next(system for user, system in chats[before:] if user == text)

    def nights(n: int) -> None:
        for _ in range(n):
            w.db.decay_event_memories()

    # --- day 0: the user tells Violet things ---------------------------------------------------------------------
    mem.plan = [
        ("前任", [[save(summary=BREAKUP, kind="event", progress="done", emotional_weight=4)]]),
        ("柯基", [[save(summary=PET, kind="fact", category="pet")]]),
        ("实习面试", [[save(summary=INTERVIEW, kind="event", progress="planned", category="event", importance=3,
                            event_time="2026-11-11T15:00:00+08:00")]]),
        ("海上钢琴师", [[save(summary=MOVIE, kind="event", progress="planned", category="event", importance=3,
                               emotional_weight=3)],
                       new_event_if_asked(summary=MOVIE, kind="event", progress="planned", category="event",
                                          importance=3, emotional_weight=3)]),
    ]
    turn("其实我上个月刚和前任分手，现在好多了")
    turn("我家柯基豆豆今天又把拖鞋咬烂了")
    turn("下周三下午三点我有个实习面试，好紧张")
    turn("说好了哦，等我面试完我们一起云看《海上钢琴师》")
    interview, movie, pet, breakup = mem.find("实习面试"), mem.find("海上钢琴师"), mem.find("柯基"), mem.find("前任")
    assert len({interview, movie, pet, breakup}) == 4                # the agreement did not merge into the interview
    assert any(r.get("action") == "needs_decision" for r in mem.results)
    assert all(json.loads(w.db.fetch_one("SELECT source_message_ids FROM event_memories WHERE id = ?", (i,))
                          ["source_message_ids"]) for i in (interview, movie, pet, breakup))

    # --- day 3: unrelated small talk pulls nothing up -----------------------------------------------------------
    clock.advance_to(at("2026-11-05T21:00:00+08:00"))
    nights(3)
    system = turn("今天食堂的麻辣烫好难吃")
    assert "相关记忆" not in system and BREAKUP not in system and MOVIE not in system

    # --- day 9: the interview happened; Intent did not search, the user's words still bring it back --------------
    clock.advance_to(at("2026-11-11T20:30:00+08:00"))
    nights(6)
    mem.plan = [("面试结束", [lambda r: [{"name": "update_event_progress", "arguments": {
        "memory_id": interview, "progress": "done", "summary": "用户11月11日的实习面试已经结束，感觉不错",
        "reason": "面试结束啦"}}]])]
    system = turn("面试结束啦！感觉还不错")
    lines = recall_lines(system, interview)
    assert lines and "进展未知" in lines[0] and "计划中" not in lines[0]        # time passed: not "planned", not "done"
    assert w.db.get_event_memory(interview)["progress"] == "done"

    # --- day 10: the agreement is remembered as an agreement, then kept ----------------------------------------
    clock.advance_to(at("2026-11-12T21:00:00+08:00"))
    nights(1)
    mem.plan = [("看完啦", [lambda r: [{"name": "update_event_progress", "arguments": {
        "memory_id": movie, "progress": "done", "summary": "用户和Eve一起云看了电影《海上钢琴师》，结尾让用户哭了",
        "reason": "看完啦"}}]])]
    system = turn("我们之前说好的电影还记得吗？今晚看吧")
    lines = recall_lines(system, movie)
    assert lines and "计划中" in lines[0] and "已结束" not in lines[0]          # not "already watched"
    system = turn("看完啦，结尾哭死我了")
    memory_request = w.llm.calls_for("fake-memory")[-1]
    assert f"id={movie};" in "\n".join(m.get("content") or "" for m in memory_request["messages"] if m["role"] == "system")
    assert w.db.get_event_memory(movie)["progress"] == "done"

    # --- day 13: the dashboard corrects the dog's name; a vague chat turn cannot undo it ---------------------
    clock.advance_to(at("2026-11-15T20:00:00+08:00"))
    nights(3)
    corrected = "用户养了一只柯基，叫豆包（不是豆豆）"
    resp = admin.post(f"/api/control/memories/event/{pet}/correct", headers=SAME, json={"content": corrected})
    assert resp.status_code == 200
    mem.plan = [("握手", [lambda r: [{"name": "update_memory", "arguments": {
        "memory_id": pet, "summary": "用户养了一只柯基，叫豆豆，会握手"}}]])]
    system = turn("豆包今天学会握手了")
    assert any(corrected in line for line in recall_lines(system, pet)) and PET not in system
    assert w.db.get_event_memory(pet)["content"] == corrected
    assert any(r.get("reason") == "confirmed_by_user" for r in mem.results)

    # --- day 20: the user's own words change it; the old version is not used again ----------------------------
    clock.advance_to(at("2026-11-22T20:00:00+08:00"))
    nights(7)
    moved = "用户的柯基豆包现在送到用户父母家养了"
    mem.plan = [("爸妈家", [lambda r: [{"name": "update_memory", "arguments": {
        "memory_id": pet, "summary": moved, "evidence": "豆包送去我爸妈家养了"}}]])]
    turn("豆包送去我爸妈家养了，现在我一个人住")
    assert w.db.get_event_memory(pet)["content"] == moved
    clock.advance_to(at("2026-11-23T20:00:00+08:00"))
    nights(1)
    system = turn("好想豆包啊")
    assert any(moved in line for line in recall_lines(system, pet)) and corrected not in system

    # --- day 29: a second, similar event stays a separate event --------------------------------------------------
    clock.advance_to(at("2026-12-01T20:00:00+08:00"))
    nights(8)
    second = "用户12月11日有一场字节的面试"
    mem.plan = [("字节", [[save(summary=second, kind="event", progress="planned", category="event",
                                event_time="2026-12-11T10:00:00+08:00")],
                         new_event_if_asked(summary=second, kind="event", progress="planned", category="event",
                                            event_time="2026-12-11T10:00:00+08:00")])]
    turn("下周五我还有一个面试，这次是字节的")
    bytedance = mem.find("字节")
    assert bytedance != interview and w.db.get_event_memory(interview)["progress"] == "done"
    system = turn("面试的事你还记得多少")
    first, later = recall_lines(system, interview), recall_lines(system, bytedance)
    assert first and later and "已结束" in first[0] and "计划中" in later[0]

    # --- day 30: a memory deleted in the dashboard never comes back --------------------------------------------
    clock.advance_to(at("2026-12-02T20:00:00+08:00"))
    nights(1)
    assert admin.post(f"/api/control/memories/event/{breakup}/delete", headers=SAME,
                      json={"confirm": True}).status_code == 200
    system = turn("今天碰到前任了，好尴尬")
    assert BREAKUP not in system and not recall_lines(system, breakup)

    # --- day 60: the shared experience is still there after weeks of nightly decay ------------------------------
    clock.advance_to(at("2027-01-01T20:00:00+08:00"))
    nights(30)
    system = turn("还记得我们一起看的那部电影吗")
    lines = recall_lines(system, movie)
    assert lines and "已结束" in lines[0] and "印象很模糊" not in lines[0]
    assert w.db.get_event_memory(movie)["status"] != "deleted"
    # every memory written by chat points at the user message it came from (no invented ids)
    from ai.memory_sources import memory_source
    for memory_id in (interview, movie, pet, bytedance):
        source = memory_source(w.db, w.db.get_event_memory(memory_id))
        assert source["state"] == "known"
        assert all(w.db.fetch_one("SELECT role FROM messages WHERE id = ?", (i,))["role"] == "user"
                   for i in source["message_ids"])


def test_pet_names_and_greetings_do_not_pull_up_intimate_memories(violetdb):
    from ai.recall_search import user_word_recall
    violetdb.add_event_memory("用户喜欢Eve叫他宝贝，觉得很甜", kind="fact")
    violetdb.add_event_memory("用户说和Eve的第一次深夜长聊让他很心动", kind="event", progress="done", emotional_weight=4)
    for message in ("宝贝晚安", "早安呀宝贝", "抱抱", "哈哈哈哈", "今天好累啊"):
        assert user_word_recall(violetdb, message) == [], message


def test_word_recall_respects_limit_switch_deleted_and_disabled(violetdb, monkeypatch):
    from ai.recall_search import user_word_recall
    ids = [violetdb.add_event_memory(f"用户的吉他课第{i}次上课", kind="event", progress="done") for i in range(5)]
    violetdb.retract_event_memory(ids[0], reason="test", source="dashboard")
    violetdb.set_event_memory_enabled(ids[1], False)
    found = [m["id"] for m in user_word_recall(violetdb, "今天吉他课好难")]
    assert len(found) == 3 and ids[0] not in found and ids[1] not in found
    monkeypatch.setenv("VIOLET_MEMORY_WORD_RECALL", "0")
    assert user_word_recall(violetdb, "今天吉他课好难") == []


def test_important_memory_stops_at_archived_while_small_talk_may_fade(violetdb):
    important = violetdb.add_event_memory("用户和Eve第一次一起跨年", category="event", kind="event", progress="done",
                                       importance=3)
    small = violetdb.add_event_memory("用户今天吃了麻辣烫", category="event", kind="event", progress="done")
    for _ in range(90):
        violetdb.decay_event_memories()
    assert violetdb.get_event_memory(important)["status"] == "archived"
    assert violetdb.get_event_memory(small)["status"] == "forgotten"


def test_free_chat_runs_the_nightly_decay_once_per_3am_crossed(violetdb):
    from types import SimpleNamespace
    from tools.free_chat import FreeChat
    calls = []
    fake = SimpleNamespace(session=SimpleNamespace(db=SimpleNamespace(
        decay_event_memories=lambda: calls.append(1) or {"expired": 0})))
    runs = FreeChat._decay_nights(fake, at("2026-11-02T20:00:00+08:00"), at("2026-11-05T03:00:00+08:00"))
    assert [r["at"][:16] for r in runs] == ["2026-11-03T03:00", "2026-11-04T03:00", "2026-11-05T03:00"]
    assert FreeChat._decay_nights(fake, at("2026-11-03T03:00:00+08:00"), at("2026-11-03T20:00:00+08:00")) == []
    assert len(calls) == 3


def test_restore_tool_lists_only_rows_without_a_user_deletion(violetdb, tmp_path):
    import shutil
    from tools.memory_restore_decayed import main
    decayed = violetdb.add_event_memory("用户小时候养过一只兔子", kind="event", progress="done")
    violetdb.execute("UPDATE event_memories SET status = 'deleted' WHERE id = ?", (decayed,))     # what old decay did
    by_user = violetdb.add_event_memory("用户不想再提的事", kind="event", progress="done")
    violetdb.retract_event_memory(by_user, reason="用户删除", source="dashboard")
    copy = tmp_path / "copy.db"
    violetdb.conn.execute("PRAGMA wal_checkpoint(FULL)")
    shutil.copy(violetdb.db_path, copy)
    import sqlite3
    assert main(["--db", str(copy)]) == 0                                   # list only
    conn = sqlite3.connect(copy)
    assert conn.execute("SELECT status FROM event_memories WHERE id = ?", (decayed,)).fetchone()[0] == "deleted"
    conn.close()
    assert main(["--db", str(copy), "--apply", "--ids", str(by_user)]) == 2  # a user deletion is never a candidate
    assert main(["--db", str(copy), "--apply"]) == 0
    conn = sqlite3.connect(copy)
    rows = dict(conn.execute("SELECT id, status FROM event_memories").fetchall())
    actions = [r[0] for r in conn.execute("SELECT action FROM memory_revisions WHERE memory_id = ?", (decayed,))]
    conn.close()
    assert rows == {decayed: "forgotten", by_user: "deleted"} and actions == ["restore"]
