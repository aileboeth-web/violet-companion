"""Phase 9.1.2: cross-store consistency of a correction (D-P911-R1) and forgetting before the reply (D-P911-R2).

Offline: isolated DB, the real stores, agents' tool handlers and pipeline with the fake SDK. Samples are the (fictional)
sentences of the real session p911-real-1: the cat 年糕 → 汤圆, the ex, the 15:00 interview."""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.fakes import completion, intent_json
from tests.test_memory_fixes_p911 import turn

CAT_OLD = "养了一只猫，名叫年糕，两岁，特别黏人"
CAT_NEW = "养了一只猫，名叫汤圆，两岁，特别黏人"
CAT_MEMORY = "用户家养了一只猫，名字叫年糕，两岁，特别黏人。"
SAY = "跟你说几件事：我家的猫叫年糕，两岁了，特别黏人；还有我上个月刚和前男友分手了，不过现在已经走出来了；另外下周三下午三点（15:00）我有个实习面试，好紧张"
CORRECT = "对了更正一下，我家猫其实叫汤圆，不叫年糕，之前是我打错字了"
FORGET = "前男友那件事你就忘掉吧，别记着了"
EX = "用户在上个月（2026年10月）刚和前男友分手，用户说自己现在已经走出来了。"
INTERVIEW = "用户下周三（2026-11-11）下午15:00有一场实习面试，用户表示很紧张。"


@pytest.fixture()
def facts(violetdb):
    from ai.profile_store import Profile
    return Profile(violetdb).facts


def correct_cat(facts, fact_id):
    return facts.correct("other", CAT_NEW, fact_id=fact_id, evidence=CORRECT, user_message=CORRECT)


# ----------------------------------------------------------------------------------------------- D-P911-R1

@pytest.mark.parametrize("old,new,expected", [
    (CAT_OLD, CAT_NEW, ("年糕", "汤圆")),
    ("家里养了一只柯基，名叫豆豆，三岁", "家里养了一只柯基，名叫豆包，三岁", ("豆豆", "豆包")),   # one character widened
    ("喜欢打羽毛球", "喜欢打网球", ("羽毛", "网")),
    ("在星河大学读大二", "已经毕业了，在上海上班", None),                                     # a rewrite: not one part
    ("养了一只猫，名叫年糕，两岁", "养了两只猫，名叫汤圆，两岁", None),                       # two changes: not one part
    ("养了一只猫，名叫年糕", "养了一只猫", None),                                             # a deletion is no correction
])
def test_the_changed_part_is_found_only_for_one_short_replacement(old, new, expected):
    from ai.memory_consistency import changed_segment
    assert changed_segment(old, new) == expected


def test_the_same_fact_stored_as_a_memory_follows_the_profile_correction(violetdb, facts):
    """The real state: the cat as profile fact 1 AND as event memory 2 (kind=fact)."""
    fact = facts.remember("other", CAT_OLD, evidence="我家的猫叫年糕，两岁了，特别黏人", user_message=SAY)["fact"]
    copy = violetdb.add_event_memory(CAT_MEMORY, category="personal_info", kind="fact", source_message_ids=[1])
    story = violetdb.add_event_memory("用户的猫年糕把花盆打翻了，用户很无奈", kind="event", progress="done")
    snack = violetdb.add_event_memory("用户周末吃了桂花年糕", kind="event", progress="done")
    shy = violetdb.add_event_memory("用户的猫年糕很怕生", kind="fact")          # a fact, but not certainly the same one
    core = violetdb.add_core_memory("user_pet", "user_profile", "用户养了一只猫，叫年糕")
    result = correct_cat(facts, fact["id"])
    assert result["action"] == "corrected"
    assert result["propagated"]["updated"] == [{"table": "event_memories", "id": copy}]
    assert {(c["table"], c["id"]) for c in result["propagated"]["conflicts"]} == {("event_memories", story),
                                                                                    ("event_memories", shy),
                                                                                    ("core_memories", core)}
    assert violetdb.get_event_memory(shy)["content"] == "用户的猫年糕很怕生"                          # flagged, not rewritten
    assert violetdb.get_event_memory(copy)["content"] == "用户家养了一只猫，名字叫汤圆，两岁，特别黏人。"
    revision = violetdb.list_memory_revisions("event_memories", copy)[-1]
    assert (revision["action"], revision["source"], revision["old_content"]) == ("correct", "user_confirmed", CAT_MEMORY)
    assert violetdb.get_event_memory(story)["content"] == "用户的猫年糕把花盆打翻了，用户很无奈"      # a record: not rewritten
    assert violetdb.get_event_memory(snack)["content"] == "用户周末吃了桂花年糕"                        # not about the cat
    assert violetdb.list_memory_revisions("event_memories", snack) == []
    audit = json.loads(facts.audit.entries(entity="profile_fact")[0]["detail"])
    assert audit["propagated"]["updated"] == [{"table": "event_memories", "id": copy}] and "年糕" not in json.dumps(audit, ensure_ascii=False)


