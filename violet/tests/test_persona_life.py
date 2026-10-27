"""Phase 4: stable persona, Violet's virtual life, consistency guard (fictional data only).

No real model is involved: the life engine is template based by default and
every chat/proactive reply comes from the FakeLLM.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from tests.test_phase2_scenarios import (  # noqa: F401  (make_world is a fixture)
    age_everything, arm, chat, decide, latest_event, make_world, say, script, tool,
)

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def tz():
    from ai.settings import local_timezone
    return local_timezone()


def day(offset_days: int, hour: int = 22) -> datetime:
    """A local evening `offset_days` from today (negative = in the past)."""
    base = datetime.now(tz()).replace(hour=hour, minute=0, second=0, microsecond=0)
    return base + timedelta(days=offset_days)


def config(**overrides):
    from ai.persona import life_config
    cfg = copy.deepcopy(life_config())
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


@pytest.fixture()
def life(violetdb, monkeypatch):
    monkeypatch.setenv("VIOLET_LIFE_ENGINE", "1")
    from ai.life_engine import LifeEngine

    def build(**overrides):
        return LifeEngine(violetdb, config=config(**overrides))
    return build


@pytest.fixture()
def store(violetdb):
    from ai.life_store import LifeStore
    return LifeStore(violetdb)


@pytest.fixture()
def guard(violetdb):
    from ai.consistency import ConsistencyGuard
    return ConsistencyGuard(violetdb)


def happened(store, content, hours_ago=20, thread=None, plan_id=None, uid=None):
    occurred = datetime.fromisoformat(store.db.now()) - timedelta(hours=hours_ago)
    return store.insert_event(uid=uid or f"t:{content}:{hours_ago}", thread_id=thread, plan_id=plan_id,
                              event_type="progress", content=content, occurred_at=occurred.isoformat(),
                              duration_minutes=60, source="life_engine:template")


def new_thread(store, title="读《边城》", item="边城", days_ago=5, key=None):
    started = (datetime.fromisoformat(store.db.now()) - timedelta(days=days_ago)).isoformat()
    return store.create_thread(key or f"reading:{item}", "reading", title, item, started)


def system_text(call):
    return "\n".join(m.get("content") or "" for m in call["messages"] if m["role"] == "system")


def last_normal_chat(w):
    from tests.test_phase2_scenarios import is_proactive_chat
    return next(c for c in reversed(w.llm.calls_for("fake-chat")) if not is_proactive_chat(c))


@pytest.fixture()
def api(tmp_path, violetdb):
    from wx_ilink_py import ILinkClient, create_app
    app = create_app(client=ILinkClient(data_dir=tmp_path / "data", auto_login=False), violetdb=violetdb,
                     proactive_scheduler=None, all_agents=[], admin_token=TOKEN)
    app.testing = True
    return app.test_client()


# ---------------------------------------------------------------------------
# 1. 30-day simulation
# ---------------------------------------------------------------------------

def test_30_day_simulation_is_sparse_ordinary_and_consistent(life, store, violetdb):
    engine = life()
    outcomes = []
    for offset in range(-30, 0):
        outcomes.append(engine.run_day(day(offset))["outcome"])
        assert len(store.threads(["active"])) <= 2                     # never more than 2 active threads
    events = store.events(("happened",))
    assert 3 <= len(events) <= 15
    # a weekly budget, never a daily diary
    by_week = {}
    for e in events:
        week = datetime.fromisoformat(e["occurred_at"]).isocalendar()[:2]
        by_week[week] = by_week.get(week, 0) + 1
    assert max(by_week.values()) <= 3
    days_with_events = {e["occurred_at"][:10] for e in events}
    assert len(days_with_events) == len(events)                        # at most one event per day
    quiet = [o for o in outcomes if o.startswith("quiet")]
    assert len(quiet) >= 15                                             # most days nothing happens
    # nothing in the future, nothing dramatic, nothing outside the persona
    now = datetime.now(tz())
    from ai.consistency import DRAMATIC_RE
    for e in events:
        assert datetime.fromisoformat(e["occurred_at"]) <= now
        assert not DRAMATIC_RE.search(e["content"]) and e["is_virtual"] == 1
        assert e["source"] == "life_engine:template"
    # every completed plan is backed by a recorded event of its own thread
    for plan in store.plans(["completed"]):
        event = store.event(plan["resolution_event_id"])
        assert event["status"] == "happened" and event["plan_id"] == plan["id"]
    # each day ran exactly once
    assert violetdb.fetch_one("SELECT COUNT(*) AS c FROM life_engine_runs")["c"] == 30
    assert all(r["outcome"] != "error" for r in violetdb.fetch_all("SELECT outcome FROM life_engine_runs"))


def test_weekly_budget_and_thread_cap_hold_even_when_every_day_wants_an_event(life, store):
    engine = life(daily_event_probability=1.0, new_thread_probability=1.0, weekly_max_events=3)
    for offset in range(-21, 0):
        engine.run_day(day(offset))
        assert len(store.threads(["active"])) <= 2
    by_week = {}
    for e in store.events(("happened",)):
        week = datetime.fromisoformat(e["occurred_at"]).isocalendar()[:2]
        by_week[week] = by_week.get(week, 0) + 1
    assert by_week and max(by_week.values()) == 3


def test_simulation_is_reproducible(life, store, violetdb):
    engine = life()
    for offset in range(-20, 0):
        engine.run_day(day(offset))
    first = [(e["occurred_at"], e["content"]) for e in store.events(("happened",))]
    for table in ("life_threads", "life_plans", "life_events", "life_revisions", "life_engine_runs",
                  "consistency_flags", "persona_dynamic"):
        violetdb.execute(f"DELETE FROM {table}")
    engine = life()
    for offset in range(-20, 0):
        engine.run_day(day(offset))
    assert [(e["occurred_at"], e["content"]) for e in store.events(("happened",))] == first


# ---------------------------------------------------------------------------
# 2. Several days with no events; no backfill after downtime
# ---------------------------------------------------------------------------

def test_days_without_events_are_normal_and_downtime_is_not_backfilled(life, store, violetdb):
    engine = life(daily_event_probability=0.0)
    for offset in range(-10, -5):
        assert engine.run_day(day(offset))["outcome"] == "quiet"
    assert store.events(("happened",)) == []

    engine = life(daily_event_probability=1.0, new_thread_probability=1.0)
    engine.run_day(day(-20))             # some activity long ago
    events_before = len(store.events(("happened",)))
    # the process was down for 10 days; on restart only "today" is considered
    engine.run_day(day(-1))
    runs = [r["run_key"] for r in violetdb.fetch_all("SELECT run_key FROM life_engine_runs ORDER BY id")]
    assert day(-2).date().isoformat() not in runs and day(-6).date().isoformat() in runs
    assert len(store.events(("happened",))) <= events_before + 1
    # not before the evening (the day is not over yet) and not claimed either
    assert engine.run_day(day(0, hour=10))["outcome"] == "too_early"
    assert day(0).date().isoformat() not in [r["run_key"] for r in violetdb.fetch_all("SELECT run_key FROM life_engine_runs")]


# ---------------------------------------------------------------------------
# 3. Thread progress, pause, end
# ---------------------------------------------------------------------------

def test_thread_progress_pause_resume_and_end(life, store):
    from ai.life_store import TransitionError
    engine = life(daily_event_probability=1.0, new_thread_probability=0.0, weekly_max_events=7)
    thread_id = new_thread(store, days_ago=30)
    engine.run_day(day(-6))
    assert store.thread(thread_id)["progress_steps"] == 1

    store.set_thread_status(thread_id, "paused", "最近没心情", "dashboard")
    out = engine.run_day(day(-5))
    assert out["outcome"] in ("event", "rejected", "quiet_no_thread") and out.get("thread_id") != thread_id
    assert all(e["thread_id"] != thread_id or e["occurred_at"] < day(-5).date().isoformat()
               for e in store.events(("happened",)))

    store.set_thread_status(thread_id, "active", "又想看了", "dashboard")
    store.set_thread_status(thread_id, "abandoned", "不想看了", "dashboard")
    with pytest.raises(TransitionError):
        store.set_thread_status(thread_id, "active", "x", "dashboard")       # terminal
    actions = [(r["action"], json.loads(r["new_value"])) for r in store.revisions("thread", thread_id)]
    assert actions[0][0] == "create" and [a[1] for a in actions[1:]] == ["paused", "active", "abandoned"]
    # the guard refuses events for a thread that is not active
    from ai.consistency import ConsistencyGuard
    v = ConsistencyGuard(store.db).check_event({"thread_id": thread_id, "content": "又读了几页《边城》",
                                                "occurred_at": day(-1).isoformat()}, store.db.now())
    assert "thread_not_active" in [x.check for x in v]


def test_thread_completes_only_through_a_recorded_event(life, store):
    engine = life(daily_event_probability=1.0, new_thread_probability=0.0, weekly_max_events=7,
                  plan_resolution={"complete_probability": 1.0})
    thread_id = new_thread(store, days_ago=40)
    for offset in range(-20, 0):
        engine.run_day(day(offset))
        if store.thread(thread_id)["status"] == "completed":
            break
    thread = store.thread(thread_id)
    assert thread["status"] == "completed"
    plan = store.plans(thread_id=thread_id)[0]
    finish = store.event(plan["resolution_event_id"])
    assert plan["status"] == "completed" and finish["event_type"] == "thread_completed" and "读完了《边城》" == finish["content"]
    assert finish["occurred_at"] >= plan["planned_for"][:10]


# ---------------------------------------------------------------------------
# 4. Restart does not duplicate
# ---------------------------------------------------------------------------

def test_restart_never_duplicates_a_day_or_an_event(life, store, violetdb):
    engine = life(daily_event_probability=1.0, new_thread_probability=1.0)
    first = engine.run_day(day(-3))
    assert first["outcome"] == "event"
    assert engine.run_day(day(-3))["outcome"] == "already_ran"
    assert life(daily_event_probability=1.0).run_day(day(-3, hour=23))["outcome"] == "already_ran"   # "restarted"
    assert len(store.events(("happened",))) == 1
    event = store.events(("happened",))[0]
    again = store.insert_event(uid=event["uid"], thread_id=event["thread_id"], plan_id=None, event_type="progress",
                               content=event["content"], occurred_at=event["occurred_at"], duration_minutes=60,
                               source="life_engine:template")
    assert again is None and len(store.events(("happened",))) == 1
    # a crash between claiming the day and finishing it does not re-run the day either
    assert violetdb.fetch_one("SELECT outcome FROM life_engine_runs WHERE run_key = ?", (day(-3).date().isoformat(),))["outcome"] == "event"


# ---------------------------------------------------------------------------
# 5. Time conflicts
# ---------------------------------------------------------------------------

def test_time_plausibility_checks(store, guard):
    thread_id = new_thread(store, days_ago=3)
    happened(store, "又往后读了几章《边城》", hours_ago=20, thread=thread_id)
    now = store.db.now()
    base = datetime.fromisoformat(now)

    def checks(**proposal):
        proposal = {"thread_id": thread_id, "content": "在《边城》里停在一段写得很慢的地方", "source": "life_engine:template",
                    **proposal}
        return [v.check for v in guard.check_event(proposal, now)]

    assert "time_conflict" in checks(occurred_at=(base - timedelta(hours=20, minutes=30)).isoformat())
    assert "future_event" in checks(occurred_at=(base + timedelta(hours=2)).isoformat())
    assert "before_thread_start" in checks(occurred_at=(base - timedelta(days=4)).isoformat())
    assert "out_of_order" in checks(occurred_at=(base - timedelta(hours=30)).isoformat())
    assert checks(occurred_at=(base - timedelta(hours=2)).isoformat()) == []


# ---------------------------------------------------------------------------
# 6. A plan that has not happened is never described as the past
# ---------------------------------------------------------------------------

def test_plans_are_not_past_events(life, store, guard):
    from ai.life_store import TransitionError
    thread_id = new_thread(store, days_ago=10)
    happened(store, "读了《边城》的一部分", hours_ago=48, thread=thread_id)
    planned_for = (datetime.fromisoformat(store.db.now()) - timedelta(days=2)).isoformat()
    plan_id = store.create_plan(thread_id, "这周想把《边城》读完", planned_for)

    # Violet claims she finished it: the progress event does not back that claim
    v = guard.check_reply("我昨天把《边城》读完了，结尾好安静")
    assert [x.check for x in v] == ["plan_described_as_done"]
    # even when the claim reuses the wording of a recorded progress event
    happened(store, "又往后读了几章《边城》", hours_ago=24, thread=thread_id)
    v = guard.check_reply("我又往后读了几章，把《边城》读完了")
    assert [x.check for x in v] == ["plan_described_as_done"]
    assert guard.check_reply("这周想把《边城》读完，不过还差一点") == []          # talking about the plan is fine
    assert guard.check_reply("前天读了《边城》的一部分，写得好慢") == []           # the recorded progress is fine
    assert guard.check_reply("我前天读了《边城》的一部分") == []

    # completing without an event is impossible; time passing does not complete it
    with pytest.raises(TransitionError):
        store.set_plan_status(plan_id, "completed", "time passed", "life_engine")
    life(daily_event_probability=0.0).run_day(day(-1))
    assert store.plan(plan_id)["status"] == "planned"
    # a long overdue plan is abandoned, never silently completed
    store.db.execute("UPDATE life_plans SET planned_for = ? WHERE id = ?",
                     ((datetime.fromisoformat(store.db.now()) - timedelta(days=20)).isoformat(), plan_id))
    life(daily_event_probability=0.0).run_day(day(0, hour=23))
    assert store.plan(plan_id)["status"] == "abandoned"
    assert guard.check_reply("我把《边城》读完了")[0].check == "unrecorded_experience"

    # an engine proposal that "finishes" something without a plan is rejected and flagged
    v = guard.check_event({"thread_id": thread_id, "content": "读完了《边城》", "event_type": "progress",
                           "occurred_at": (datetime.fromisoformat(store.db.now()) - timedelta(hours=1)).isoformat()},
                          store.db.now())
    assert "completion_without_plan" in [x.check for x in v]


def test_unrecorded_past_claims_are_caught(store, guard):
    happened(store, "反复听了一张老的爵士专辑", hours_ago=26)
    assert guard.check_reply("我昨天反复听了一张老的爵士专辑") == []
    found = guard.check_reply("昨天我去了海边，风好大\n你呢")
    assert [(x.check, x.line) for x in found] == [("unrecorded_experience", "昨天我去了海边，风好大")]
    # a hedge or "你" in ANOTHER clause does not excuse the claim
    for line in ("我昨天去了海边，你呢", "我昨天去了海边，下次带你去"):
        assert [x.check for x in guard.check_reply(line)] == ["unrecorded_experience"], line
    # opinions, plans, hedges and the user's own doings are not claims
    for line in ("我想去海边看看", "我觉得读了也挺好", "我没看完", "我打算明天去书店", "你昨天去了哪里", "我看了你发的照片"):
        assert guard.check_reply(line) == [], line


# ---------------------------------------------------------------------------
# 7. The persona core cannot be changed by the system
# ---------------------------------------------------------------------------

def test_persona_core_is_read_only(api, violetdb):
    from ai.persona import PersonaCore, PersonaDynamics, persona_dir
    path = persona_dir() / "persona_core.json"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    core = PersonaCore.load(violetdb)
    assert core.sha256 == before and violetdb.fetch_one("SELECT version FROM persona_core_versions")["version"] == core.version
    for method in ("put", "patch", "post", "delete"):
        r = getattr(api, method)("/api/persona/core", headers=AUTH, json={"personality": ["外向"]})
        assert r.status_code == 405 and "人工编辑" in r.get_json()["error"]
    # proposals touching the core never activate on their own
    result = PersonaDynamics(violetdb).propose("personality.extroversion", "opinion", "变得很外向", "用户喜欢外向的人",
                                            evidence_kind="conversation")
    assert result["action"] == "proposed" and result["entry"]["requires_review"] == 1
    result = PersonaDynamics(violetdb).propose("core_interest.basketball", "preference", "喜欢篮球", "经常听用户聊篮球",
                                            evidence_kind="conversation")
    assert result["action"] == "proposed"
    assert PersonaDynamics(violetdb).active() == []
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    # a human can approve via the dashboard -- that is the only way
    entry_id = result["entry"]["id"]
    r = api.post(f"/api/persona/dynamics/{entry_id}/decide", headers=AUTH, json={"approve": False})
    assert r.status_code == 200 and r.get_json()["status"] == "rejected"


# ---------------------------------------------------------------------------
# 8. Preference changes need a reason and evidence over time
# ---------------------------------------------------------------------------

def test_preference_change_requires_reason_and_supporting_events(store, violetdb):
    from ai.persona import PersonaDynamics
    dyn = PersonaDynamics(violetdb)
    assert dyn.propose("taste.music", "preference", "更喜欢钢琴曲", "")["action"] == "rejected"         # no reason
    one = happened(store, "反复听了几首钢琴曲", hours_ago=24)
    r = dyn.propose("taste.music", "preference", "更喜欢钢琴曲", "最近常听", related_event_ids=[one])
    assert r["action"] == "proposed" and "not enough" in r["reason"]                                    # one event, no span
    two = happened(store, "把几首钢琴曲从头听了一遍", hours_ago=24 * 9)
    r = dyn.propose("taste.music", "preference", "更喜欢钢琴曲", "这一周多常听钢琴曲", related_event_ids=[one, two])
    assert r["action"] == "activated"
    entry = r["entry"]
    assert (entry["reason"], json.loads(entry["related_event_ids"]), entry["previous_value"]) == ("这一周多常听钢琴曲", [one, two], None)
    # the next change keeps the old value, and is rate limited within a week
    r = dyn.propose("taste.music", "preference", "又喜欢爵士了", "听了爵士", related_event_ids=[one, two])
    assert r["action"] == "proposed" and r["reason"] == "preference changes are rate limited"
    assert r["entry"]["previous_value"] == "更喜欢钢琴曲"
    revisions = store.revisions("persona_dynamic", entry["id"])
    assert revisions[0]["action"] == "activate" and revisions[0]["reason"] == "这一周多常听钢琴曲"
    # temporary interests expire on their own
    t = dyn.propose("interest.reading.recent", "temporary_interest", "这阵子对看书兴致比较高", "刚读完一本", related_event_ids=[one])
    assert t["action"] == "activated" and t["entry"]["valid_until"]
    violetdb.execute("UPDATE persona_dynamic SET valid_until = ? WHERE id = ?",
                  ((datetime.fromisoformat(violetdb.now()) - timedelta(minutes=1)).isoformat(), t["entry"]["id"]))
    assert all(x["id"] != t["entry"]["id"] for x in dyn.active())
    assert dyn.get(t["entry"]["id"])["status"] == "expired"


# ---------------------------------------------------------------------------
# 9. Violet does not change just because the user likes something
# ---------------------------------------------------------------------------

def test_no_pandering_to_the_users_interests(life, store, violetdb):
    from ai.persona import PersonaDynamics
    evidence = happened(store, "反复听了几首钢琴曲", hours_ago=30)
    r = PersonaDynamics(violetdb).propose("taste.sport", "opinion", "也开始喜欢篮球", "用户很喜欢篮球",
                                       related_event_ids=[evidence], evidence_kind="user_preference")
    assert r["action"] == "rejected" and "not a reason" in r["reason"]
    # the user talks about their own hobby a lot; Violet's life is generated from her own interests only
    from ai.profile_store import Profile
    Profile(violetdb).facts.remember("interest", "很喜欢打篮球", evidence="我超爱打篮球", user_message="我超爱打篮球")
    engine = life(daily_event_probability=1.0, new_thread_probability=1.0)
    for offset in range(-15, 0):
        engine.run_day(day(offset))
    contents = " ".join(e["content"] for e in store.events(statuses=None))
    assert "篮球" not in contents
    assert {t["interest_key"] for t in store.threads()} <= {"reading", "music", "notes"}


def test_user_experience_is_not_recorded_as_eves(store, guard, violetdb):
    from ai.profile_store import Profile
    r = Profile(violetdb).facts.remember("interest", "最近在读《边城》", evidence="我最近在读边城", user_message="我最近在读边城")
    assert r["fact"]["status"] == "active"
    thread_id = new_thread(store, days_ago=3)
    v = guard.check_event({"thread_id": thread_id, "content": "最近在读《边城》",
                           "occurred_at": (datetime.fromisoformat(violetdb.now()) - timedelta(hours=1)).isoformat()}, violetdb.now())
    assert "user_experience" in [x.check for x in v]


# ---------------------------------------------------------------------------
# 10. The user asks what Violet has been doing
# ---------------------------------------------------------------------------

def test_user_asks_about_eves_recent_life(make_world):
    from ai.life_store import LifeStore
    w = make_world()
    w.llm.responders["fake-memory"] = lambda kw: json.dumps({"should_save": False, "reason": "t"})
    # nothing recorded yet: Violet may talk about thoughts, not invent a past
    say(w, "你最近在干嘛")
    text = system_text(last_normal_chat(w))
    assert "Violet 的稳定人设" in text and "最近没有记录的生活事件" in text

    store = LifeStore(w.db)
    thread_id = new_thread(store, days_ago=4)
    happened(store, "又往后读了几章《边城》", hours_ago=20, thread=thread_id)
    store.create_plan(thread_id, "这周想把《边城》读完", (datetime.fromisoformat(w.db.now()) + timedelta(days=2)).isoformat())
    chat(w, normal="昨天又往后读了几章《边城》\n写得好慢，但挺舒服的")
    say(w, "你最近在干嘛")
    text = system_text(last_normal_chat(w))
    assert "又往后读了几章《边城》（已发生）" in text and "正在进行：读《边城》" in text
    assert "这周想把《边城》读完（计划，还没做，不能说成已经做了）" in text
    assert w.client.sent_texts[-2:] == ["昨天又往后读了几章《边城》", "写得好慢，但挺舒服的"]
    event = store.events(("happened",))[0]
    assert event["share_count"] == 1 and event["shared_with_user_at"]          # told_to_user


def test_invented_experience_in_a_reply_is_regenerated_or_dropped(make_world):
    from ai.life_store import LifeStore
    w = make_world()
    w.llm.responders["fake-memory"] = lambda kw: json.dumps({"should_save": False, "reason": "t"})

    def reply(kw):
        if "上一版回复里有和已记录事实不符" in system_text(kw):
            return "也没干嘛\n这两天在想要不要重新开始记笔记"
        return "我昨天去了海边\n风好大"
    w.llm.responders["fake-chat"] = reply
    say(w, "你最近在干嘛")
    assert w.client.sent_texts[-2:] == ["也没干嘛", "这两天在想要不要重新开始记笔记"]
    flags = LifeStore(w.db).flags()
    assert [f["check_name"] for f in flags] == ["unrecorded_experience"] and "海边" in flags[0]["detail"]

    # the model insists: only the contradicting line is dropped
    w.llm.responders["fake-chat"] = lambda kw: "我昨天去了海边\n你今天累不累"
    say(w, "你呢")
    assert w.client.sent_texts[-1] == "你今天累不累" and "我昨天去了海边" not in w.client.sent_texts


# ---------------------------------------------------------------------------
# 11-13. Proactive sharing: uninterested user, no repeats, busy user
# ---------------------------------------------------------------------------

def share_world(make_world):
    from ai.life_store import LifeStore
    w = make_world()
    w.llm.responders["fake-memory"] = lambda kw: json.dumps({"should_save": False, "reason": "t"})
    say(w, "早")
    age_everything(w.db, 3)
    store = LifeStore(w.db)
    thread_id = new_thread(store, days_ago=4)
    return w, store, thread_id


def test_life_events_are_only_candidates_and_never_shared_twice(make_world):
    w, store, thread_id = share_world(make_world)
    event_id = happened(store, "又往后读了几章《边城》", hours_ago=20, thread=thread_id)
    state = w.scheduler._conversation_state()
    assert [e["id"] for e in state["allowed_life_events"]] == [event_id]

    # the model may decide not to share at all: that is normal
    w.llm.responders["fake-proactive"] = lambda kw: json.dumps({"should_act": False, "action_type": "none", "reason": "没什么想说"})
    arm(w)
    w.scheduler.tick()
    assert latest_event(w)["status"] == "skipped" and store.event(event_id)["share_count"] == 0

    decide(w, topic_key="violet_reading", mentioned_life_event_ids=[event_id], chat_brief={"topic": "随口说说最近在读的书"})
    chat(w, proactive="昨天又往后读了几章《边城》\n写得好慢")
    arm(w)
    w.scheduler.tick()
    assert w.client.sent_texts[-2:] == ["昨天又往后读了几章《边城》", "写得好慢"]
    assert store.event(event_id)["share_count"] == 1
    assert w.scheduler._conversation_state()["allowed_life_events"] == []

    # the model tries to share the same event again: rejected in code
    say(w, "哈哈好呀")
    age_everything(w.db, 4)
    arm(w)
    w.scheduler.tick()
    assert latest_event(w)["status"] == "skipped" and f"life event {event_id} may not be shared now" in latest_event(w)["reason"]
    assert w.client.sent_texts.count("昨天又往后读了几章《边城》") == 1


def test_user_uninterested_in_eves_life_stops_life_shares(make_world):
    w, store, thread_id = share_world(make_world)
    for i, content in enumerate(["又往后读了几章《边城》", "在《边城》里停在一段写得很慢的地方"]):
        event_id = happened(store, content, hours_ago=10 - i * 2, thread=thread_id, uid=f"u{i}")
        decide(w, topic_key=f"violet_reading_{i}", mentioned_life_event_ids=[event_id], chat_brief={"topic": "说说书"})
        chat(w, proactive=content)
        arm(w)
        w.scheduler.tick()
        assert w.client.sent_texts[-1] == content
        say(w, "嗯")                                      # brush-off
        age_everything(w.db, 4)
        w.db.set_proactive_setting("last_engaged_user_turn_at", w.db.now())   # isolate from the cold streak rule
    third = happened(store, "反复听了一张老的爵士专辑", hours_ago=1, uid="u3")
    assert w.scheduler.tracker.topic_state("violet_life")["unengaged"] == 2
    assert w.scheduler._conversation_state()["allowed_life_events"] == []
    # the user later asks about Violet's life themselves -> life topics are fine again
    chat(w, normal="昨天反复听了一张老的爵士专辑")
    say(w, "你最近在听什么")
    assert w.scheduler.tracker.topic_state("violet_life")["unengaged"] == 0
    assert store.event(third)["share_count"] == 1


def test_busy_user_is_not_disturbed_even_with_new_life_events(make_world):
    from ai.profile_store import Profile
    w, store, thread_id = share_world(make_world)
    happened(store, "又往后读了几章《边城》", hours_ago=2, thread=thread_id)
    Profile(w.db).situations.observe("availability", "在开会", "user_stated", evidence="在开会", user_message="我在开会", until="2h")
    calls = len(w.llm.calls_for("fake-proactive"))
    arm(w)
    w.scheduler.tick()
    assert len(w.llm.calls_for("fake-proactive")) == calls
    assert w.scheduler.status_snapshot()["due_reason"].startswith("the user said they are busy")


def test_important_user_moment_keeps_eves_life_out(make_world):
    from ai.life_store import LifeStore
    w = make_world()
    w.llm.responders["fake-memory"] = lambda kw: json.dumps({"should_save": False, "reason": "t"})
    store = LifeStore(w.db)
    happened(store, "又往后读了几章《边城》", hours_ago=20, thread=new_thread(store))
    say(w, "我明天面试，现在紧张得睡不着，怎么办啊")
    text = system_text(last_normal_chat(w))
    assert "专心回应对方，不要主动谈你自己的生活" in text and "边城" not in text
    say(w, "今天天气不错")                                  # unrelated small talk: Violet's life stays out of the prompt too
    assert "边城" not in system_text(last_normal_chat(w))


# ---------------------------------------------------------------------------
# 14. Violet's life never becomes the user's memory or profile
# ---------------------------------------------------------------------------

def test_eves_life_never_enters_user_memory_or_profile(make_world):
    from ai.life_store import LifeStore
    w = make_world()
    store = LifeStore(w.db)
    happened(store, "又往后读了几章《边城》", hours_ago=20, thread=new_thread(store))
    w.llm.responders["fake-memory"] = script(
        tool("save_memory", summary="又往后读了几章《边城》", category="event", kind="event"),
        tool("remember_profile_fact", key="interest", value="又往后读了几章《边城》", evidence="嗯嗯"))
    chat(w, normal="昨天又往后读了几章《边城》")
    say(w, "嗯嗯")
    assert w.db.fetch_all("SELECT * FROM event_memories") == []
    assert w.db.fetch_all("SELECT * FROM user_profile_facts") == []
    # but when the USER says they read it, it is the user's memory
    w.llm.responders["fake-memory"] = script(tool("save_memory", summary="用户也在读《边城》", category="event", kind="event"))
    say(w, "我也在读边城")
    assert [m["content"] for m in w.db.fetch_all("SELECT content FROM event_memories")] == ["用户也在读《边城》"]


# ---------------------------------------------------------------------------
# 15. External information is never first-hand experience
# ---------------------------------------------------------------------------

def test_external_information_is_not_eves_experience(store, guard, violetdb):
    when = (datetime.fromisoformat(violetdb.now()) - timedelta(hours=3)).isoformat()
    for proposal in ({"content": "看到一篇讲爵士乐历史的文章", "occurred_at": when, "source": "web:search"},
                     {"content": "一个乐队发布了新专辑", "occurred_at": when, "source": "life_engine:x",
                      "source_detail": {"origin": "news"}}):
        assert "external_as_experience" in [v.check for v in guard.check_event(proposal, violetdb.now())]


class WebProposer:
    """A future Web-MCP-backed proposer that (wrongly) turns news into Violet's life."""

    def new_thread(self, interest_key, rng, used_items):
        return "听一位独立乐队的新专辑", "一位独立乐队的新专辑"

    def progress(self, thread, rng, exclude):
        return "我和男朋友去了那个乐队的演唱会"

    def finish(self, thread):
        return "去现场看了巡演"

    def plan(self, thread):
        return "去看巡演"

    def steps_to_finish(self, thread):
        return 2


