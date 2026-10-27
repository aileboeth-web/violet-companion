"""Phase 7 B.9.1: plan checks by category (PASS / FAIL / NOT_APPLICABLE / UNSUPPORTED), db: commitment_created,
and experiential claims ("我也爬过山") in the product's consistency guard. Offline, fictional data."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
USER_26 = "提醒我明天早上八点交作业"
ROUND_TIME = "2026-10-22T16:10:00+08:00"


def _plan_rounds():
    from tools.replay import load_plan
    return {r["n"]: r for r in load_plan(PROJECT / "tests" / "samples" / "acceptance_dialogue_30.json")[0]["rounds"]}


def _facts(**over):
    base = {"tool_called": False, "generated": False, "violet_scene": False, "sent_recorded": False, "provider": "off",
            "non_mock_calls": []}
    return {**base, **over}


@pytest.fixture()
def clean_env():
    saved = dict(os.environ)
    yield os.environ
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture()
def run_eval(clean_env, violetdb, tmp_path, monkeypatch):
    from tools import dialogue_eval, violet_test_chat
    real_open = violet_test_chat.open_session
    monkeypatch.setattr(violet_test_chat, "open_session",
                        lambda args, input_fn=input, **kw: real_open(args, input_fn=input_fn, out=lambda s: None, db=violetdb, **kw))
    counter = {"n": 0}

    def run(*argv):
        counter["n"] += 1
        out = tmp_path / f"out{counter['n']}"
        rc = dialogue_eval.main([*argv, "--output-dir", str(out)])
        return rc, json.loads((out / "report.json").read_text(encoding="utf-8")) if (out / "report.json").exists() else None
    return run


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

def test_round23_web_search_is_not_applicable_and_never_an_image_failure():
    from tools.dialogue_eval import NOT_APPLICABLE, picture_checks, plan_checks
    rnd = _plan_rounds()[23]
    checks = plan_checks(rnd, {"image": True, "web": False}, set(), _facts())
    assert checks == [{"check": "tools_has:violet-web__web_search", "category": "web", "status": NOT_APPLICABLE,
                       "reason": checks[0]["reason"]}]
    assert "网页搜索未测试" in checks[0]["reason"]
    assert picture_checks(rnd, _facts(), None, [], {"image": True, "web": False}, checks) is None


def test_an_enabled_tool_still_passes_or_fails():
    from tools.dialogue_eval import FAIL, PASS, plan_checks
    rnd = _plan_rounds()[23]
    on = {"image": True, "web": True}
    assert plan_checks(rnd, on, {"violet-web__web_search"}, _facts())[0]["status"] == PASS
    missed = plan_checks(rnd, on, set(), _facts())[0]
    assert missed["status"] == FAIL and missed["category"] == "web"


def test_unknown_tools_and_db_checks_are_reported_unsupported():
    from tools.dialogue_eval import UNSUPPORTED, plan_checks
    rnd = {"n": 99, "checks": {"tools_has": ["calendar__add"], "tools_lacks": ["mystery"], "db": "something_new"}}
    statuses = {c["check"]: c["status"] for c in plan_checks(rnd, {"image": True, "web": True}, set(), _facts())}
    assert statuses == {"tools_has:calendar__add": UNSUPPORTED, "tools_lacks:mystery": UNSUPPORTED,
                        "db:something_new": UNSUPPORTED}


def test_images_off_makes_picture_checks_not_applicable_never_a_pass_or_a_model_failure():
    from tools.dialogue_eval import NOT_APPLICABLE, picture_checks, plan_checks
    rounds = _plan_rounds()
    off = {"image": False, "web": False}
    c21 = plan_checks(rounds[21], off, set(), _facts())
    assert {c["status"] for c in c21} == {NOT_APPLICABLE}
    p21 = picture_checks(rounds[21], _facts(), None, [], off, c21)
    assert p21["results"] == {} and p21["failures"] == {} and p21["valid"] is None and p21["status"] == "not_applicable"
    c22 = plan_checks(rounds[22], off, set(), _facts())
    p22 = picture_checks(rounds[22], _facts(), {"required": True, "met": False}, [], off, c22)
    assert p22["failures"] == {} and "premise" in p22["not_applicable"] and p22["valid"] is None
    # a picture claimed while the feature is off is still a real failure of the reply
    flagged = picture_checks(rounds[21], _facts(), None, ["claims_picture_without_one_sent"], off, c21)
    assert "reply:claims_picture_without_one_sent" in flagged["failures"]


def test_with_image_mock_the_picture_rules_are_unchanged():
    from tools.dialogue_eval import picture_checks
    rounds = _plan_rounds()
    good = _facts(tool_called=True, generated=True, violet_scene=True, sent_recorded=True, provider="mock")
    p21 = picture_checks(rounds[21], good, None, [])
    assert p21["results"] == {"tools_has:create_image": True, "db:violet_picture_sent": True} and p21["valid"] is True
    bad = picture_checks(rounds[22], _facts(provider="mock"), {"required": True, "met": False}, [])
    assert bad["valid"] is False and "premise" in bad["failures"]


def test_summary_keeps_categories_apart_and_untested_is_never_a_plain_pass(run_eval):
    from tools.dialogue_eval import summarize
    rc, report = run_eval("--rounds", "23", "--image-mock")
    assert rc == 0
    r23 = report["rounds"][0]
    assert r23["image_checks"] is None and r23["checks"][0]["status"] == "NOT_APPLICABLE"
    for r in report["rounds"]:
        r["score"] = 2
    summary = summarize(report)
    assert summary["image_check_failures"] == [] and summary["check_failures"] == []
    assert [u["check"] for u in summary["untested_checks"]] == ["tools_has:violet-web__web_search"]
    assert summary["verdict"] == "pass_with_untested_checks"


def test_an_image_failure_is_counted_once_and_only_as_an_image_failure(run_eval):
    from tools.dialogue_eval import summarize
    rc, report = run_eval("--rounds", "21", "--image-mock")     # the mock chat model never draws
    assert rc == 0
    report["rounds"][0]["score"] = 2
    summary = summarize(report)
    assert "tools_has:create_image" in [f["check"] for f in summary["image_check_failures"]]
    assert summary["check_failures"] == [] and summary["verdict"] == "fail"


# ---------------------------------------------------------------------------
# db: commitment_created
# ---------------------------------------------------------------------------

def _mark(violetdb):
    return int(violetdb.fetch_one("SELECT COALESCE(MAX(id), 0) AS m FROM commitments")["m"])


@pytest.mark.parametrize("content,due,evidence,status,expected", [
    ("交作业", "2026-10-23T08:00:00+08:00", USER_26, "pending", "PASS"),
    ("提醒用户提交作业", "2026-10-23T00:00:00+00:00", "提醒我明天早上八点交作业", "pending", "PASS"),   # same instant
    ("交作业", "2026-10-23T09:00:00+08:00", USER_26, "pending", "FAIL"),                           # default time
    ("交作业", "2026-10-22T08:00:00+08:00", USER_26, "pending", "FAIL"),                           # wrong day
    ("交作业", "2026-10-23T08:00:00+08:00", USER_26, "cancelled", "FAIL"),
    ("交作业", "2026-10-23T08:00:00+08:00", "我明天早上八点交作业", "pending", "FAIL"),              # not a request
    ("交作业", "2026-10-23T08:00:00+08:00", "提醒我明天交论文", "pending", "FAIL"),                  # not the user's words
    ("买牛奶", "2026-10-23T08:00:00+08:00", USER_26, "pending", "FAIL"),                           # another thing
])
def test_commitment_created_checks_every_field(clean_env, violetdb, content, due, evidence, status, expected):
    from tools.dialogue_eval import commitment_check
    mark = _mark(violetdb)
    row = violetdb.add_commitment(content, due, evidence=evidence, source_turn_id="t-26")["commitment"]
    if status != "pending":
        violetdb.update_commitment(row["id"], status=status)
    result = commitment_check(violetdb, _plan_rounds()[26], mark, USER_26, ROUND_TIME)
    assert result["status"] == expected, result


def test_no_new_commitment_fails_even_when_an_old_one_exists(clean_env, violetdb):
    from tools.dialogue_eval import commitment_check
    violetdb.add_commitment("交作业", "2026-10-23T08:00:00+08:00", evidence=USER_26, source_turn_id="earlier")
    result = commitment_check(violetdb, _plan_rounds()[26], _mark(violetdb), USER_26, ROUND_TIME)
    assert result["status"] == "FAIL" and "没有新建提醒" in result["reason"]


def test_commitment_check_without_a_planned_reminder_is_unsupported(clean_env, violetdb):
    from tools.dialogue_eval import commitment_check
    result = commitment_check(violetdb, {"n": 99, "memory": []}, 0, USER_26, ROUND_TIME)
    assert result["status"] == "UNSUPPORTED"


def test_round26_through_the_product_memory_tool(run_eval, violetdb):
    """The scripted memory step goes through MemoryAgent.create_commitment (the product's own rules and
    storage); the check then finds it in this round at the planned time."""
    rc, report = run_eval("--rounds", "26", "--scripted-memory")
    assert rc == 0
    r26 = report["rounds"][0]
    assert r26["checks"] == [{"check": "db:commitment_created", "category": "memory", "status": "PASS", "reason": ""}]
    row = violetdb.fetch_one("SELECT * FROM commitments WHERE id = ?", (r26["commitment"]["commitment_id"],))
    assert row["status"] == "pending" and row["evidence"] == USER_26
    assert row["due_at"][11:16] == "08:00"


def test_round26_without_the_memory_write_fails_in_the_report(run_eval, tmp_path):
    from tools.dialogue_eval import summarize
    # nothing may write the reminder: the B.10.1 step before the reply is switched off (its own tests:
    # test_reminder_b101.py), and the mock memory model writes nothing
    config = tmp_path / "no-reminder-before-reply.env"
    config.write_text("VIOLET_REMINDER_BEFORE_REPLY=0\n", encoding="utf-8")
    rc, report = run_eval("--rounds", "26", "--config", str(config))
    assert rc == 0
    assert report["rounds"][0]["checks"][0]["status"] == "FAIL"
    report["rounds"][0]["score"] = 2
    summary = summarize(report)
    assert [f["check"] for f in summary["check_failures"]] == ["db:commitment_created"]
    assert summary["image_check_failures"] == [] and summary["verdict"] == "fail"


# ---------------------------------------------------------------------------
# Experiential claims in the product's consistency guard
# ---------------------------------------------------------------------------

@pytest.fixture()
def guard(violetdb):
    from ai.consistency import ConsistencyGuard
    from ai.life_store import LifeStore
    store = LifeStore(violetdb)
    thread = store.create_thread("eval:读《边城》", "reading", "读《边城》", "边城", "2026-10-08T09:00:00+08:00")
    store.insert_event(uid="eval:边城", thread_id=thread, plan_id=None, event_type="progress", content="又往后读了几章《边城》",
                       occurred_at="2026-10-12T09:00:00+08:00", duration_minutes=60, source="dialogue_eval")
    violetdb.add_event_memory("用户周末去爬了泰山，累坏了", kind="event", progress="done")
    return ConsistencyGuard(violetdb)


def _checks(guard, line):
    return [v.check for v in guard.check_reply(line, now="2026-10-22T16:10:00+08:00")]


@pytest.mark.parametrize("line", [
    "我也爬过山，山顶的风特别大",
    "我去过泰山",
    "以前我爬过一次黄山",
    "我也去过，确实累",
    "我住过一阵子海边",
    "我上个月爬了一次山",                      # the completed form: unchanged
])
def test_unrecorded_experiences_are_flagged(guard, line):
    assert set(_checks(guard, line)) & {"unrecorded_experience", "user_experience_as_own"}


def test_the_users_own_trip_told_as_evas_is_named_as_such(guard):
    assert "user_experience_as_own" in _checks(guard, "我也爬过泰山，确实累坏了")


@pytest.mark.parametrize("line", [
    "我没去过泰山",
    "我从来没爬过山",
    "我从未去过那么高的地方",
    "我们一起去过吗",
    "你说你去过泰山，真厉害",
    "如果我去过就好了",
    "我下次想去爬山",
    "我读过《边城》",                          # backed by the recorded reading only when 读过 is read as 读了
    "我读过《边城》，后面几章还没看",          # backed by the recorded reading
    "我也读过几章《边城》",
    "我这就跑过去",                            # a direction, not an experience
    "爬山的时候要注意休息",
])
def test_negations_questions_quotes_hypotheses_and_recorded_events_are_not_flagged(guard, line):
    assert not set(_checks(guard, line)) & {"unrecorded_experience", "user_experience_as_own", "plan_described_as_done"}


def test_no_mountain_specific_rule_exists():
    """The fix is about the experiential aspect, not about this scenario's words."""
    from ai import consistency
    patterns = [consistency.EXPERIENCE_CLAIM_RE.pattern, consistency.EXPERIENCE_NOT_A_CLAIM_RE.pattern,
                consistency.EXPERIENCE_AS_DONE_RE.pattern]
    assert not any(w in p for p in patterns for w in ("泰山", "黄山", "爬山", "山"))
