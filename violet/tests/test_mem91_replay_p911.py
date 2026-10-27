"""Phase 9.1.1: the Phase 9.1 real session mem91-real-1 replayed through the real runtime (offline).

Same fictional user, same messages, same simulated dates (2026-11-02 → 11-30, nightly decay), same dashboard actions
(the Control Center routes), and the memory model making the calls the real one made -- including the ones that went
wrong: a correction without naming the old fact, the agreement linked to the interview with only its date. What is
checked is the stored state and what reaches the Chat request (sections only: the history is real dialogue and is
checked on its own). Whether a real model then words it well is for a real run to show (tools/README.md §22).
"""

from __future__ import annotations

import json

from tests.test_control_center_p90 import SAME, admin, app, env_guard, made  # noqa: F401  (fixtures)
from tests.test_long_term_memory_p91 import MemoryModel, at, clock, recall_lines, save  # noqa: F401  (clock: fixture)
from tests.test_memory_fixes_p911 import BREAKUP, CORRECTION, DOG_NEW, DOG_OLD, INTERVIEW, MOVIE_MERGED, SAY_DOG
from tests.test_phase2_scenarios import make_world, say  # noqa: F401  (fixture)
from tests.fakes import completion, intent_json


def tool(name, **arguments):
    return {"name": name, "arguments": arguments}