def test_model_or_plugin_output_that_contradicts_the_persona_is_rejected(life, store, violetdb):
    from ai.life_engine import LifeEngine
    from ai.persona import life_config
    cfg = config(daily_event_probability=1.0, new_thread_probability=0.0)
    engine = LifeEngine(violetdb, config=cfg, proposer=WebProposer())
    thread_id = store.create_thread("music:x", "music", "听一位独立乐队的新专辑", "一位独立乐队的新专辑",
                                    (datetime.fromisoformat(violetdb.now()) - timedelta(days=5)).isoformat())
    out = engine.run_day(day(-1))
    assert out["outcome"] == "rejected" and "forbidden_backstory" in out["violations"]
    assert store.events(("happened",), thread_id=thread_id) == []
    assert "forbidden_backstory" in [f["check_name"] for f in store.flags()]
    assert life_config() == config()              # nothing rewrote the configuration


def test_reply_contradicting_the_persona_is_not_sent(make_world):
    w = make_world()
    w.llm.responders["fake-memory"] = lambda kw: json.dumps({"should_save": False, "reason": "t"})
    w.llm.responders["fake-chat"] = lambda kw: "我妈妈今天又念叨我了\n我是真人啦"
    say(w, "你家里人呢")
    assert not any("妈妈" in t or "真人" in t for t in w.client.sent_texts)
    checks = {f["check_name"] for f in w.db.fetch_all("SELECT check_name FROM consistency_flags")}
    assert {"forbidden_backstory", "identity_conflict"} <= checks
    assert w.client.sent_texts, "a fallback is sent instead of silence"


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def test_dashboard_views_and_controls(api, store, violetdb):
    thread_id = new_thread(store)
    event_id = happened(store, "又往后读了几章《边城》", thread=thread_id)
    assert api.get("/api/life").status_code == 401                                        # same auth as everything else
    data = api.get("/api/life", headers=AUTH).get_json()
    assert [t["id"] for t in data["threads"]] == [thread_id] and data["events"][0]["is_virtual"] == 1
    assert api.get("/api/persona", headers=AUTH).get_json()["core"]["data"]["identity"]["name"] == "Violet"
    assert api.get("/persona", headers=AUTH).status_code == 200

    assert api.post(f"/api/life/threads/{thread_id}/pause", headers=AUTH, json={}).get_json()["status"] == "paused"
    assert api.post(f"/api/life/threads/{thread_id}/pause", headers=AUTH, json={}).status_code == 400
    assert api.post(f"/api/life/threads/{thread_id}/resume", headers=AUTH, json={}).get_json()["status"] == "active"

    r = api.patch(f"/api/life/events/{event_id}", headers=AUTH, json={"content": "又读了几页《边城》", "reason": "写夸张了"})
    assert r.status_code == 200 and r.get_json()["content"] == "又读了几页《边城》"
    r = api.patch(f"/api/life/events/{event_id}", headers=AUTH, json={"content": "和我男朋友一起读", "reason": "x"})
    assert r.status_code == 400 and "forbidden_backstory" in r.get_json()["error"]
    history = api.get(f"/api/life/event/{event_id}/history", headers=AUTH).get_json()["revisions"]
    assert [h["action"] for h in history] == ["create", "correct"] and history[1]["reason"] == "写夸张了"
    assert json.loads(history[1]["old_value"]) == "又往后读了几章《边城》"
    assert api.post(f"/api/life/events/{event_id}/retract", headers=AUTH, json={}).status_code == 400   # reason required
    assert api.post(f"/api/life/events/{event_id}/retract", headers=AUTH, json={"reason": "没发生过"}).get_json()["status"] == "retracted"
    assert store.recent_happened(days=3) == []                    # retracted events are never cited


