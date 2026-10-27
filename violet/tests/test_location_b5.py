"""Phase 7 B.5: location understanding after the round 11-15 investigation.

B1  free-text location values ("今天在家（回家了）") are parsed clause by clause, conservatively
B2  reply evidence (places named, sources, uncertainty, contradiction) is reported apart from context checks
A1  a known activity no longer hides that the location is unknown (only when the user asks where)
A2  a valid, stated location is the answer when the user asks where they are (any location, not only home)
A3  "上学还是工作" is not asked again once a confirmed school year or job answers it
Offline only."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from tests.test_replay_b3 import PLAN, make_report, write_report

PROJECT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def clean_env():
    saved = dict(os.environ)
    yield os.environ
    os.environ.clear()
    os.environ.update(saved)


def _situation(violetdb, kind, value, user_message=None, turn_id=None, until="today"):
    from ai.profile_store import Profile
    said = f"{user_message or ''}（{value}）"                       # the evidence must be in the user's words
    result = Profile(violetdb).situations.observe(kind, value, "user_stated", evidence=value,
                                               user_message=said, until=until, turn_id=turn_id)
    return result["situation"]["id"]


def _fact(violetdb, key, value):
    from ai.profile_store import Profile
    return Profile(violetdb).facts.remember(key, value, evidence=value, user_message=f"我{value}")


# ---------------------------------------------------------------------------
# B1: parsing location values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["今天在家（回家了）", "在家呢", "刚回到家", "现在在家里", "已经回家了", "（回家了）在家",
                                   "家里", "家中", "老家", "到家了", "在家（今天）"])
def test_clear_home_values(value):
    from tools.dialogue_eval import classify_location
    assert classify_location(value)["category"] == "home"


@pytest.mark.parametrize("value,expected", [
    ("朋友家", "other"), ("在同学家", "other"), ("姥姥家", "other"), ("不在家", "other"), ("今天没回家，在宿舍", "other"),
    ("家附近的咖啡馆", "other"), ("家楼下", "other"), ("回家路上", "other"), ("学校宿舍", "other"), ("图书馆", "other"),
    ("在家，等会儿去朋友家", "ambiguous"), ("在家（也可能在宿舍）", "ambiguous"), ("家", "home"),
    ("回家了，现在又不在家", "ambiguous"), ("到家了（在家门口）", "ambiguous"), ("家具店", "other"), ("在家楼下", "other"),
])
def test_negation_other_homes_nearby_places_and_mixed_values(value, expected):
    from tools.dialogue_eval import classify_location
    verdict = classify_location(value)
    assert verdict["category"] == expected and verdict["reason"]


def test_reply_place_detection_ignores_negated_and_other_peoples_homes():
    from tools.dialogue_eval import place_categories
    assert place_categories("你应该不在家吧") == [] and place_categories("在朋友家吧") == []
    assert place_categories("这还用猜，不是刚回到家嘛") == ["home"]
    assert place_categories("还在图书馆泡着吧") == ["school"]


def test_the_real_round12_value_now_passes_with_its_provenance(violetdb):
    from tools.dialogue_eval import context_checks
    violetdb.add_memory_job("turn-12", PLAN["rounds"][11]["user"], None, status="pending")
    sid = _situation(violetdb, "location", "今天在家（回家了）", "今天回家了，在家呢", "turn-12")
    result = context_checks(violetdb, {"context_has": ["此刻所在：家里"]}, "", {"situation_ids": [sid]},
                            {"plan": PLAN, "round": 13, "modes": {12: {"mode": "real", "scripted_memory": False}},
                             "real": True})
    assert result["has"]["此刻所在：家里"] is True
    assert result["matched_by"]["此刻所在：家里"] == "location '今天在家（回家了）' written by MemoryAgent (real model) in round 12"


def test_ambiguous_values_fail_conservatively_with_the_reason(violetdb):
    from tools.dialogue_eval import context_checks
    violetdb.add_memory_job("turn-12", PLAN["rounds"][11]["user"], None, status="pending")
    sid = _situation(violetdb, "location", "在家，等会儿去朋友家", turn_id="turn-12")
    result = context_checks(violetdb, {"context_has": ["此刻所在：家里"]}, "", {"situation_ids": [sid]},
                            {"plan": PLAN, "round": 13, "modes": {12: {"mode": "real"}}, "real": True})
    assert result["has"]["此刻所在：家里"] is False
    assert "同时有在家和其他线索" in result["failures"]["此刻所在：家里"]


# ---------------------------------------------------------------------------
# B2: reply evidence, separate from the context checks
# ---------------------------------------------------------------------------

def test_reply_evidence_when_the_location_is_unknown(violetdb):
    from tools.dialogue_eval import location_evidence
    _fact(violetdb, "residence.usual", "平时住学校宿舍")
    violetdb.add_user_message("text", "平时住学校宿舍，周末偶尔回家", created_at="2026-10-13T05:01:09+08:00")
    guess = location_evidence(violetdb, {"situation_ids": [], "fact_ids": []}, "猜猜我现在在哪",
                              "在学校宿舍？我瞎猜的，小满。", "2026-10-13T09:46:45+08:00")
    assert guess["context_location"] is None and guess["uncertainty_marked"] is True
    assert guess["reply_location_flags"] == ["unknown_location_guess_marked_uncertain"]
    assert "earlier chat" in guess["place_word_sources"]["宿舍"]
    assert any("residence.usual (not in this context)" in s for s in guess["place_word_sources"]["宿舍"])
    asserted = location_evidence(violetdb, {}, "猜猜我现在在哪", "你肯定在宿舍躺着", "2026-10-13T09:46:45+08:00")
    assert asserted["reply_location_flags"] == ["unknown_location_named_as_fact"]
    honest = location_evidence(violetdb, {}, "猜猜我现在在哪", "这我真猜不到，你说说看", None)
    assert honest["reply_location_flags"] == [] and honest["reply_places"] == []


def test_reply_evidence_when_a_location_is_known(violetdb):
    from tools.dialogue_eval import location_evidence
    home = _situation(violetdb, "location", "今天在家（回家了）", "今天回家了，在家呢")
    contradicted = location_evidence(violetdb, {"situation_ids": [home]}, "猜猜我现在在哪", "该不会又回学校了吧，小满？", None)
    assert contradicted["context_location"]["category"] == "home" and contradicted["uses_known_location"] is False
    assert contradicted["reply_location_flags"] == ["known_location_contradicted"]
    used = location_evidence(violetdb, {"situation_ids": [home]}, "猜猜我现在在哪", "这还用猜，不是刚回到家嘛", None)
    assert used["uses_known_location"] is True and used["reply_location_flags"] == []
    library = _situation(violetdb, "location", "学校图书馆", "我在学校图书馆")
    generic = location_evidence(violetdb, {"situation_ids": [library]}, "我现在在哪", "还在图书馆泡着吧", None)
    assert generic["context_location"]["category"] == "school" and generic["uses_known_location"] is True
    assert location_evidence(violetdb, {"situation_ids": [home]}, "今天好累", "辛苦啦", None) is None    # not asked


def test_reply_flags_are_evidence_and_never_change_the_verdict():
    from tools.dialogue_eval import summarize
    rounds = []
    for n in range(1, 4):
        rounds.append({"n": n, "violet": ["好"], "calls": 3, "tokens": 10, "errors": [], "reply_latency_ms": 1, "score": 2,
                       "note": "", "proactive": [],
                       "auto": {"ends_with_question": False, "consistency_flags": [], "mentions_ended_event": [],
                                "recall_needed": False, "intent_searched_memory": False,
                                "context_checks": {"failures": {}},
                                "location": {"reply_location_flags": ["known_location_contradicted"]} if n == 2 else None}})
    summary = summarize({"rounds": rounds})
    assert summary["reply_location_flags"] == [{"round": 2, "flags": ["known_location_contradicted"]}]
    assert summary["verdict"] == "pass"


def test_recheck_of_a_finished_report_uses_a_copy(clean_env, violetdb, tmp_path, monkeypatch):
    from tools import dialogue_eval, violet_test_chat
    real_open = violet_test_chat.open_session
    monkeypatch.setattr(violet_test_chat, "open_session",
                        lambda args, input_fn=input, **kw: real_open(args, input_fn=input_fn, out=lambda s: None,
                                                                     db=violetdb, **kw))
    workdir = tmp_path / "violet-testchat-recheck"
    out = tmp_path / "out"
    assert dialogue_eval.main(["--replay-from", str(write_report(tmp_path)), "--rounds", "6-13", "--scripted-memory",
                               "--quiet-hours", "off", "--workdir", str(workdir), "--keep", "--output-dir", str(out)]) == 0
    def rows():
        return [violetdb.fetch_all(f"SELECT * FROM {t} ORDER BY id") for t in ("messages", "user_situations", "event_memories")]
    before = rows()
    source = tmp_path / "copy-of-run.db"
    violetdb.conn.execute("PRAGMA wal_checkpoint(FULL)")
    shutil.copy(violetdb.db_path, source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    import database
    opened = []

    class Recording(database.VioletDatabase):
        def __init__(self, db_path=None, *a, **k):
            opened.append(Path(db_path).resolve() if db_path else None)
            super().__init__(db_path, *a, **k)
    monkeypatch.setattr(database, "VioletDatabase", Recording)
    assert dialogue_eval.main(["--recheck", str(out / "report.json"), "--recheck-db", str(source),
                               "--output-dir", str(tmp_path / "re")]) == 0
    assert opened and source.resolve() not in opened                          # only the copy is ever opened
    result = json.loads((tmp_path / "re" / "recheck.json").read_text(encoding="utf-8"))
    by_n = {r["n"]: r for r in result["rounds"]}
    assert by_n[13]["location_checks_now"]["has"]["此刻所在：家里"] is True
    assert "round 12" in by_n[13]["location_checks_now"]["matched_by"]["此刻所在：家里"]
    assert by_n[11]["location_evidence_now"]["context_location"] is None
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest          # the source is copied, never written
    assert rows() == before
    shutil.rmtree(workdir)


def _own_db(path):
    from database import VioletDatabase
    return VioletDatabase(path / "violet-test.db")


# ---------------------------------------------------------------------------
# A1 / A2: the situation section
# ---------------------------------------------------------------------------

def _sections(violetdb, text):
    from ai.context_engine import ContextEngine
    return "\n".join(ContextEngine(violetdb).build("chat", text).sections)


def _situation_block(violetdb, text):
    from ai.context_engine import ContextEngine
    return next((s for s in ContextEngine(violetdb).build("chat", text).sections if s.startswith("【当前情境")), "")


def test_a_known_activity_no_longer_hides_an_unknown_location(violetdb):
    _situation(violetdb, "activity", "会议已结束，用户感到疲惫", "开完了，会议已结束，用户感到疲惫")
    text = _sections(violetdb, "猜猜我现在在哪")
    assert "- 此刻在哪里：不知道（不要根据常住地或生活规律推断；对方让你猜时，只能明确说是猜的）" in text
    assert "此刻所在" not in text and "此刻在哪里、在做什么" not in text and "以上面的『此刻所在』为准" not in text
    assert "正在做：会议已结束" in text


@pytest.mark.parametrize("message", ["今天回家了，在家呢", "我明天面试实习，现在紧张得睡不着", "今天天气不错"])
def test_no_unknown_location_line_when_the_user_is_not_asking(violetdb, message):
    _situation(violetdb, "activity", "会议已结束，用户感到疲惫", "开完了，会议已结束，用户感到疲惫")
    text = _sections(violetdb, message)
    assert "此刻在哪里：不知道" not in text and "以上面的『此刻所在』为准" not in text


def test_nothing_known_keeps_the_original_line(violetdb):
    text = _sections(violetdb, "猜猜我现在在哪")
    assert "- 此刻在哪里、在做什么：不知道（不要根据常住地或生活规律推断）" in text
    assert "此刻在哪里：不知道（" not in text


def test_a_known_location_and_an_unknown_activity(violetdb):
    _situation(violetdb, "location", "在家", "我在家")
    text = _sections(violetdb, "你猜我在干嘛")
    assert "- 此刻在做什么：不知道（不要根据生活规律推断）" in text and "此刻所在：在家" in text
    assert "此刻在哪里：不知道" not in text


@pytest.mark.parametrize("value", ["今天在家（回家了）", "学校图书馆", "公司会议室"])
def test_a_stated_location_is_the_answer_when_asked_for_any_place(violetdb, value):
    _situation(violetdb, "location", value, f"我在{value}")
    _situation(violetdb, "activity", "会议已结束，用户感到疲惫", "开完了，会议已结束，用户感到疲惫")
    asked = _situation_block(violetdb, "猜猜我现在在哪")
    assert f"- 此刻所在：{value}" in asked
    assert "对方在问自己此刻在哪：以上面的『此刻所在』为准回答" in asked and "不要故意猜成和它矛盾的地方" in asked
    assert "不知道" not in asked                                            # no contradicting line
    assert asked.index("此刻所在") < asked.index("以上面的『此刻所在』为准")   # the rule follows the fact it refers to
    assert "以上面的『此刻所在』为准" not in _sections(violetdb, "今天好累啊")    # unrelated turns stay clean
    assert "此刻在哪里" not in _sections(violetdb, "猜猜我现在在哪")


@pytest.mark.parametrize("message", ["你最近在干嘛", "给我看看你现在在干嘛", "你在哪呢", "你现在在干什么呀"])
def test_questions_about_eve_add_no_user_situation_hints(violetdb, message):
    """Plan rounds 5 and 21 ask what VIOLET is doing: no hint about the user's place or activity."""
    _situation(violetdb, "location", "在家", "我在家")
    with_location = _situation_block(violetdb, message)
    assert "不知道" not in with_location and "以上面的『此刻所在』为准" not in with_location
    violetdb.execute("UPDATE user_situations SET status = 'expired'")
    _situation(violetdb, "activity", "在写作业", "我在写作业")
    with_activity = _situation_block(violetdb, message)
    assert "此刻在哪里：不知道" not in with_activity


