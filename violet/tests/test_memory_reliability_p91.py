"""Phase 9.1: long-term memory reliability (offline, isolated DB, fake SDK).

Each test reproduces one defect found in the 9.1 audit (see docs/knowledge/bugs-and-risks.md, Phase 9.1)
and stays as its regression test.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from tests.fakes import FakeLLM, default_responders, install_fake_llm, intent_json
from tests.test_control_center_p90 import SAME, admin, app, env_guard, made  # noqa: F401  (fixtures)
from tests.test_phase2_scenarios import make_world, say, script, tool  # noqa: F401  (fixture)


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


def call(agent, name, **args):
    return json.loads(getattr(agent, f"_tool_{name}")(args))


def system_text(request) -> str:
    return "\n".join(m.get("content") or "" for m in request["messages"] if m["role"] == "system")


# --- A1: daily decay hard-deleted memories ------------------------------------------------------------

@pytest.mark.parametrize("category", ["event", "general"])
def test_decay_never_deletes_and_old_shared_experience_stays_recallable(agents, violetdb, category):
    target = violetdb.save_event_memory("用户和Eve一起云看了电影《海上钢琴师》，看哭了", category=category,
                                     kind="event", progress="done")["memory"]
    for _ in range(90):                      # three months of nightly decay runs
        violetdb.decay_event_memories()
    row = violetdb.get_event_memory(target["id"])
    assert row["status"] != "deleted"
    found = agents["intent"]._tool_query_memory({"query": "海上钢琴师"})["memories"]
    assert [m["id"] for m in found] == [target["id"]]
    # 'deleted' stays reserved for a user / dashboard retraction, which always leaves a revision
    assert not [r for r in violetdb.list_memory_revisions("event_memories", target["id"]) if r["action"] == "retract"]


# --- A2: event recall depended only on Intent calling query_memory ---------------------------------------

def test_open_event_reaches_chat_when_intent_did_not_search(make_world):
    w = make_world()
    target = w.db.save_event_memory("用户下周三下午有一场实习面试，有点紧张", category="event", kind="event",
                                    progress="planned")["memory"]
    w.llm.responders["fake-intent"] = lambda kw: intent_json("chat")          # no query_memory call
    seen = []
    w.llm.responders["fake-chat"] = lambda kw: seen.append(system_text(kw)) or "恭喜呀"
    say(w, "面试结束啦，感觉还行")
    assert any(f"id={target['id']};" in s and "实习面试" in s for s in seen)


def test_word_recall_shows_a_plan_whose_time_passed_today_as_unknown(violetdb):
    """The nightly decay (03:00) also refreshes states; between the event time and that night nothing else did."""
    from datetime import datetime, timedelta
    from ai.recall_search import user_word_recall
    passed = (datetime.fromisoformat(violetdb.now()) - timedelta(hours=2)).isoformat()
    memory_id = violetdb.add_event_memory("用户今天下午有一场实习面试", kind="event", progress="planned", event_time=passed)
    found = user_word_recall(violetdb, "面试结束啦")
    assert [(m["id"], m["progress"]) for m in found] == [(memory_id, "unknown")]


def test_unrelated_message_does_not_pull_memories(make_world):
    w = make_world()
    w.db.save_event_memory("用户下周三下午有一场实习面试，有点紧张", kind="event", progress="planned")
    w.db.save_event_memory("用户说和Eve的第一次深夜长聊让他很心动", kind="event", progress="done")
    seen = []
    w.llm.responders["fake-chat"] = lambda kw: seen.append(system_text(kw)) or "哈哈"
    say(w, "今天食堂的麻辣烫好难吃")
    assert seen and not any("相关记忆" in s for s in seen)


def test_recall_question_without_a_hit_is_not_treated_as_never_said(violetdb):
    from ai.context_engine import ContextEngine
    packet = ContextEngine(violetdb).build("chat", "你还记得我上次说的那家店叫什么吗", {"recalled_memories": [], "route": "chat"})
    text = "\n".join(packet.sections)
    assert "不代表对方没说过" in text


# --- A3: synonyms after the first query word were useless -----------------------------------------------

def test_later_query_variant_is_used_when_the_first_word_has_no_hit(agents, violetdb):
    target = violetdb.add_event_memory("用户最近总是睡不着，压力很大", kind="event", progress="ongoing")
    violetdb.add_event_memory("用户的猫最近不爱吃饭", kind="event", progress="ongoing")
    found = agents["intent"]._tool_query_memory({"query": "失眠 睡不着 焦虑 安慰方式"})["memories"]
    assert [m["id"] for m in found] == [target]


# --- A4: the search window skipped low-weight (old) memories --------------------------------------------

def test_old_low_weight_memory_is_still_a_search_candidate(agents, violetdb):
    target = violetdb.add_event_memory("用户小时候学过三年钢琴", kind="event", progress="done", forget_weight=0.2)
    for i in range(320):
        violetdb.add_event_memory(f"用户第{i}次提到天气不错", kind="background", forget_weight=5.0)
    found = agents["intent"]._tool_query_memory({"query": "钢琴"})["memories"]
    assert [m["id"] for m in found] == [target]


# --- A5: deleted / disabled memory text leaked through the cooling-topic section -----------------------

@pytest.mark.parametrize("how", ["dashboard_delete", "disable"])
def test_removed_memory_text_does_not_leak_through_cooling_topics(violetdb, how):
    from ai.chat import cooling_topics_for_turn
    from ai.context_engine import ContextEngine
    from ai.conversation_state import TopicTracker
    secret = "用户和前任分手那晚哭了很久"
    memory_id = violetdb.add_event_memory(secret, kind="event", progress="done")
    tracker = TopicTracker(violetdb)
    for _ in range(2):                                   # Violet raised it twice, the user did not engage
        tracker.record_eve_topic("breakup", memory_ids=[memory_id], source="violet_proactive")
        tracker.evaluate("嗯")
    assert any(c.get("memory_id") == memory_id for c in tracker.cooling_topics())
    if how == "dashboard_delete":
        violetdb.retract_event_memory(memory_id, reason="管理后台删除", source="dashboard")
    else:
        violetdb.set_event_memory_enabled(memory_id, False)
    packet = ContextEngine(violetdb).build("chat", "在吗", {"restricted_topics": cooling_topics_for_turn([]), "route": "chat"})
    assert secret not in "\n".join(packet.sections)


# --- A6: silent turns skipped memory consolidation -----------------------------------------------------

def test_substantive_message_in_a_silent_turn_is_still_consolidated(make_world):
    w = make_world()
    w.llm.responders["fake-intent"] = lambda kw: intent_json("silent")
    say(w, "晚安啦，对了明早八点我要去医院复查")
    assert [j["status"] for j in w.db.list_memory_jobs()] == ["done"]


def test_bare_goodnight_in_a_silent_turn_costs_no_memory_call(make_world):
    w = make_world()
    w.llm.responders["fake-intent"] = lambda kw: intent_json("silent")
    say(w, "晚安")
    assert w.db.list_memory_jobs() == [] and w.llm.calls_for("fake-memory") == []


# --- A7: dashboard corrections could be silently overwritten by chat consolidation ----------------------

def test_dashboard_corrected_event_is_not_rewritten_without_the_users_words(agents, violetdb):
    memory_id = violetdb.add_event_memory("用户在A公司实习", kind="fact")
    violetdb.apply_event_update(memory_id, content="用户在B公司实习（管理后台确认）", reason="管理后台纠正",
                             source="dashboard", action="correct")
    with turn_context(turn_id="t-vague", user_message="最近实习好累啊"):
        result = call(agents["memory"], "update_memory", memory_id=memory_id, summary="用户在A公司实习，很累")
    assert result["ok"] is False and result["reason"] == "confirmed_by_user"
    assert violetdb.get_event_memory(memory_id)["content"] == "用户在B公司实习（管理后台确认）"
    with turn_context(turn_id="t-explicit", user_message="我换到C公司实习了"):
        result = call(agents["memory"], "correct_memory", memory_id=memory_id, corrected_summary="用户在C公司实习",
                      evidence="我换到C公司实习了")
    assert result["ok"] and violetdb.get_event_memory(memory_id)["content"] == "用户在C公司实习"


def test_dashboard_corrected_core_memory_is_not_overwritten_and_changes_are_revisioned(agents, violetdb):
    violetdb.upsert_core_memory("user_pet", "pet", "用户养了一只猫")
    core = violetdb.get_core_memory_by_key("user_pet")
    violetdb.add_memory_revision("core_memories", core["id"], "correct", old_content=core["content"],
                              new_content="用户养了一只柯基，叫豆豆", reason="管理后台纠正", source="dashboard")
    violetdb.execute("UPDATE core_memories SET content = ? WHERE id = ?", ("用户养了一只柯基，叫豆豆", core["id"]))
    with turn_context(turn_id="t-core", user_message="今天好累"):
        result = call(agents["memory"], "upsert_core_memory", memory_key="user_pet", category="pet", content="用户养了一只猫")
    assert result["ok"] is False and result["reason"] == "confirmed_by_user"
    assert violetdb.get_core_memory(core["id"])["content"] == "用户养了一只柯基，叫豆豆"
    with turn_context(turn_id="t-core2", user_message="我还喜欢晚上散步"):
        call(agents["memory"], "upsert_core_memory", memory_key="user_walk", category="habit", content="用户喜欢晚上散步")
        call(agents["memory"], "upsert_core_memory", memory_key="user_walk", category="habit", content="用户喜欢晚上散步和夜跑")
    walk = violetdb.get_core_memory_by_key("user_walk")
    revisions = violetdb.list_memory_revisions("core_memories", walk["id"])
    assert [r["new_content"] for r in revisions][-1] == "用户喜欢晚上散步和夜跑"
    assert revisions[-1]["old_content"] == "用户喜欢晚上散步" and revisions[-1]["source_turn_id"] == "t-core2"


def test_confirmed_event_progress_may_change_but_its_wording_needs_the_users_words(agents, violetdb):
    memory_id = violetdb.add_event_memory("用户周五要去B公司二面", kind="event", progress="planned")
    violetdb.apply_event_update(memory_id, content="用户周五去B公司二面（后台确认）", source="dashboard", action="correct")
    with turn_context(turn_id="t-p", user_message="二面结束了"):
        result = call(agents["memory"], "update_event_progress", memory_id=memory_id, progress="done",
                      summary="用户去A公司二面结束了", reason="二面结束了")
    row = violetdb.get_event_memory(memory_id)
    assert result["ok"] and result["content_kept"] and row["progress"] == "done"
    assert row["content"] == "用户周五去B公司二面（后台确认）"


def test_quoting_one_word_is_not_the_users_words(agents, violetdb):
    memory_id = violetdb.add_event_memory("用户在B公司实习", kind="fact")
    violetdb.apply_event_update(memory_id, content="用户在B公司实习（确认）", source="dashboard", action="correct")
    with turn_context(turn_id="t-short", user_message="我好累"):
        assert call(agents["memory"], "update_memory", memory_id=memory_id, summary="用户在A公司实习",
                    evidence="我")["reason"] == "confirmed_by_user"
        assert call(agents["memory"], "correct_memory", memory_id=memory_id, corrected_summary="用户在A公司实习",
                    reason="x", evidence="没说过的话啊")["reason"] == "confirmed_by_user"
        assert call(agents["memory"], "retract_memory", memory_id=memory_id, reason="x")["reason"] == "confirmed_by_user"
        assert call(agents["memory"], "save_memory", summary="用户在A公司实习", category="work",
                    related_to=memory_id)["reason"] == "confirmed_by_user"
    assert violetdb.get_event_memory(memory_id)["status"] == "active"
    with turn_context(turn_id="t-retract", user_message="我从来没在B公司实习过，删掉吧"):
        assert call(agents["memory"], "retract_memory", memory_id=memory_id, reason="用户否认",
                    evidence="我从来没在B公司实习过")["action"] == "retracted"


def test_unconfirmed_memory_keeps_the_old_correction_path(agents, violetdb):
    memory_id = violetdb.add_event_memory("用户养了一只猫", kind="fact")
    with turn_context(turn_id="t-c", user_message="我养的是狗不是猫"):
        assert call(agents["memory"], "correct_memory", memory_id=memory_id, corrected_summary="用户养了一只狗",
                    reason="我养的是狗不是猫")["action"] == "corrected"
    assert not violetdb.confirmed_by_user("event_memories", memory_id)          # no quote -> not "confirmed"
    with turn_context(turn_id="t-c2", user_message="对，是一只柴犬"):
        assert call(agents["memory"], "correct_memory", memory_id=memory_id, corrected_summary="用户养了一只柴犬",
                    reason="补充", evidence="是一只柴犬")["action"] == "corrected"
    assert violetdb.confirmed_by_user("event_memories", memory_id)


# --- A8: source of a memory -------------------------------------------------------------------------------

def test_memory_saved_in_a_background_job_points_at_its_user_message(make_world):
    w = make_world()
    w.llm.responders["fake-memory"] = script(tool("save_memory", summary="用户养了一只柯基，叫豆豆", kind="fact"))
    say(w, "我家柯基叫豆豆")
    row = w.db.fetch_one("SELECT * FROM event_memories WHERE content = '用户养了一只柯基，叫豆豆'")
    message = w.db.fetch_one("SELECT id FROM messages WHERE role = 'user' AND content = '我家柯基叫豆豆'")
    assert json.loads(row["source_message_ids"]) == [message["id"]]
    from ai.memory_sources import memory_source
    source = memory_source(w.db, w.db.get_event_memory(row["id"]))
    assert source["state"] == "known" and source["message_ids"] == [message["id"]]


def test_each_update_adds_the_message_it_was_based_on(agents, violetdb):
    first = violetdb.add_user_message("text", "下周三我有个面试")
    later = violetdb.add_user_message("text", "面试结束啦")
    memory_id = violetdb.save_event_memory("用户下周三有个面试", kind="event", progress="planned",
                                        source_message_id=first)["memory"]["id"]
    with turn_context(turn_id="t-up", user_message="面试结束啦", user_message_id=later):
        call(agents["memory"], "update_event_progress", memory_id=memory_id, progress="done", reason="面试结束啦")
        call(agents["memory"], "update_event_progress", memory_id=memory_id, progress="done", reason="again")
    assert violetdb.get_event_memory(memory_id)["source_message_ids"] == [first, later]


def test_memory_without_any_source_is_reported_unknown_not_invented(violetdb):
    from ai.memory_sources import memory_source
    legacy = violetdb.add_event_memory("用户喜欢吃辣", kind="legacy")
    source = memory_source(violetdb, violetdb.get_event_memory(legacy))
    assert source == {"state": "unknown", "message_ids": [], "turn_id": None, "label": "来源未知"}


def test_control_center_shows_source_and_confirmation(violetdb, admin):
    message_id = violetdb.add_user_message("text", "我下周去杭州出差")
    with_source = violetdb.save_event_memory("用户下周去杭州出差", kind="event", progress="planned",
                                          source_message_id=message_id)["memory"]["id"]
    legacy = violetdb.add_event_memory("用户喜欢吃辣", kind="legacy")
    rows = {r["id"]: r for r in admin.get("/api/control/memories?kind=event").get_json()["data"]["memories"]}
    assert rows[with_source]["source"]["message_ids"] == [message_id]
    assert rows[legacy]["source"]["label"] == "来源未知" and rows[legacy]["confirmed_by_user"] is False
    assert admin.post(f"/api/control/memories/event/{legacy}/correct", headers=SAME,
                      json={"content": "用户很能吃辣"}).status_code == 200
    rows = {r["id"]: r for r in admin.get("/api/control/memories?kind=event").get_json()["data"]["memories"]}
    assert rows[legacy]["confirmed_by_user"] is True
    history = admin.get(f"/api/control/memories/event/{with_source}/history").get_json()["data"]
    assert history["source_messages"][0]["content"] == "我下周去杭州出差"


# --- A9: the recall section was trimmed before Violet's own life / web sections -----------------------------

def test_recalled_memories_are_trimmed_after_eves_life_and_web_sections():
    from ai.context_engine import ContextEngine, _Section
    recall = _Section("recall", ContextEngine.RECALL_PRIORITY, "相关记忆：", ["- id=1; 用户下周三面试"], "规则")
    life = _Section("violet_life", 4, "【Violet 的生活】", ["- 今天读了几页书"])
    web = _Section("external_info", 5, "【网上】", ["- 某新闻"])
    sections = [recall, life, web]
    budget = sum(len(s.render()) for s in sections) - 5
    ContextEngine._fit(sections, budget)
    assert recall.lines == ["- id=1; 用户下周三面试"] and not web.lines