def test_after_the_correction_chat_gets_no_stale_valid_fact(violetdb, facts):
    from ai.chat import format_recalled_memories
    from ai.context_engine import ContextEngine
    fact = facts.remember("other", CAT_OLD, evidence="我家的猫叫年糕，两岁了，特别黏人", user_message=SAY)["fact"]
    copy = violetdb.add_event_memory(CAT_MEMORY, category="personal_info", kind="fact")
    story = violetdb.add_event_memory("用户的猫年糕把花盆打翻了，用户很无奈", kind="event", progress="done")
    violetdb.add_core_memory("user_pet", "user_profile", "用户养了一只猫，叫年糕")
    correct_cat(facts, fact["id"])
    recalled = [violetdb.get_event_memory(copy), violetdb.get_event_memory(story)]
    lines = format_recalled_memories(recalled)
    assert f"id={copy}; [长期事实] 用户家养了一只猫，名字叫汤圆" in lines
    assert "其中“年糕”已被用户纠正为“汤圆”" in next(l for l in lines.splitlines() if l.startswith(f"- id={story};"))
    packet = ContextEngine(violetdb).build("chat", "你还记得我家猫叫什么吗？", {"recalled_memories": recalled}, with_history=False)
    sections = "\n".join(packet.sections)
    for line in sections.splitlines():
        if "年糕" in line:                                  # only ever next to the correction it was overruled by
            assert "已被用户纠正为“汤圆”" in line, line
    # once someone fixes the record (here the dashboard), the conflict no longer applies
    violetdb.apply_event_update(story, content="用户的猫汤圆把花盆打翻了，用户很无奈", source="dashboard", action="correct")
    assert "已被用户纠正" not in format_recalled_memories([violetdb.get_event_memory(story)])


def test_a_multi_valued_key_keeps_its_other_values(violetdb, facts):
    movies = facts.remember("interest", "喜欢看电影", evidence="我喜欢看电影", user_message="我喜欢看电影")["fact"]
    sport = facts.remember("interest", "喜欢打羽毛球", evidence="我喜欢打羽毛球", user_message="我喜欢打羽毛球")["fact"]
    sport_copy = violetdb.add_event_memory("用户喜欢打羽毛球", kind="fact")
    movie_copy = violetdb.add_event_memory("用户喜欢看电影", kind="fact")
    result = facts.correct("interest", "喜欢打网球", fact_id=sport["id"], evidence="其实我喜欢打网球",
                           user_message="说错了，其实我喜欢打网球")
    assert result["action"] == "corrected"
    assert sorted(f["value"] for f in facts.active_facts()) == ["喜欢打网球", "喜欢看电影"]
    assert facts.get_fact(movies["id"])["status"] == "active"
    assert violetdb.get_event_memory(sport_copy)["content"] == "用户喜欢打网球"
    assert violetdb.get_event_memory(movie_copy)["content"] == "用户喜欢看电影"


def test_correcting_the_memory_corrects_the_profile_copy_too(violetdb, facts):
    from ai.agents import MemoryAgent
    fact = facts.remember("other", CAT_OLD, evidence="我家的猫叫年糕，两岁了，特别黏人", user_message=SAY)["fact"]
    copy = violetdb.add_event_memory(CAT_MEMORY, category="personal_info", kind="fact")
    with turn(CORRECT, "t3", 9):
        result = json.loads(MemoryAgent()._tool_correct_memory({
            "memory_id": copy, "corrected_summary": "用户家养了一只猫，名字叫汤圆，两岁，特别黏人。",
            "reason": "用户纠正猫名", "evidence": CORRECT}))
    assert result["ok"] and result["profile_corrections"] == [{"fact_id": fact["id"], "action": "corrected"}]
    assert [f["value"] for f in facts.active_facts()] == [CAT_NEW]


