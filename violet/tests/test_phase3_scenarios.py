"""Phase 3 replay scenarios: getting to know the user (fictional persona "小满").

Each scenario runs through the real runtime (inbox -> pipeline -> outbox ->
background memory job) with the fake WeChat client and fake LLM, and checks
behaviour, state, message content and database records.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from tests.fakes import intent_json
from tests.test_phase2_scenarios import (  # noqa: F401  (make_world is a fixture)
    age_everything, arm, chat, decide, make_world, say, script, tool,
)


def age_all(db, hours):
    age_everything(db, hours)
    for table, cols in {
        "user_situations": ["observed_at", "expires_at", "created_at"],
        "user_profile_facts": ["stated_at", "confirmed_at", "created_at"],
        "profile_questions": ["created_at"],
        "preference_observations": ["observed_at"],
    }.items():
        for row in db.fetch_all(f"SELECT id, {', '.join(cols)} FROM {table}"):
            updates = {c: (datetime.fromisoformat(row[c]) - timedelta(hours=hours)).isoformat() for c in cols if row[c]}
            if updates:
                db.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in updates)} WHERE id = ?",
                           (*updates.values(), row["id"]))


def last_chat_system(w):
    call = next(c for c in reversed(w.llm.calls_for("fake-chat"))
                if not any("主动触达" in (m.get("content") or "") for m in c["messages"] if m["role"] == "user"))
    return "\n".join(m.get("content") or "" for m in call["messages"] if m["role"] == "system")


def facts(w, status="active"):
    return [(f["fact_key"], f["value"]) for f in w.db.fetch_all(
        "SELECT * FROM user_profile_facts WHERE status = ? ORDER BY id", (status,))]


def quiet_memory(w):
    w.llm.responders["fake-memory"] = lambda kw: json.dumps({"should_save": False, "reason": "t"})


# ---------------------------------------------------------------------------
# 1. The user says they are a university student
# ---------------------------------------------------------------------------

def test_p3_s1_user_says_they_are_a_university_student(make_world):
    w = make_world()
    w.llm.responders["fake-memory"] = script(
        tool("remember_profile_fact", key="identity.status", value="大学生", evidence="我是大学生"))
    say(w, "我是大学生，今年大二")
    row = w.db.fetch_one("SELECT * FROM user_profile_facts")
    user_msg = w.db.fetch_one("SELECT id FROM messages WHERE role = 'user'")
    assert (row["fact_key"], row["value"], row["status"], row["origin"]) == ("identity.status", "大学生", "active", "explicit")
    assert (row["evidence"], row["source_message_id"], row["confidence"]) == ("我是大学生", user_msg["id"], 1.0)

    quiet_memory(w)
    say(w, "下午还有两节专业课")
    system = last_chat_system(w)
    assert "身份：大学生" in system and "用户亲口说" not in system          # natural context: no source tags
    say(w, "今天的晚霞好好看")
    assert "大学生" not in last_chat_system(w)                       # only when relevant


# ---------------------------------------------------------------------------
# 2. The user usually lives on campus
# ---------------------------------------------------------------------------

def test_p3_s2_user_usually_lives_on_campus(make_world):
    w = make_world()
    w.llm.responders["fake-memory"] = script(
        tool("remember_profile_fact", key="residence.usual", value="平时住学校宿舍", evidence="我平时住校"))
    say(w, "我平时住校，周末偶尔回家")
    assert facts(w) == [("residence.usual", "平时住学校宿舍")]
    quiet_memory(w)
    say(w, "宿舍今天又停水了")
    system = last_chat_system(w)
    assert "平时住：平时住学校宿舍" in system and "不代表用户此刻在哪里" in system


# ---------------------------------------------------------------------------
# 3. Today the user went home: the situation updates, the fact does not
# ---------------------------------------------------------------------------

def test_p3_s3_going_home_today_is_a_situation_not_a_fact(make_world):
    w = make_world()
    w.db.execute("DELETE FROM user_profile_facts")
    w.llm.responders["fake-memory"] = script(
        tool("remember_profile_fact", key="residence.usual", value="平时住学校宿舍", evidence="我平时住校"))
    say(w, "我平时住校")
    w.llm.responders["fake-memory"] = script(
        tool("set_situation", kind="location", value="家里", evidence="今天回家了", until="today"))
    say(w, "今天回家了，妈妈做了红烧肉")
    situation = w.db.fetch_one("SELECT * FROM user_situations")
    assert (situation["kind"], situation["value"], situation["source"], situation["status"]) == \
        ("location", "家里", "user_stated", "active")
    assert situation["expires_at"].startswith(w.db.now()[:10]) and situation["expires_at"].endswith("T23:59:00+08:00")
    assert facts(w) == [("residence.usual", "平时住学校宿舍")]        # the stable fact is untouched
    quiet_memory(w)
    say(w, "吃得好撑")
    assert "此刻所在：家里（对方说的" in last_chat_system(w)


# ---------------------------------------------------------------------------
# 4. Violet does not treat the usual residence as the current location
# ---------------------------------------------------------------------------

def test_p3_s4_usual_residence_is_not_current_location(make_world):
    w = make_world()
    w.db.save_event_memory  # noqa: B018  (world ready)
    from ai.profile_store import Profile
    Profile(w.db).facts.remember("residence.usual", "平时住学校宿舍", evidence="我平时住校", user_message="我平时住校")
    quiet_memory(w)
    say(w, "猜猜我现在在哪")
    system = last_chat_system(w)
    assert "此刻在哪里、在做什么：不知道（不要根据常住地或生活规律推断）" in system
    assert "平时住学校宿舍" not in system
    assert "车辆" not in system and "用户当前位置" not in system
    # a phone/vehicle signal cannot set the location either
    assert Profile(w.db).situations.observe("location", "学校", "vehicle")["action"] == "rejected"
    assert w.db.fetch_all("SELECT * FROM user_situations") == []


# ---------------------------------------------------------------------------
# 5. The user corrects school-life information
# ---------------------------------------------------------------------------

def test_p3_s5_user_corrects_campus_life(make_world):
    w = make_world()
    w.llm.responders["fake-memory"] = script(
        tool("remember_profile_fact", key="residence.usual", value="平时住学校宿舍", evidence="我平时住校"))
    say(w, "我平时住校")
    old = w.db.fetch_one("SELECT * FROM user_profile_facts")
    w.llm.responders["fake-memory"] = script(
        tool("correct_profile_fact", key="residence.usual", new_value="在校外租房住",
             evidence="我早就不住宿舍了，现在在校外租房"))
    say(w, "你记错了，我早就不住宿舍了，现在在校外租房")
    assert facts(w) == [("residence.usual", "在校外租房住")]
    old_row = w.db.fetch_one("SELECT * FROM user_profile_facts WHERE id = ?", (old["id"],))
    new_row = w.db.fetch_one("SELECT * FROM user_profile_facts WHERE status = 'active'")
    assert old_row["status"] == "superseded" and old_row["superseded_by"] == new_row["id"]
    assert new_row["supersedes_id"] == old["id"]
    actions = [a["action"] for a in w.db.fetch_all("SELECT action FROM profile_audit_log ORDER BY id")]
    assert actions == ["create", "correct"]
    quiet_memory(w)
    say(w, "房东说下个月要涨房租，住的地方真烦")
    system = last_chat_system(w)
    assert "平时住：在校外租房住" in system and "平时住学校宿舍" not in system


# ---------------------------------------------------------------------------
# 6. Violet does not ask again about what she already knows
# ---------------------------------------------------------------------------

def test_p3_s6_known_facts_are_not_asked_again(make_world):
    w = make_world()
    from ai.profile_store import Profile
    profile = Profile(w.db)
    for key, value, words in (("address_name", "小满", "叫我小满"), ("identity.status", "大学生", "我是大学生"),
                              ("residence.usual", "平时住学校宿舍", "我平时住校")):
        profile.facts.remember(key, value, evidence=words, user_message=words)
    quiet_memory(w)
    offered = []
    for i in range(4):
        say(w, "早上好")
        offered.append(w.db.fetch_one("SELECT fact_key FROM profile_questions ORDER BY id DESC LIMIT 1"))
        age_all(w.db, 24)
        for j in range(6):
            w.db.add_user_message("text", f"闲聊{i}-{j}")
    asked = [o["fact_key"] for o in offered if o]
    assert asked and set(asked) == {"interest"}                    # never a key that is already known
    system = last_chat_system(w)
    assert "【已经了解过的方面（不要再问这些）】" in system and "希望被称呼为" in system
    # once everything optional is known, Violet stops offering questions altogether
    profile.facts.remember("interest", "打羽毛球", evidence="我喜欢打羽毛球", user_message="我喜欢打羽毛球")
    age_all(w.db, 24)
    for j in range(6):
        w.db.add_user_message("text", f"再聊{j}")
    say(w, "早上好")
    assert "【可选的了解】" not in last_chat_system(w)


# ---------------------------------------------------------------------------
# 7. The user declines a personal question
# ---------------------------------------------------------------------------

def test_p3_s7_user_declines_and_eve_respects_it(make_world):
    w = make_world()
    quiet_memory(w)
    chat(w, normal="早～对了我该怎么叫你呀")
    say(w, "早上好")
    assert "对方希望你怎么称呼他/她" in last_chat_system(w)
    assert w.db.fetch_one("SELECT * FROM profile_questions")["outcome"] == "offered"

    chat(w, normal="好嘞")
    say(w, "这个先不告诉你")
    assert w.db.fetch_one("SELECT outcome FROM profile_questions WHERE fact_key = 'address_name'")["outcome"] == "declined"
    for i in range(3):
        age_all(w.db, 24 * 20)
        for j in range(6):
            w.db.add_user_message("text", f"随便{i}{j}")
        say(w, "早上好")
        assert "希望你怎么称呼" not in last_chat_system(w)
    asked = {r["fact_key"] for r in w.db.fetch_all("SELECT fact_key FROM profile_questions WHERE outcome = 'offered'")}
    assert "address_name" not in asked                            # other topics may come up, this one never again


# ---------------------------------------------------------------------------
# 8. An occasional short reply does not mark the user as cold for good
# ---------------------------------------------------------------------------

def test_p3_s8_occasional_short_reply_is_not_a_permanent_trait(make_world):
    w = make_world()
    quiet_memory(w)
    say(w, "早")
    age_all(w.db, 3)
    decide(w, topic_key="small_talk", chat_brief={"topic": "闲聊"})
    chat(w, proactive="在干嘛呀")
    arm(w)
    w.scheduler.tick()
    say(w, "嗯")
    assert w.scheduler.tracker.cold_streak() == 1
    assert "proactive_chat" not in w.scheduler.profile.preferences.effective()      # one "嗯" is not a trait
    obs = w.db.fetch_all("SELECT value FROM preference_observations WHERE pref_key = 'proactive_chat'")
    assert [o["value"] for o in obs] == ["less"]

    age_all(w.db, 2)
    say(w, "在吗")                                                # the user comes back on their own
    assert w.scheduler.tracker.cold_streak() == 0
    age_all(w.db, 4)
    arm(w)
    w.scheduler.tick()
    assert w.client.sent_texts.count("在干嘛呀") == 2              # Violet is not silenced


# ---------------------------------------------------------------------------
# 9. The user forbids using a memory
# ---------------------------------------------------------------------------

def test_p3_s9_forbidden_memory_never_reaches_context_again(make_world):
    w = make_world()
    memory_id = w.db.save_event_memory("用户和室友闹了点矛盾", kind="event", progress="ongoing")["memory"]["id"]
    w.db.set_event_memory_enabled(memory_id, False)
    # Phase 7 B.2: query_memory is only offered while some event is searchable; an unrelated enabled event
    # keeps it offered, so the search below still has to filter the disabled one out by itself.
    w.db.save_event_memory("用户周末去看了一场电影", kind="event", progress="done")
    w.llm.responders["fake-intent"] = script(tool("query_memory", query="室友 矛盾"), final=intent_json("chat"))
    quiet_memory(w)
    say(w, "室友今天又把音乐开很大声")
    intent_tool = next(m for m in w.llm.calls_for("fake-intent")[-1]["messages"] if m["role"] == "tool")
    assert json.loads(intent_tool["content"])["memories"] == []
    assert "闹了点矛盾" not in last_chat_system(w)
    state = w.scheduler._conversation_state()
    assert "闹了点矛盾" not in json.dumps(state, ensure_ascii=False)
    # the memory agent still sees it, flagged, so it does not save it again
    agent = w.agents["memory"]
    found = agent._tool_query_memory({"query": "室友 矛盾"})["memories"]
    assert found[0]["disabled_by_user"] is True


# ---------------------------------------------------------------------------
# 10. An expired situation is no longer used
# ---------------------------------------------------------------------------

def test_p3_s10_expired_situation_is_not_used_for_the_present(make_world):
    w = make_world()
    w.llm.responders["fake-memory"] = script(
        tool("set_situation", kind="availability", value="在考试", evidence="我要去考试了", until="5h"))
    say(w, "我要去考试了，晚点聊")
    age_all(w.db, 3)
    arm(w)
    status = w.scheduler.status_snapshot()
    assert status["due_reason"].startswith("the user said they are busy")
    # three hours later the stated window is over: the situation expired
    age_all(w.db, 3)
    quiet_memory(w)
    say(w, "你猜我现在在干嘛")
    system = last_chat_system(w)
    assert "在考试" not in system and "此刻在哪里、在做什么：不知道" in system
    assert w.db.fetch_one("SELECT status FROM user_situations")["status"] == "expired"
    age_all(w.db, 3)
    arm(w)
    assert not w.scheduler.status_snapshot()["due_reason"].startswith("the user said they are busy")


# ---------------------------------------------------------------------------
# 11. Old memories and the new profile coexist
# ---------------------------------------------------------------------------

def test_p3_s11_legacy_core_memories_and_new_profile(make_world):
    w = make_world()
    nick = w.db.upsert_core_memory("nickname_preference", "nickname", "用户喜欢被叫阿澈")["memory"]
    style = w.db.upsert_core_memory("reply_style", "reply_style", "用户喜欢短句回复")["memory"]
    w.rt.recover_pending()                                      # startup: legacy import
    candidates = w.db.fetch_all("SELECT * FROM user_profile_facts ORDER BY id")
    assert [(c["fact_key"], c["status"], c["origin"]) for c in candidates] == \
        [("address_name", "candidate", "legacy"), ("other", "candidate", "legacy")]
    quiet_memory(w)
    say(w, "早")
    system = last_chat_system(w)
    assert "【旧系统保存的相处偏好与长期记忆（未逐条确认" in system and "用户喜欢短句回复" in system
    assert "希望被称呼为：" not in system                         # an unconfirmed candidate is not a fact

    from ai.profile_store import Profile
    profile = Profile(w.db)
    confirmed = profile.facts.confirm_candidate(candidates[0]["id"], value="阿澈")
    assert confirmed["fact"]["status"] == "active" and confirmed["fact"]["origin"] == "dashboard"
    profile.facts.set_enabled(candidates[1]["id"], False)
    say(w, "早")
    system = last_chat_system(w)
    assert "希望被称呼为：阿澈" in system and "用户喜欢短句回复" not in system
    profile.facts.delete(candidates[1]["id"])
    assert w.db.get_core_memory(style["id"]) is None and w.db.get_core_memory(nick["id"]) is not None