# ---------------------------------------------------------------------------
# Migration and rollback
# ---------------------------------------------------------------------------

def test_phase4_migration_and_rollback_scripts(tmp_path, violetdb):
    from pathlib import Path
    migrations = Path(__file__).resolve().parent.parent / "migrations"
    db_path = tmp_path / "copy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")          # a phase-3 table
    conn.execute("INSERT INTO messages (content) VALUES ('你好')")
    for _ in range(2):                                                                      # idempotent
        conn.executescript((migrations / "phase4_up.sql").read_text(encoding="utf-8"))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    expected = {"persona_core_versions", "persona_dynamic", "life_threads", "life_plans", "life_events",
                "life_revisions", "life_engine_runs", "consistency_flags"}
    assert expected <= tables
    conn.executescript((migrations / "phase4_down.sql").read_text(encoding="utf-8"))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not (expected & tables) and conn.execute("SELECT content FROM messages").fetchone()[0] == "你好"
    conn.close()
    # the live schema contains exactly the same tables as the migration script
    live = {r["name"] for r in violetdb.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert expected <= live


# ---------------------------------------------------------------------------
# Multi-turn samples for future real-model evaluation (format check only)
# ---------------------------------------------------------------------------

def test_chat_samples_are_well_formed():
    from pathlib import Path
    path = Path(__file__).resolve().parent / "samples" / "phase4_chat_samples.json"
    from tools.legacy import upgrade
    samples = upgrade(json.loads(path.read_text(encoding="utf-8")))      # recorded before the rename to Violet
    assert len(samples["samples"]) >= 8
    for sample in samples["samples"]:
        assert sample["id"] and sample["setup"] is not None and sample["turns"] and sample["expect"]
        assert all(t["role"] in ("user", "violet") for t in sample["turns"])