def test_the_check_tool_lists_old_conflicts_and_changes_nothing(violetdb, facts, tmp_path):
    import shutil
    from tools.memory_conflicts import main
    clean = tmp_path / "clean.db"
    violetdb.conn.execute("PRAGMA wal_checkpoint(FULL)")
    shutil.copy(violetdb.db_path, clean)
    assert main(["--db", str(clean)], out=lambda s: None) == 0
    # what 9.1.0 / 9.1.1 left behind: two active dog facts; a superseded cat fact whose old name is still in a memory
    dog1 = facts.remember("other", "家里养了一只柯基，名叫豆豆", evidence="柯基叫豆豆", user_message="我家柯基叫豆豆")["fact"]
    dog2 = facts.remember("other", "家里养了一只柯基，名叫豆包", evidence="叫豆包", user_message="其实叫豆包")["fact"]
    old = facts.remember("other", CAT_OLD, evidence="我家的猫叫年糕，两岁了，特别黏人", user_message=SAY)["fact"]
    facts.correct("other", CAT_NEW, fact_id=old["id"], evidence=CORRECT, user_message=CORRECT, propagate=False)
    stale = violetdb.add_event_memory(CAT_MEMORY, kind="fact")
    copy = tmp_path / "copy.db"
    violetdb.conn.execute("PRAGMA wal_checkpoint(FULL)")
    shutil.copy(violetdb.db_path, copy)
    before = hashlib.sha256(copy.read_bytes()).hexdigest()
    printed: list[str] = []
    assert main(["--db", str(copy), "--json"], out=printed.append) == 1
    found = json.loads(printed[0])
    assert {"kind": "duplicate_active_facts", "fact_ids": [dog1["id"], dog2["id"]], "key": "other", "old": "豆豆",
            "new": "豆包"} in found
    assert any(f["kind"] == "stale_copy" and f["id"] == stale and f["old"] == "年糕" for f in found)
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == before


# ----------------------------------------------------------------------------------------------- D-P911-R2

@pytest.mark.parametrize("text,asked", [
    (FORGET, True), ("当我没说", True), ("把那条记录删了吧", True), ("这件事别记着了", True),
    ("别忘了我们的约定", False), ("我忘了带伞", False), ("那张照片删掉吧", False), ("你可不要忘掉哦", False),
])
def test_what_counts_as_a_request_to_forget(text, asked):
    from ai.memory_forget import is_forget_request
    assert is_forget_request(text) is asked


@pytest.fixture()
def first_turn(violetdb):
    """Round 1 of p911-real-1 as stored, plus the failed task the Performer wrote in round 4."""
    user = violetdb.add_message("user", "incoming", "text", SAY)
    violetdb.add_message("assistant", "outgoing", "text", "年糕这个名字好可爱，两岁正是最黏人的时候吧")
    ex_reply = violetdb.add_message("assistant", "outgoing", "text", "嗯，能走出来就好")
    violetdb.add_message("assistant", "outgoing", "text", "面试的事，紧张挺正常的，还有好几天呢，慢慢准备就好啦")
    violetdb.add_inbound_message("p911-r1", "wx-1", 0, 1, SAY)
    ids = {
        "user": user, "ex_reply": ex_reply,
        "interview": violetdb.add_event_memory(INTERVIEW, kind="event", progress="planned", event_time="2026-11-11T15:00:00+08:00",
                                            source_message_ids=[user]),
        "cat": violetdb.add_event_memory(CAT_MEMORY, kind="fact", source_message_ids=[user]),
        "ex": violetdb.add_event_memory(EX, kind="event", progress="done", source_message_ids=[user]),
    }
    ids["task"] = violetdb.create_task_session(task_name="删除前男友相关记忆", task_description="用户要求忘掉前男友的事",
                                            task_info={"target_memories": [EX], "request": "还有我上个月刚和前男友分手了"},
                                            status="pending_info")
    violetdb.update_task_session(task_id=ids["task"], status="failed", result={"target_memories": [EX]})
    return ids


def test_the_request_is_carried_out_before_the_reply_and_only_its_target_goes(violetdb, first_turn):
    from ai.memory_forget import forget_note, settle_forget_request, settled_forget
    outcome = settle_forget_request(violetdb, FORGET, "t4")
    assert outcome["status"] == "forgotten" and outcome["memory_id"] == first_turn["ex"]
    assert violetdb.get_event_memory(first_turn["ex"]) is None
    assert violetdb.get_event_memory(first_turn["cat"]) and violetdb.get_event_memory(first_turn["interview"])
    text = violetdb.fetch_one("SELECT content FROM messages WHERE id = ?", (first_turn["user"],))["content"]
    assert "前男友" not in text and "走出来" not in text and "年糕" in text and "15:00" in text
    task = violetdb.fetch_one("SELECT * FROM task_sessions WHERE id = ?", (first_turn["task"],))
    assert all("分手" not in (task[c] or "") for c in ("task_info", "result"))       # the verbatim copies are gone
    for row in violetdb.fetch_all("SELECT content FROM inbound_messages"):
        assert "前男友分手" not in (row["content"] or "")
    assert settled_forget(violetdb, "t4") == {"status": "forgotten", "memory_id": first_turn["ex"]}
    entry = violetdb.fetch_one("SELECT detail FROM profile_audit_log WHERE action = 'forget'")
    assert json.loads(entry["detail"])["turn_id"] == "t4" and "分手" not in entry["detail"]
    assert "已经处理好了" in forget_note(outcome) and "不要说做不到" in forget_note(outcome)