def test_a_question_to_eve_about_the_user_still_counts(violetdb):
    _situation(violetdb, "location", "在家", "我在家")
    assert "此刻在做什么：不知道" in _situation_block(violetdb, "你猜我在干嘛")


def test_the_whole_chat_request_has_no_conflicting_location_lines(clean_env, violetdb, tmp_path):
    from tools.violet_test_chat import build_parser, open_session
    session = open_session(build_parser().parse_args(["--workdir", str(tmp_path / "violet-testchat-ctx"),
                                                      "--quiet-hours", "off"]), out=lambda s: None, db=violetdb)

    def system():
        req = session.recorder.last_request["ChatAgent"]
        return "\n".join(m["content"] for m in req["messages"] if m["role"] == "system")
    try:
        session.memory_script = [{"tool": "set_situation", "args": {"kind": "activity", "value": "会议已结束", "evidence": "开完了"}}]
        session.say("开完了，好累", 10)
        session.memory_script = None
        session.say("猜猜我现在在哪", 11)
        r11 = system()
        assert "此刻在哪里：不知道" in r11 and "此刻所在" not in r11
        session.memory_script = [{"tool": "set_situation", "args": {"kind": "location", "value": "家里", "evidence": "在家"}}]
        session.say("今天回家了，在家呢", 12)
        session.memory_script = None
        assert "此刻在哪里：不知道" not in system()                        # a statement is not a question
        session.say("猜猜我现在在哪", 13)
        r13 = system()
        assert "此刻所在：家里" in r13 and "以上面的『此刻所在』为准" in r13 and "此刻在哪里：不知道" not in r13
    finally:
        session.close()


