"""Phase 3 unit tests: profile facts, situations, preferences, onboarding, deletion, time zone.

All people, schools and places here are fictional.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def profile(violetdb):
    from ai.profile_store import Profile
    return Profile(violetdb)


def now(db):
    return datetime.fromisoformat(db.now())


# --- facts: explicit vs inferred ---------------------------------------------------------

def test_explicit_statement_becomes_active_fact(profile):
    result = profile.facts.remember("identity.status", "大学生", evidence="我是大学生", user_message="我是大学生，今年大三")
    fact = result["fact"]
    assert (result["action"], fact["status"], fact["origin"], fact["confidence"], fact["use_in_context"]) == \
        ("created", "active", "explicit", 1.0, 1)
    assert fact["evidence"] == "我是大学生" and fact["confirmed_at"] and fact["category"] == "identity"


def test_guess_is_only_a_candidate_never_a_fact(profile):
    result = profile.facts.remember("identity.status", "研究生", evidence="经常提到实验室",
                                    user_message="今天又在实验室待了一天")
    assert result["action"] == "candidate"
    fact = result["fact"]
    assert (fact["status"], fact["origin"], fact["use_in_context"]) == ("candidate", "inferred", 0)
    assert fact["confidence"] <= 0.6
    assert profile.facts.active_facts() == []


def test_unknown_key_and_empty_value_are_rejected(profile):
    assert profile.facts.remember("blood_type", "A", evidence="x", user_message="x")["reason"] == "unknown_key"
    assert profile.facts.remember("interest", " ", evidence="x", user_message="x")["reason"] == "empty_value"


def test_sensitive_information_is_not_persisted_by_default(profile, violetdb):
    result = profile.facts.remember("other", "手机号13812345678", evidence="我的手机号是13812345678",
                                    user_message="我的手机号是13812345678")
    assert result == {"action": "not_stored", "reason": "sensitive"}
    assert profile.facts.list_facts() == []
    audit = profile.audit.entries()
    assert audit[0]["action"] == "not_stored_sensitive" and "13812345678" not in json.dumps(audit, ensure_ascii=False)


def test_sensitive_storage_can_be_opted_in(profile, monkeypatch):
    monkeypatch.setenv("VIOLET_PROFILE_STORE_SENSITIVE", "1")
    fact = profile.facts.remember("other", "对花粉过敏，看病常去社区医院", evidence="我对花粉过敏",
                                  user_message="我对花粉过敏")
    assert fact["fact"]["sensitivity"] == "sensitive"
    assert profile.facts.active_facts() == []          # stored, but still never put into context


# --- facts: conflicts ---------------------------------------------------------------------

def test_newer_explicit_statement_supersedes_and_keeps_history(profile):
    old = profile.facts.remember("residence.usual", "平时住学校宿舍", evidence="我平时住宿舍", user_message="我平时住宿舍")["fact"]
    new = profile.facts.remember("residence.usual", "在校外租房住", evidence="我搬出来租房了", user_message="我搬出来租房了")
    assert new["action"] == "superseded_previous"
    assert profile.facts.get_fact(old["id"])["status"] == "superseded"
    assert profile.facts.get_fact(old["id"])["superseded_by"] == new["fact"]["id"]
    assert [f["value"] for f in profile.facts.active_facts()] == ["在校外租房住"]


def test_older_statement_never_overrides_a_newer_one(profile, violetdb):
    profile.facts.remember("study.major", "软件工程", evidence="我学软件工程的", user_message="我学软件工程的")
    late = profile.facts.remember("study.major", "数学", evidence="我是数学系的", user_message="我是数学系的",
                                  stated_at=(now(violetdb) - timedelta(days=30)).isoformat())
    assert late["action"] == "older_statement" and late["fact"]["status"] == "superseded"
    assert [f["value"] for f in profile.facts.active_facts()] == ["软件工程"]


def test_inference_never_overrides_explicit_fact(profile):
    profile.facts.remember("identity.status", "大学生", evidence="我是大学生", user_message="我是大学生")
    guess = profile.facts.remember("identity.status", "上班族", evidence="提到加班", user_message="昨天加班到很晚")
    assert guess["action"] == "candidate"
    assert [f["value"] for f in profile.facts.active_facts()] == ["大学生"]


def test_multi_valued_interests_accumulate(profile):
    for value, ev in (("打羽毛球", "我喜欢打羽毛球"), ("听播客", "平时爱听播客")):
        profile.facts.remember("interest", value, evidence=ev, user_message=ev)
    assert sorted(f["value"] for f in profile.facts.active_facts()) == ["听播客", "打羽毛球"]


def test_retract_requires_the_users_words(profile):
    profile.facts.remember("work.role", "在奶茶店兼职", evidence="我在奶茶店兼职", user_message="我在奶茶店兼职")
    assert profile.facts.retract("work.role", evidence="辞了", user_message="今天天气好")["action"] == "rejected"
    assert profile.facts.retract("work.role", evidence="兼职已经辞了", user_message="兼职已经辞了")["action"] == "retracted"
    assert profile.facts.active_facts() == []


# --- disable / delete ---------------------------------------------------------------------

def test_disabled_fact_is_kept_but_never_used_or_reactivated(profile):
    fact = profile.facts.remember("study.school", "星河大学", evidence="我在星河大学", user_message="我在星河大学")["fact"]
    profile.facts.set_enabled(fact["id"], False)
    assert profile.facts.active_facts() == []
    again = profile.facts.remember("study.school", "星河大学", evidence="我在星河大学读书", user_message="我在星河大学读书")
    assert again["action"] == "kept_disabled"
    profile.facts.set_enabled(fact["id"], True)
    assert [f["value"] for f in profile.facts.active_facts()] == ["星河大学"]


def test_delete_purges_fact_history_evidence_and_derived_memories(profile, violetdb):
    violetdb.add_user_message("text", "对了我在星河大学读书")
    violetdb.add_assistant_message("text", "星河大学那边食堂好吃吗")
    fact = profile.facts.remember("study.school", "星河大学", evidence="我在星河大学读书", user_message="对了我在星河大学读书")["fact"]
    memory_id = violetdb.add_event_memory("用户在星河大学的社团招新", kind="event", progress="ongoing")
    other_id = violetdb.add_event_memory("用户周末去爬山", kind="event", progress="planned")
    violetdb.upsert_core_memory("school_info", "user_profile", "用户在星河大学")
    report = profile.facts.delete(fact["id"])
    assert report["facts_deleted"] == [fact["id"]] and report["memories_deleted"] == [memory_id]
    assert len(report["core_memories_deleted"]) == 1
    dump = json.dumps([violetdb.fetch_all(f"SELECT * FROM {t}") for t in
                       ("messages", "event_memories", "core_memories", "user_profile_facts", "memory_revisions", "profile_audit_log")],
                      ensure_ascii=False)
    assert "星河大学" not in dump
    assert violetdb.get_event_memory(other_id) is not None
    assert "[已删除的个人信息]" in violetdb.get_recent_messages(5)[0]["content"]
    audit = profile.audit.entries(entity="profile_fact", entity_id=fact["id"])
    assert audit[0]["action"] == "delete" and audit[0]["fact_key"] == "study.school"


def test_known_keys_cover_active_candidate_and_disabled(profile):
    profile.facts.remember("address_name", "小满", evidence="叫我小满", user_message="叫我小满就行")
    profile.facts.remember("identity.status", "大学生", evidence="猜的", user_message="今天上课")
    disabled = profile.facts.remember("interest", "跑步", evidence="我喜欢跑步", user_message="我喜欢跑步")["fact"]
    profile.facts.set_enabled(disabled["id"], False)
    assert profile.facts.known_keys() == {"address_name", "identity.status", "interest"}


# --- legacy -------------------------------------------------------------------------------

def test_legacy_core_memories_import_as_unconfirmed_candidates(profile, violetdb):
    violetdb.upsert_core_memory("nickname_preference", "nickname", "用户喜欢被叫阿澈")
    violetdb.upsert_core_memory("reply_style", "reply_style", "用户喜欢短句回复")
    assert profile.facts.import_legacy_core_memories() == 2
    assert profile.facts.import_legacy_core_memories() == 0              # idempotent
    rows = profile.facts.list_facts()
    assert {(r["fact_key"], r["status"], r["origin"], r["use_in_context"]) for r in rows} == {
        ("address_name", "candidate", "legacy", 0), ("other", "candidate", "legacy", 0)}
    assert profile.facts.active_facts() == []


# --- situations ------------------------------------------------------------------------------

def test_situation_from_users_words_expires(profile, violetdb):
    result = profile.situations.observe("location", "家里", "user_stated", evidence="今天回家了",
                                        user_message="今天回家了，明天回学校", until="today")
    situation = result["situation"]
    assert situation["expires_at"].startswith(violetdb.now()[:10]) and situation["expires_at"].endswith("23:59:00+08:00")
    assert [s["value"] for s in profile.situations.current()] == ["家里"]
    violetdb.execute("UPDATE user_situations SET expires_at = ? WHERE id = ?",
                  ((now(violetdb) - timedelta(minutes=1)).isoformat(), situation["id"]))
    assert profile.situations.current() == []
    assert profile.situations.get(situation["id"])["status"] == "expired"


def test_newer_situation_supersedes_older_of_same_kind(profile):
    first = profile.situations.observe("activity", "在上课", "user_stated", evidence="在上课", user_message="在上课")["situation"]
    profile.situations.observe("activity", "在食堂吃饭", "user_stated", evidence="在食堂吃饭", user_message="下课了在食堂吃饭")
    assert [s["value"] for s in profile.situations.current()] == ["在食堂吃饭"]
    assert profile.situations.get(first["id"])["status"] == "superseded"


@pytest.mark.parametrize("kind,source", [("location", "phone_mcp"), ("location", "vehicle"),
                                         ("activity", "phone_mcp"), ("availability", "vehicle")])
def test_tools_and_vehicles_cannot_set_the_users_location_or_activity(profile, kind, source):
    result = profile.situations.observe(kind, "宿舍", source)
    assert result["action"] == "rejected"


def test_phone_tool_may_only_report_device_state_with_low_confidence(profile, violetdb):
    result = profile.situations.observe("device", "屏幕熄灭", "phone_mcp", confidence=0.9, until="3h")
    s = result["situation"]
    assert s["confidence"] == 0.5
    assert datetime.fromisoformat(s["expires_at"]) - now(violetdb) <= timedelta(minutes=30, seconds=5)


def test_situation_requires_users_words(profile):
    assert profile.situations.observe("location", "家里", "user_stated", evidence="回家了",
                                      user_message="今天好累")["reason"] == "evidence_not_in_user_message"


# --- preferences -------------------------------------------------------------------------------

def test_explicit_preference_requires_words_and_beats_implicit(profile):
    for _ in range(5):
        profile.preferences.observe("follow_up_questions", "less", "engagement")
    assert profile.preferences.effective()["follow_up_questions"]["origin"] == "implicit"
    assert profile.preferences.set_explicit("follow_up_questions", "ok", evidence="你多问问我没关系",
                                            user_message="今天好累")["action"] == "rejected"
    profile.preferences.set_explicit("follow_up_questions", "ok", evidence="你多问问我没关系",
                                     user_message="你多问问我没关系的")
    pref = profile.preferences.effective()["follow_up_questions"]
    assert (pref["value"], pref["origin"]) == ("ok", "explicit")


def test_implicit_preference_needs_several_consistent_observations(profile):
    profile.preferences.observe("proactive_chat", "less", "engagement")
    assert "proactive_chat" not in profile.preferences.effective()           # one brief reply is not a trait
    for _ in range(3):
        profile.preferences.observe("proactive_chat", "less", "engagement")
    pref = profile.preferences.effective()["proactive_chat"]
    assert (pref["value"], pref["origin"]) == ("less", "implicit") and pref["confidence"] <= 0.6


def test_implicit_preference_recovers_when_behaviour_changes(profile):
    for _ in range(4):
        profile.preferences.observe("proactive_chat", "less", "engagement")
    for _ in range(6):
        profile.preferences.observe("proactive_chat", "welcome", "engagement")
    pref = profile.preferences.effective().get("proactive_chat")
    assert pref is None or pref["value"] != "less"


def test_old_observations_fall_out_of_the_window(profile, violetdb):
    for _ in range(5):
        profile.preferences.observe("proactive_chat", "less", "engagement")
    violetdb.execute("UPDATE preference_observations SET observed_at = ?", ((now(violetdb) - timedelta(days=30)).isoformat(),))
    profile.preferences.observe("proactive_chat", "welcome", "engagement")
    assert profile.preferences.effective().get("proactive_chat") is None


def test_avoid_topics_are_multi_valued(profile):
    for topic, ev in (("前任", "别再提我前任了"), ("体重", "也别聊体重")):
        profile.preferences.set_explicit("avoid_topic", topic, evidence=ev, user_message=ev)
    assert [a["value"] for a in profile.preferences.effective()["avoid_topic"]] == ["前任", "体重"]


# --- onboarding -------------------------------------------------------------------------------------

@pytest.fixture()
def planner(profile, violetdb):
    from ai.onboarding import OnboardingPlanner
    return OnboardingPlanner(violetdb, profile.facts)


def test_one_question_at_most_and_not_again_soon(planner, violetdb):
    first = planner.hint("早呀")
    assert first["key"] == "address_name"
    assert planner.hint("嗯嗯") is None                                # rate limit: hours and turns


@pytest.mark.parametrize("message,route", [
    ("今天被导师骂了，好难过", None),
    ("明天考试我还一点都没复习，你说我该先看哪一章？", None),
    ("你觉得这个好吗", None),
    ("帮我点个外卖", "task"),
])
def test_no_question_while_the_user_has_something_important(planner, message, route):
    assert planner.hint(message, route=route) is None


def test_known_and_declined_keys_are_never_asked(planner, profile, violetdb):
    profile.facts.remember("address_name", "小满", evidence="叫我小满", user_message="叫我小满")
    offer = planner.hint("早上好")
    assert offer["key"] == "identity.status"
    assert planner.note_user_turn("这个就不说了吧") == "identity.status"
    violetdb.execute("UPDATE profile_questions SET created_at = ?", ((now(violetdb) - timedelta(days=30)).isoformat(),))
    for i in range(8):
        violetdb.add_user_message("text", f"随便聊{i}")
    assert planner.hint("嗯")["key"] == "residence.usual"


def test_onboarding_can_be_switched_off(planner, monkeypatch):
    monkeypatch.setenv("VIOLET_ONBOARDING", "0")
    assert planner.hint("早") is None


# --- time zone ----------------------------------------------------------------------------------

def test_configurable_iana_time_zone(monkeypatch):
    from ai.memory_model import resolve_time
    monkeypatch.setenv("VIOLET_TIMEZONE", "America/New_York")
    winter = datetime.fromisoformat("2026-01-10T20:00:00-05:00")
    summer = datetime.fromisoformat("2026-07-10T20:00:00-04:00")
    assert resolve_time(date_hint="tomorrow", time_hint="09:00", now=winter) == "2026-01-11T09:00:00-05:00"
    assert resolve_time(date_hint="tomorrow", time_hint="09:00", now=summer) == "2026-07-11T09:00:00-04:00"


def test_default_zone_and_fallback(monkeypatch, caplog):
    from ai.settings import local_timezone, now_local
    monkeypatch.delenv("VIOLET_TIMEZONE", raising=False)
    assert now_local().utcoffset() == timedelta(hours=8)
    monkeypatch.setenv("VIOLET_TIMEZONE", "Not/AZone")
    assert local_timezone().utcoffset(None) == timedelta(hours=8)
    assert "falling back" in caplog.text
