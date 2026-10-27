"""Phase 9.1.1: the defects the Phase 9.1 real test (local/free_chat/mem91-real-1) confirmed -- offline, isolated DB,
fictional content, the real stores and agents' tool handlers (no model).

D-P91-2  a correction ("叫豆包不叫豆豆") left the old profile fact active next to the new one
D-P91-1  merging a new agreement into the interview memory dropped 15:00 and stored 23:59
P1       a deleted memory's source words stayed in the chat history; forgetting is now a separate operation
The samples are the real rows of that session (values and the user's words verbatim)."""

from __future__ import annotations

import contextlib
import json

import pytest

DOG_OLD = "家里养了一只柯基，名叫豆豆，三岁，很爱拆家"
DOG_NEW = "家里养了一只柯基，名叫豆包，三岁，很爱拆家"
SAY_DOG = "跟你说两件事：我家养了只柯基叫豆豆，三岁了，特别能拆家；还有我上个月刚和前任分手了，不过现在已经好多了"
CORRECTION = "对了更正一下，我家柯基其实叫豆包，不叫豆豆，之前是我打错字了"
INTERVIEW = "用户有一场实习面试，时间是2026-11-11（下周三）下午15:00，用户表示很紧张。"
MOVIE_MERGED = "用户和 Violet 约定：等 2026-11-11 下午的实习面试结束后，一起云看《海上钢琴师》。"
BREAKUP = "用户在2026年10月与前任分手，表示现在已经好多了。"


@contextlib.contextmanager
def turn(user_message: str, turn_id: str = "t", message_id: int | None = None):
    from ai.agents.base_agent import _CALL_CONTEXT
    token = _CALL_CONTEXT.set({"user_message": user_message, "turn_id": turn_id, "user_message_id": message_id})
    try:
        yield
    finally:
        _CALL_CONTEXT.reset(token)


@pytest.fixture()
def facts(violetdb):
    from ai.profile_store import Profile
    return Profile(violetdb).facts


@pytest.fixture()
def dog(facts):
    return facts.remember("other", DOG_OLD, evidence="我家养了只柯基叫豆豆，三岁了，特别能拆家", user_message=SAY_DOG)["fact"]


def statuses(facts):
    return [(f["id"], f["value"], f["status"], f["supersedes_id"], f["superseded_by"]) for f in facts.list_facts(fact_key="other")]


def audit_actions(facts):
    return [(e["action"], json.loads(e["detail"])) for e in reversed(facts.audit.entries(entity="profile_fact"))]


# ----------------------------------------------------------------------------------------------- D-P91-2 corrections

@pytest.mark.parametrize("old_value", ["豆豆", DOG_OLD, "名叫豆豆"])
def test_a_part_of_the_old_value_is_enough_to_correct_it(facts, dog, old_value):
    result = facts.correct("other", DOG_NEW, old_value=old_value, evidence=CORRECTION, user_message=CORRECTION)
    new = result["fact"]["id"]
    assert result["action"] == "corrected" and result["superseded"] == [dog["id"]]
    assert statuses(facts) == [(dog["id"], DOG_OLD, "superseded", None, new), (new, DOG_NEW, "active", dog["id"], None)]
    assert audit_actions(facts)[-1] == ("correct", {"superseded": [dog["id"]], "reused_existing": False, "via": "old_value"})


@pytest.mark.parametrize("old_value", [None, "柯基叫豆豆"])
def test_a_multi_valued_key_without_a_clear_target_writes_nothing(facts, dog, old_value):
    """The real run: neither a fact id nor a matching part -- it used to add a second active fact and say ok."""
    result = facts.correct("other", DOG_NEW, old_value=old_value, evidence=CORRECTION, user_message=CORRECTION)
    assert result["action"] == "ambiguous" and [c["id"] for c in result["candidates"]] == [dog["id"]]
    assert statuses(facts) == [(dog["id"], DOG_OLD, "active", None, None)]
    assert audit_actions(facts)[-1][0] == "correct_ambiguous"


def test_the_fact_id_corrects_and_a_repeated_correction_only_confirms(facts, dog):
    first = facts.correct("other", DOG_NEW, fact_id=dog["id"], evidence=CORRECTION, user_message=CORRECTION)
    again = facts.correct("other", DOG_NEW, fact_id=dog["id"], evidence=CORRECTION, user_message=CORRECTION)
    assert first["action"] == "corrected" and again["action"] == "confirmed" and again["reason"] == "already_corrected"
    assert [f["value"] for f in facts.active_facts()] == [DOG_NEW]
    assert [a for a, _ in audit_actions(facts)] == ["create", "correct", "correct_confirmed"]