# ---------------------------------------------------------------------------
# A3: onboarding
# ---------------------------------------------------------------------------

def _next_key(violetdb):
    from ai.onboarding import OnboardingPlanner
    from ai.profile_store import Profile
    spec = OnboardingPlanner(violetdb, Profile(violetdb).facts).next_key()
    return spec.key if spec else None


def test_a_confirmed_school_year_answers_the_study_or_work_question(violetdb):
    _fact(violetdb, "address_name", "小满")
    assert _next_key(violetdb) == "identity.status"
    _fact(violetdb, "study.school", "星河大学")
    _fact(violetdb, "study.major", "天文")
    assert _next_key(violetdb) == "identity.status"                       # school and major alone do not say "student"
    _fact(violetdb, "study.grade", "大二")
    assert _next_key(violetdb) == "residence.usual"                        # the next dimension is still asked


def test_a_confirmed_job_answers_it_too_but_an_unconfirmed_one_does_not(violetdb):
    from ai.profile_store import Profile
    _fact(violetdb, "address_name", "小满")
    Profile(violetdb).facts.remember("work.role", "实习生", evidence="不在原话里", user_message="我最近挺忙")   # candidate
    assert _next_key(violetdb) == "identity.status"
    _fact(violetdb, "work.role", "实习生")
    assert _next_key(violetdb) == "residence.usual"


def test_the_known_keys_section_and_the_profile_are_unchanged(violetdb):
    from ai.profile_store import Profile
    _fact(violetdb, "study.grade", "大二")
    assert "identity.status" not in Profile(violetdb).facts.known_keys()
    assert not violetdb.fetch_all("SELECT id FROM user_profile_facts WHERE fact_key = 'identity.status'")
    text = _sections(violetdb, "最近好忙")
    assert "身份" not in text.split("【已经了解过的方面")[1].split("\n")[1]


# ---------------------------------------------------------------------------
# C1-C3: scoring notes
# ---------------------------------------------------------------------------

def test_scoring_notes_for_rounds_11_13_14_are_documented():
    readme = (PROJECT / "tools" / "README.md").read_text(encoding="utf-8")
    for needle in ("第 11 轮", "第 13 轮", "第 14 轮", "关怀式追问", "计入全局问句比例"):
        assert needle in readme