@pytest.mark.parametrize("text,status", [("那件事你就忘掉吧", "not_found")])
def test_nothing_certain_nothing_deleted_and_the_memory_model_keeps_to_it(violetdb, first_turn, text, status):
    from ai.agents import MemoryAgent
    from ai.memory_forget import forget_note, settle_forget_request
    outcome = settle_forget_request(violetdb, text, "t5")
    assert outcome["status"] == status and "什么都没有删除" in forget_note(outcome)
    with turn(text, "t5"):
        later = json.loads(MemoryAgent()._tool_retract_memory({"memory_id": first_turn["ex"], "reason": "用户要求",
                                                               "evidence": text, "forget": True}))
    assert later["action"] == "already_settled" and violetdb.get_event_memory(first_turn["ex"])["status"] == "active"


def test_two_equally_likely_memories_are_ambiguous(violetdb):
    from ai.memory_forget import settle_forget_request
    a = violetdb.add_event_memory("用户和前男友去过一次海边", kind="event", progress="done")
    b = violetdb.add_event_memory("用户的前男友喜欢打游戏", kind="event", progress="done")
    outcome = settle_forget_request(violetdb, "前男友的事都忘掉吧", "t6")
    assert outcome["status"] == "ambiguous"
    assert violetdb.get_event_memory(a)["status"] == violetdb.get_event_memory(b)["status"] == "active"


def pipeline_world(make_world, route="task"):
    w = make_world()
    w.llm.responders["fake-intent"] = lambda kw: intent_json(route, task={
        "exists": route == "task", "name": "删除前男友相关记忆", "description": "用户要求忘掉前男友的事"})
    chats: list = []
    w.llm.responders["fake-chat"] = lambda kw: chats.append(kw) or "好，已经忘掉啦"
    memory_results: list = []

    def memory(kw):
        tools = [json.loads(m["content"]) for m in kw["messages"] if m["role"] == "tool"]
        memory_results.extend(tools[-1:])
        if not tools and "忘掉" in kw["messages"][-1]["content"]:
            ex = w.db.fetch_one("SELECT id FROM event_memories WHERE content LIKE '%前男友%'")
            return completion(tool_calls=[{"name": "retract_memory", "arguments": {
                "memory_id": ex["id"] if ex else 999, "reason": "用户要求忘掉", "evidence": FORGET, "forget": True}}])
        return "整理完了"
    w.llm.responders["fake-memory"] = memory
    return w, chats, memory_results


def system_of(kw) -> str:
    return "\n".join(m.get("content") or "" for m in kw["messages"] if m["role"] == "system")


def test_in_the_pipeline_no_failed_task_and_the_reply_is_told_the_truth(make_world, first_turn):
    from tests.test_phase2_scenarios import say
    w, chats, memory_results = pipeline_world(make_world)
    tasks_before = w.db.fetch_one("SELECT COUNT(*) AS n FROM task_sessions")["n"]
    say(w, FORGET)
    assert w.db.fetch_one("SELECT COUNT(*) AS n FROM task_sessions")["n"] == tasks_before    # no Performer task
    assert w.llm.calls_for("fake-performer") == []
    system = system_of(chats[-1])
    assert "【遗忘】用户要你忘掉的那件事已经处理好了" in system and EX not in system
    history = "\n".join(m.get("content") or "" for m in chats[-1]["messages"] if m["role"] in ("user", "assistant"))
    assert "前男友分手" not in history and "走出来" not in history and "年糕" in history
    assert w.db.get_event_memory(first_turn["ex"]) is None
    assert any(r.get("action") == "already_settled" for r in memory_results)     # the memory model did not act again


def test_in_the_pipeline_a_failure_is_reported_as_a_failure(make_world, first_turn, monkeypatch):
    import ai.memory_forget as mf
    from tests.test_phase2_scenarios import say
    w, chats, memory_results = pipeline_world(make_world)

    def broken(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(mf, "forget_event_memory", broken)
    say(w, FORGET)
    system = system_of(chats[-1])
    assert "处理时出错了，这次什么都没有删除" in system and "已经处理好了" not in system
    assert w.db.get_event_memory(first_turn["ex"])["status"] == "active"          # really not deleted
    assert any(r.get("action") == "already_settled" and r.get("status") == "failed" for r in memory_results)


from tests.test_phase2_scenarios import make_world  # noqa: E402,F401  (fixture)