def test_the_broken_state_of_the_real_session_can_be_repaired(facts, dog):
    """mem91-real-1 ended with fact 1 (豆豆) and fact 2 (豆包) both active; correcting 1 links it to 2, no third row."""
    second = facts.remember("other", DOG_NEW, evidence=CORRECTION, user_message=CORRECTION)["fact"]
    assert len(facts.active_facts()) == 2
    result = facts.correct("other", DOG_NEW, fact_id=dog["id"], evidence=CORRECTION, user_message=CORRECTION)
    assert result["action"] == "corrected" and result["fact"]["id"] == second["id"]
    assert statuses(facts) == [(dog["id"], DOG_OLD, "superseded", None, second["id"]),
                               (second["id"], DOG_NEW, "active", dog["id"], None)]


@pytest.mark.parametrize("kwargs, reason", [
    ({"fact_id": 999}, "fact_not_found"),
    ({"old_value": "豆豆", "evidence": "我家狗叫豆包"}, "evidence_not_in_user_message"),
    ({"old_value": "豆豆", "new_value": ""}, "empty_value"),
])
def test_failures_write_nothing_and_say_why(facts, dog, kwargs, reason):
    args = {"key": "other", "new_value": DOG_NEW, "evidence": CORRECTION, "user_message": CORRECTION, **kwargs}
    result = facts.correct(args.pop("key"), args.pop("new_value"), **args)
    assert result["action"] == "failed" and result["reason"] == reason
    assert statuses(facts) == [(dog["id"], DOG_OLD, "active", None, None)]
    assert audit_actions(facts)[-1][0] == "correct_failed"


def test_nothing_stored_under_the_key_is_saved_as_new_and_audited_as_such(facts):
    result = facts.correct("study.major", "物理", old_value="天文", evidence="其实我是学物理的",
                           user_message="其实我是学物理的")
    assert result["action"] == "failed" and result["reason"] == "old_value_not_found"
    result = facts.correct("study.major", "物理", evidence="其实我是学物理的", user_message="其实我是学物理的")
    assert result["action"] == "created" and audit_actions(facts)[-1][0] == "correct_created"


def test_a_single_valued_key_still_corrects_without_naming_the_old_fact(facts):
    facts.remember("study.major", "天文", evidence="我学天文的", user_message="我学天文的")
    result = facts.correct("study.major", "物理", evidence="其实我是学物理的", user_message="其实我是学物理的")
    assert result["action"] == "corrected"
    assert [(f["value"], f["status"]) for f in facts.list_facts(fact_key="study.major")] == [("天文", "superseded"),
                                                                                            ("物理", "active")]


def test_after_the_correction_only_the_new_fact_reaches_chat(violetdb, facts, dog):
    from ai.context_engine import ContextEngine
    facts.correct("other", DOG_NEW, old_value="豆豆", evidence=CORRECTION, user_message=CORRECTION)
    packet = ContextEngine(violetdb).build("chat", "好久没聊啦！你还记得我家狗叫什么吗？", {}, with_history=False)
    profile = next(s for s in packet.sections if s.startswith(("【你了解的对方", "【用户档案")))
    assert "豆包" in profile and "豆豆" not in profile and packet.report["fact_ids"] == [dog["id"] + 1]


def test_the_memory_tool_returns_candidates_and_does_not_end_the_job_on_ambiguity(violetdb, dog):
    from ai.agents import MemoryAgent
    from ai.agents.memory_agent import write_step_succeeded
    agent = MemoryAgent()
    with turn(CORRECTION):
        raw = agent._tool_correct_profile_fact({"key": "other", "new_value": DOG_NEW, "evidence": CORRECTION})
        unclear = json.loads(raw)
        assert unclear["ok"] is False and unclear["action"] == "ambiguous" and not write_step_succeeded("correct_profile_fact", raw)
        assert unclear["candidates"] == [{"id": dog["id"], "key": "other", "value": DOG_OLD}]
        raw = agent._tool_correct_profile_fact({"key": "other", "new_value": DOG_NEW, "fact_id": dog["id"],
                                               "evidence": CORRECTION})
    done = json.loads(raw)
    assert done["ok"] is True and done["action"] == "corrected" and done["superseded_ids"] == [dog["id"]]
    assert write_step_succeeded("correct_profile_fact", raw)


