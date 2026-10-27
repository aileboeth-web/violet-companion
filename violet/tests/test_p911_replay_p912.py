"""Phase 9.1.2: the real session p911-real-1 replayed through the real runtime (offline, fictional content).

Same messages and simulated dates (2026-11-02, then ten days later), the memory model making the calls the real one
made (the cat saved twice -- as a profile fact and as a memory of kind=fact; the agreement linked to the interview with
only a date; the correction by fact_id; retract_memory forget=true), Intent routing the forget request as a task as
the real one did. Checked: what is stored after each round and what the Chat request of round 5 contains."""

from __future__ import annotations

import json

from tests.fakes import completion, intent_json
from tests.test_consistency_forget_p912 import CAT_MEMORY, CAT_NEW, CAT_OLD, CORRECT, EX, FORGET, INTERVIEW, SAY
from tests.test_long_term_memory_p91 import MemoryModel, at, clock, recall_lines, save  # noqa: F401  (clock: fixture)
from tests.test_phase2_scenarios import make_world, say  # noqa: F401  (fixture)

AGREEMENT = "用户和 Violet 约定：等用户 2026-11-11 下午 15:00 的实习面试结束后，一起云看《海上钢琴师》，具体时间由用户挑。"


def tool(name, **arguments):
    return {"name": name, "arguments": arguments}


def test_p911_real_session_replayed_with_the_fixes(make_world, clock):
    w = make_world()
    db = w.db
    mem = MemoryModel(db)
    w.llm.responders["fake-memory"] = mem

    def intent(kw):
        said = next(m["content"] for m in reversed(kw["messages"]) if m["role"] == "user")
        if "忘掉" in said:                     # the real Intent made it a task
            return intent_json("task", task={"exists": True, "name": "删除前男友相关记忆",
                                             "description": "用户要求忘掉关于前男友的那件事"})
        if "好久没聊" in said and not any(m["role"] == "tool" for m in kw["messages"]):
            return completion(tool_calls=[tool("query_memory", query="猫 名字"), tool("query_memory", query="约定 云看电影")])
        return intent_json("chat")
    w.llm.responders["fake-intent"] = intent
    chats: list[dict] = []
    w.llm.responders["fake-chat"] = lambda kw: chats.append(kw) or "嗯嗯"

    def turn(text):
        before = len(chats)
        say(w, text)
        return chats[before]

    # --- round 1: the cat (twice), the ex, the 15:00 interview ------------------------------------------------------
    mem.plan = [("猫叫年糕", [[
        save(summary=INTERVIEW, kind="event", category="event", progress="planned", event_date="2026-11-11",
             event_clock="15:00", importance=4, emotional_weight=4),
        save(summary=CAT_MEMORY, category="personal_info", kind="fact", importance=3),
        save(summary=EX, category="relationship", kind="event", progress="done", event_date="2026-10", importance=4),
        tool("remember_profile_fact", key="other", value=CAT_OLD, evidence="我家的猫叫年糕，两岁了，特别黏人"),
    ]])]
    turn(SAY)
    interview, cat, ex = mem.find("实习面试"), mem.find("年糕"), mem.find("前男友")
    assert db.get_event_memory(ex)["event_time"] is None                         # "2026-10": no time is made up
    cat_fact = db.fetch_one("SELECT id FROM user_profile_facts WHERE value = ?", (CAT_OLD,))["id"]

    # --- round 2: the agreement linked to the interview with only its date ---------------------------------------
    mem.plan = [("海上钢琴师", [lambda r: [save(summary=AGREEMENT, kind="event", category="event", progress="planned",
                                                 related_to=interview, event_date="2026-11-11")]])]
    turn("说好了哦，等我面试完，我们一起云看《海上钢琴师》")
    assert db.get_event_memory(interview)["event_time"] == "2026-11-11T15:00:00+08:00"

    # --- round 3: the correction (by fact_id, as the real model did) reaches the memory copy too -----------------
    mem.plan = [("更正", [[tool("correct_profile_fact", key="other", fact_id=cat_fact, new_value=CAT_NEW, evidence=CORRECT)]])]
    mem.results.clear()
    turn(CORRECT)
    assert mem.results[-1]["action"] == "corrected" and mem.results[-1]["also_corrected_memory_ids"] == [cat]
    assert db.get_event_memory(cat)["content"] == "用户家养了一只猫，名字叫汤圆，两岁，特别黏人。"
    assert [f["value"] for f in db.fetch_all("SELECT value FROM user_profile_facts WHERE status = 'active'")] == [CAT_NEW]

    # --- round 4: "忘掉吧" is done before the reply; no Performer task; the memory model does not act twice ---------
    tasks = db.fetch_one("SELECT COUNT(*) AS n FROM task_sessions")["n"]
    mem.plan = [("忘掉", [lambda r: [tool("retract_memory", memory_id=ex, reason="用户要求忘掉", evidence=FORGET, forget=True),
                                    tool("set_interaction_preference", key="avoid_topic", value="前男友/前任相关的事，不要主动提起",
                                         evidence=FORGET)]])]
    mem.results.clear()
    request = turn(FORGET)
    system = "\n".join(m.get("content") or "" for m in request["messages"] if m["role"] == "system")
    assert "【遗忘】用户要你忘掉的那件事已经处理好了" in system
    assert db.fetch_one("SELECT COUNT(*) AS n FROM task_sessions")["n"] == tasks and w.llm.calls_for("fake-performer") == []
    tool_results = [json.loads(m["content"]) for c in w.llm.calls_for("fake-memory")[-2:] for m in c["messages"]
                    if m["role"] == "tool"]
    assert db.get_event_memory(ex) is None and any(r.get("action") == "already_settled" for r in tool_results)
    first = db.fetch_one("SELECT content FROM messages ORDER BY id LIMIT 1")["content"]
    assert "前男友" not in first and "走出来" not in first and "年糕" in first and "15:00" in first

    # --- round 5, ten days later: the corrected name, 15:00, nothing of what was forgotten ------------------------
    clock.advance_to(at("2026-11-12T20:05:00+08:00"))
    for _ in range(10):
        db.decay_event_memories()
    mem.plan = []
    request = turn("好久没聊啦！你还记得我家猫叫什么吗？还有我们之前约好面试完要一起做的事，你还记得吗？")
    system = "\n".join(m.get("content") or "" for m in request["messages"] if m["role"] == "system")
    history = "\n".join(m.get("content") or "" for m in request["messages"] if m["role"] in ("user", "assistant"))
    assert "名叫汤圆" in system and "名字叫汤圆" in system                         # profile and memory: the new name
    stale = [line for line in system.splitlines() if "年糕" in line]
    # no stored fact says 年糕 any more; the only place left is the relationship section quoting the user's own first
    # message (raw words, like the dialogue history -- not a stored fact; documented in bugs-and-risks)
    assert len(stale) == 1 and stale[0].startswith("- 对方 11-02 20:00 说过“跟你说几件事：我家的猫叫年糕")
    lines = recall_lines(system, interview)
    assert lines and "15:00" in lines[0] and "进展未知" in lines[0] and "23:59" not in system
    for text in (system, history):
        assert "分手" not in text and "走出来" not in text
    assert "我家的猫叫年糕" in history          # the user's own words in the dialogue stay; they are not a stored fact