def test_mem91_real_session_replayed_with_the_fixes(make_world, clock, admin):
    from ai.context_engine import NOT_FOUND_LINE
    from ai.profile_store import REDACTED
    w = make_world()
    db = w.db
    mem = MemoryModel(db)
    w.llm.responders["fake-memory"] = mem
    searches = {"约好": "约定", "拉面店": "拉面店"}

    def intent(kw):                                   # the real Intent searched in rounds 9 and 11
        if any(m["role"] == "tool" for m in kw["messages"]):
            return intent_json("chat")
        said = next(m["content"] for m in reversed(kw["messages"]) if m["role"] == "user")
        query = next((q for k, q in searches.items() if k in said), None)
        return completion(tool_calls=[tool("query_memory", query=query)]) if query else intent_json("chat")
    w.llm.responders["fake-intent"] = intent
    chats: list[dict] = []
    w.llm.responders["fake-chat"] = lambda kw: chats.append(kw) or "嗯嗯，我记着呢"

    def turn(text: str) -> tuple[str, str]:
        """(the system part of this turn's Chat request, its dialogue history)."""
        before = len(chats)
        say(w, text)
        kw = chats[before]                            # this turn's (first) Chat request
        system = "\n".join(m.get("content") or "" for m in kw["messages"] if m["role"] == "system")
        history = "\n".join(m.get("content") or "" for m in kw["messages"][:-1] if m["role"] in ("user", "assistant"))
        return system, history

    def nights(n: int) -> None:
        for _ in range(n):
            db.decay_event_memories()

    # --- launch 1, 11-02 20:00: the dog, the breakup, the interview, the agreement -------------------------------------
    mem.plan = [
        ("柯基叫豆豆", [[tool("remember_profile_fact", key="other", value=DOG_OLD, evidence="我家养了只柯基叫豆豆，三岁了，特别能拆家"),
                       save(summary=BREAKUP, kind="event", progress="done", importance=4, emotional_weight=4)]]),
        ("实习面试", [[save(summary=INTERVIEW, kind="event", progress="planned", category="event", importance=4,
                          emotional_weight=4, event_date="2026-11-11", event_clock="15:00")]]),
        # the real model linked the agreement to the interview, with only the date (that is where 23:59 came from)
        ("海上钢琴师", [lambda r: [save(summary=MOVIE_MERGED, kind="event", category="event",
                                         related_to=mem.find("实习面试"), event_date="2026-11-11")]]),
    ]
    turn(SAY_DOG)
    turn("下周三下午三点我有个实习面试，好紧张啊")
    turn("说好了哦，等我面试完，我们一起云看《海上钢琴师》")
    agreement = mem.find("海上钢琴师")
    breakup = mem.find("前任")
    row = db.get_event_memory(agreement)
    assert row["event_time"] == "2026-11-11T15:00:00+08:00" and row["progress"] == "planned"   # D-P91-1
    dog = db.fetch_one("SELECT id FROM user_profile_facts WHERE value = ?", (DOG_OLD,))["id"]

    # --- the dashboard call of the real test: no proactive messages --------------------------------------------------
    from ai.profile_store import Profile
    assert Profile(db).preferences.set_explicit("proactive_chat", "no", source="dashboard", actor="dashboard")["action"] == "set"

    # --- launch 2, 11-04: the correction without naming the old fact -> ambiguous, then with the candidate's id ------
    clock.advance_to(at("2026-11-04T20:03:00+08:00"))
    nights(2)

    def pick_candidate(results):
        last = results[-1] if results else {}
        if last.get("action") == "ambiguous":
            return [tool("correct_profile_fact", key="other", new_value=DOG_NEW, fact_id=last["candidates"][0]["id"],
                         evidence=CORRECTION)]
        return None
    mem.plan = [("叫豆包", [[tool("correct_profile_fact", key="other", new_value=DOG_NEW, evidence=CORRECTION)],
                           pick_candidate])]
    mem.results.clear()
    turn(CORRECTION)
    assert [r.get("action") for r in mem.results if "action" in r][:2] == ["ambiguous", "corrected"]
    facts = db.fetch_all("SELECT id, value, status, supersedes_id, superseded_by FROM user_profile_facts ORDER BY id")
    new_dog = next(f["id"] for f in facts if f["value"] == DOG_NEW)
    assert [(f["value"], f["status"]) for f in facts] == [(DOG_OLD, "superseded"), (DOG_NEW, "active")]  # D-P91-2
    assert facts[0]["superseded_by"] == new_dog and facts[1]["supersedes_id"] == dog

    # --- launch 3, 11-06: the same correction again (the real model named the fact it had just written) --------------
    clock.advance_to(at("2026-11-06T20:04:00+08:00"))
    nights(2)
    mem.plan = [("叫豆包", [[tool("correct_profile_fact", key="other", new_value=DOG_NEW, fact_id=dog, evidence=CORRECTION)]])]
    mem.results.clear()
    turn(CORRECTION)
    assert mem.results[-1]["action"] == "confirmed"
    assert db.fetch_one("SELECT COUNT(*) AS n FROM user_profile_facts WHERE status = 'active'")["n"] == 1

    # --- launch 4, 11-30 (24 more nights): long-term recall, the corrected name, the unfinished agreement -------------
    clock.advance_to(at("2026-11-30T20:05:00+08:00"))
    nights(24)
    mem.plan = []
    system, _ = turn("好久没聊啦！你还记得我家狗叫什么吗？还有我们之前约好要一起做的那件事，你还记得吗？")
    assert "名叫豆包" in system and "名叫豆豆" not in system           # the superseded fact never comes back
    lines = recall_lines(system, agreement)
    assert lines and "15:00" in lines[0] and "进展未知" in lines[0] and "已结束" not in lines[0]
    assert "23:59" not in system and db.get_event_memory(agreement)["progress"] == "unknown"     # never "done"

    # --- the dashboard deletes the breakup: no memory path brings it back; the history still holds the words ---------
    assert admin.post(f"/api/control/memories/event/{breakup}/delete", headers=SAME, json={"confirm": True}).status_code == 200
    system, history = turn("今天在地铁上碰到前任了，好尴尬")
    assert BREAKUP not in system and not recall_lines(system, breakup)
    assert "上个月刚和前任分手了" in history                            # delete is not forget (P1, documented)

    # --- forgetting it removes those words from the history too, and only those --------------------------------------
    resp = admin.post(f"/api/control/memories/event/{breakup}/forget", headers=SAME, json={"confirm": True})
    assert resp.status_code == 200 and resp.get_json()["data"]["messages_changed"]
    system, history = turn("算了不想这个了")
    assert "前任分手" not in history and "上个月刚和前任" not in history and REDACTED in history
    assert "我家养了只柯基叫豆豆" in history                           # the dog clause of the same message stays
    assert "今天在地铁上碰到前任了，好尴尬" in history                    # a later turn is not touched

    # --- something never said: nothing is found, Chat is told so, nothing is stored ---------------------------------
    system, _ = turn("还记得我上次跟你说的那家拉面店叫什么吗")
    assert NOT_FOUND_LINE in system and "相关记忆（来自记忆库" not in system
    assert not db.fetch_all("SELECT id FROM event_memories WHERE content LIKE '%拉面%'")