def test_the_dashboard_correction_of_a_superseded_fact_is_refused(facts, dog, violetdb, tmp_path):
    import os
    from wx_ilink_py import ILinkClient, create_app
    facts.correct("other", DOG_NEW, fact_id=dog["id"], evidence=CORRECTION, user_message=CORRECTION)
    result = facts.correct_from_dashboard(dog["id"], "家里养了一只柯基，名叫团子")
    assert result["action"] == "failed" and result["reason"] == "fact_not_active:superseded"
    token = os.environ["VIOLET_ADMIN_TOKEN"]
    app = create_app(client=ILinkClient(data_dir=tmp_path / "data", auto_login=False), violetdb=violetdb,
                     proactive_scheduler=None, all_agents=[], admin_token=token)
    resp = app.test_client().patch(f"/api/profile/facts/{dog['id']}", json={"value": "家里养了一只柯基，名叫团子"},
                                   headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400 and resp.get_json()["action"] == "failed"
    assert [f["value"] for f in facts.active_facts()] == [DOG_NEW]


# ----------------------------------------------------------------------------------------------- D-P91-1 merges / time

def save(agent, **args):
    return json.loads(agent._tool_save_memory({"category": "event", **args}))


def test_the_agreement_merged_into_the_interview_keeps_15_00_and_never_gets_23_59(violetdb):
    """The real calls: round 2 saved the interview at 15:00; round 3 linked the agreement to it with only the date."""
    from ai.agents import MemoryAgent
    agent = MemoryAgent()
    with turn("下周三下午三点我有个实习面试，好紧张啊", "r2", 4):
        first = save(agent, summary=INTERVIEW, kind="event", progress="planned", event_date="2026-11-11",
                     event_clock="15:00", importance=4)
    with turn("说好了哦，等我面试完，我们一起云看《海上钢琴师》", "r3", 7):
        merged = save(agent, summary=MOVIE_MERGED, kind="event", related_to=first["memory_id"], event_date="2026-11-11")
    memory = violetdb.get_event_memory(first["memory_id"])
    assert merged["action"] == "updated" and memory["event_time"] == "2026-11-11T15:00:00+08:00"
    assert memory["progress"] == "planned"                      # no progress given: not reset to "unknown"
    assert memory["content"] == MOVIE_MERGED                    # 15:00 is held by event_time; nothing to add
    assert memory["source_message_ids"] == [4, 7]
    times = [v for r in violetdb.fetch_all("SELECT event_time, valid_until FROM event_memories") for v in r.values() if v]
    assert times and not any("T23:59" in v for v in times)


def test_a_detail_the_new_text_and_the_new_time_both_lose_is_written_back(violetdb):
    from ai.agents import MemoryAgent
    agent = MemoryAgent()
    with turn("下周三下午三点我有个实习面试", "r2", 4):
        first = save(agent, summary=INTERVIEW, kind="event", progress="planned", event_date="2026-11-11",
                     event_clock="15:00")
    with turn("面试完那天我们一起看电影吧", "r3", 7):
        merged = save(agent, summary="用户和Eve约好11月12日一起云看《海上钢琴师》", kind="event",
                      related_to=first["memory_id"], event_date="2026-11-12")
    memory = violetdb.get_event_memory(first["memory_id"])
    assert memory["event_time"] == "2026-11-12"
    assert memory["content"] == "用户和Eve约好11月12日一起云看《海上钢琴师》（原记录：2026-11-11、下午15:00）"
    assert merged["details_kept"] == ["2026-11-11", "下午15:00"]
    revision = violetdb.list_memory_revisions("event_memories", first["memory_id"])[-1]
    assert "保留原有细节" in revision["reason"] and "2026-11-11T15:00:00+08:00 → 2026-11-12" in revision["reason"]


def test_a_text_that_contradicts_the_stored_time_is_refused(violetdb):
    from ai.agents import MemoryAgent
    agent = MemoryAgent()
    with turn("下周三下午三点我有个实习面试", "r2", 4):
        first = save(agent, summary=INTERVIEW, kind="event", progress="planned", event_date="2026-11-11",
                     event_clock="15:00")
    with turn("面试改到下午四点了", "r4", 9):
        refused = json.loads(agent._tool_update_memory({"memory_id": first["memory_id"],
                                                        "summary": "用户的实习面试改到2026-11-11下午4点"}))
        moved = save(agent, summary="用户的实习面试改到2026-11-11下午4点", kind="event", related_to=first["memory_id"],
                     event_date="2026-11-11", event_clock="16:00")
    assert refused["ok"] is False and refused["action"] == "time_mismatch"
    memory = violetdb.get_event_memory(first["memory_id"])
    assert moved["ok"] is True and memory["event_time"] == "2026-11-11T16:00:00+08:00"
    assert memory["content"] == "用户的实习面试改到2026-11-11下午4点"   # the user named a new time: 15:00 may go


def test_a_day_without_a_clock_stays_a_day_and_passes_at_its_end(violetdb, monkeypatch):
    from ai.agents import MemoryAgent
    from ai.chat import format_recalled_memories
    from ai.memory_model import event_deadline
    with turn("下周三有个答辩", "r1", 1):
        saved = save(MemoryAgent(), summary="用户2026-11-11有一场答辩", kind="event", progress="planned",
                     event_date="2026-11-11")
    memory = violetdb.get_event_memory(saved["memory_id"])
    assert memory["event_time"] == "2026-11-11"
    assert "时间 2026-11-11（具体时刻不知道）" in format_recalled_memories([memory])
    assert event_deadline("2026-11-11").isoformat() == "2026-11-11T23:59:59+08:00"
    monkeypatch.setattr(violetdb, "now", lambda: "2026-11-11T23:30:00+08:00")
    violetdb.refresh_event_states()
    assert violetdb.get_event_memory(saved["memory_id"])["progress"] == "planned"
    monkeypatch.setattr(violetdb, "now", lambda: "2026-11-12T00:01:00+08:00")
    violetdb.refresh_event_states()
    assert violetdb.get_event_memory(saved["memory_id"])["progress"] == "unknown"   # passed: unknown, never done


def test_time_details_reads_chinese_clocks_without_taking_yidian_for_one_oclock():
    from ai.memory_model import dropped_time_details, time_details, time_mismatch
    got = time_details("有一点紧张，三点建议，下午三点半，晚上8点，凌晨两点，11月12日，2026-11-11 15:00")
    assert got["clocks"] == {"15:30": "下午三点半", "20:00": "晚上8点", "02:00": "凌晨两点", "15:00": "15:00"}
    assert got["dates"] == {"11-11": "2026-11-11", "11-12": "11月12日"}
    assert dropped_time_details(INTERVIEW, MOVIE_MERGED, "说好了哦") == ["下午15:00"]
    assert dropped_time_details(INTERVIEW, MOVIE_MERGED, "说好了哦", "2026-11-11T15:00:00+08:00") == []
    assert dropped_time_details(INTERVIEW, MOVIE_MERGED, "面试改到周四了") == []
    assert time_mismatch("下午4点面试", "2026-11-11T15:00:00+08:00") and not time_mismatch("下午三点面试", "2026-11-11T15:00:00+08:00")


# ----------------------------------------------------------------------------------------------- P1 delete vs forget

@pytest.fixture()
def first_turn(violetdb):
    """Turn 1 of mem91-real-1 as stored: the user's message, Violet's two lines, its memory job, the breakup memory."""
    user = violetdb.add_message("user", "incoming", "text", SAY_DOG)
    replies = [violetdb.add_message("assistant", "outgoing", "text", t) for t in
               ("三岁的柯基，正是精力最旺的时候吧，拆家估计拦都拦不住", "分手的事，能说出“好多了”，说明这段时间你自己慢慢扛过来了")]
    other = violetdb.add_message("user", "incoming", "text", "我朋友上周也分手了，她哭了好久")
    later_reply = violetdb.add_message("assistant", "outgoing", "text", "朋友分手了确实难受，你多陪陪她")
    job = violetdb.add_memory_job("t1", SAY_DOG, user, status="done")
    violetdb.execute("UPDATE memory_jobs SET assistant_reply = ? WHERE id = ?",
                  ("三岁的柯基，正是精力最旺的时候吧，拆家估计拦都拦不住\n分手的事，能说出“好多了”，说明这段时间你自己慢慢扛过来了", job))
    memory = violetdb.add_event_memory(BREAKUP, category="event", kind="event", progress="done", source_message_ids=[user])
    return {"user": user, "replies": replies, "other": other, "later_reply": later_reply, "job": job, "memory": memory}


def content(violetdb, message_id):
    return violetdb.fetch_one("SELECT content FROM messages WHERE id = ?", (message_id,))["content"]


def test_delete_keeps_the_history_forget_removes_only_what_is_about_it(violetdb, first_turn):
    from ai.context_engine import ContextEngine
    from ai.memory_forget import forget_event_memory
    from ai.profile_store import REDACTED
    violetdb.retract_event_memory(first_turn["memory"], reason="管理后台删除", source="dashboard")
    history = " ".join(h["content"] for h in ContextEngine(violetdb).build("chat", "今天在地铁上碰到前任了，好尴尬").history)
    assert "前任分手" in history                                  # delete: the source words are still in the history
    report = forget_event_memory(violetdb, first_turn["memory"])
    assert report["messages_changed"] == [first_turn["user"], first_turn["replies"][1]]
    assert content(violetdb, first_turn["user"]) == f"跟你说两件事：我家养了只柯基叫豆豆，三岁了，特别能拆家；{REDACTED}"
    assert content(violetdb, first_turn["replies"][0]) == "三岁的柯基，正是精力最旺的时候吧，拆家估计拦都拦不住"
    assert content(violetdb, first_turn["replies"][1]) == REDACTED
    assert content(violetdb, first_turn["other"]) == "我朋友上周也分手了，她哭了好久"     # another turn: untouched
    assert content(violetdb, first_turn["later_reply"]) == "朋友分手了确实难受，你多陪陪她"
    job = violetdb.get_memory_job(first_turn["job"])
    assert "前任" not in job["user_message"] and "分手" not in job["assistant_reply"] and "柯基" in job["user_message"]
    assert violetdb.get_event_memory(first_turn["memory"]) is None
    assert violetdb.list_memory_revisions("event_memories", first_turn["memory"]) == []
    history = " ".join(h["content"] for h in ContextEngine(violetdb).build("chat", "今天在地铁上碰到前任了，好尴尬").history)
    assert "前任分手" not in history and "上个月" not in history and "豆豆" in history
    entry = next(e for e in violetdb.fetch_all("SELECT * FROM profile_audit_log WHERE action = 'forget'"))
    assert "前任" not in entry["detail"] and json.loads(entry["detail"])["messages_changed"] == report["messages_changed"]


def test_forget_without_a_recorded_source_says_the_history_was_not_touched(violetdb):
    from ai.memory_forget import forget_event_memory
    memory = violetdb.add_event_memory(BREAKUP, kind="event", progress="done")
    report = forget_event_memory(violetdb, memory)
    assert report["messages_changed"] == [] and "找不到这条记忆的来源消息" in report["history_note"]


def test_the_memory_model_forgets_only_on_the_users_explicit_request(violetdb, first_turn):
    from ai.agents import MemoryAgent
    agent = MemoryAgent()
    with turn("你记错了，我没分手"):
        refused = json.loads(agent._tool_retract_memory({"memory_id": first_turn["memory"], "reason": "记错",
                                                         "evidence": "你记错了，我没分手", "forget": True}))
    assert refused["action"] == "rejected" and violetdb.get_event_memory(first_turn["memory"])["status"] == "active"
    with turn("前任那件事你忘掉吧，别记着了"):
        done = json.loads(agent._tool_retract_memory({"memory_id": first_turn["memory"], "reason": "用户要求遗忘",
                                                      "evidence": "前任那件事你忘掉吧", "forget": True}))
    assert done["ok"] is True and done["action"] == "forgotten" and done["messages_changed"] == 2
    assert violetdb.get_event_memory(first_turn["memory"]) is None


def test_the_forget_route_needs_confirmation(admin, violetdb, first_turn):
    from tests.test_control_center_p90 import SAME
    assert admin.post(f"/api/control/memories/event/{first_turn['memory']}/forget", headers=SAME, json={}).status_code == 400
    assert admin.post("/api/control/memories/event/9999/forget", headers=SAME, json={"confirm": True}).status_code == 404
    resp = admin.post(f"/api/control/memories/event/{first_turn['memory']}/forget", headers=SAME, json={"confirm": True})
    assert resp.status_code == 200 and violetdb.get_event_memory(first_turn["memory"]) is None


from tests.test_control_center_p90 import admin, app, env_guard, made  # noqa: E402,F401  (fixtures)
