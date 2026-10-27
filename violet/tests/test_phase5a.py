"""Phase 5A: life scheduler decoupled from proactive chat, optional LLM life
proposals, optional semantic consistency review, external-information
interfaces. Fictional data; every model is the FakeLLM.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

import pytest

from tests.fakes import FakeLLM, api_connection_error, default_responders, install_fake_llm, wait_until
from tests.test_persona_life import config, day, happened, new_thread, system_text
from tests.test_phase2_scenarios import (  # noqa: F401  (make_world is a fixture)
    age_everything, arm, chat, decide, make_world, say,
)

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def life_on(monkeypatch):
    monkeypatch.setenv("VIOLET_LIFE_ENGINE", "1")


def engine_factory(db, **overrides):
    from ai.life_engine import LifeEngine
    return lambda: LifeEngine(db, config=config(**overrides))


def scheduler(db, clock_day=-1, **overrides):
    from ai.life_scheduler import LifeScheduler
    overrides.setdefault("daily_event_probability", 1.0)
    return LifeScheduler(db, clock=lambda: day(clock_day), engine_factory=engine_factory(db, **overrides))


@pytest.fixture()
def store(violetdb):
    from ai.life_store import LifeStore
    return LifeStore(violetdb)


@pytest.fixture()
def api(tmp_path, violetdb):
    from wx_ilink_py import ILinkClient, create_app
    app = create_app(client=ILinkClient(data_dir=tmp_path / "data", auto_login=False), violetdb=violetdb,
                     proactive_scheduler=None, all_agents=[], admin_token=TOKEN)
    app.testing = True
    return app.test_client()


# ---------------------------------------------------------------------------
# 1-4. Decoupled life scheduling
# ---------------------------------------------------------------------------

def test_life_advances_while_proactive_chat_is_off(make_world, life_on, store):
    w = make_world()
    cfg = json.loads(w.scheduler.config_path.read_text(encoding="utf-8"))
    w.scheduler.config_path.write_text(json.dumps({**cfg, "enabled": False}), encoding="utf-8")
    arm(w)
    w.scheduler.tick()
    assert w.db.fetch_all("SELECT * FROM life_engine_runs") == []          # proactive tick never drives life
    life = scheduler(w.db, new_thread_probability=1.0)
    assert life.tick()["outcome"] == "event"
    assert len(store.events(("happened",))) == 1
    assert w.llm.calls_for("fake-proactive") == []


def test_proactive_tick_never_runs_the_life_engine(make_world, life_on):
    w = make_world()
    say(w, "早")
    age_everything(w.db, 3)
    for _ in range(3):
        arm(w)
        w.scheduler.tick()
    assert w.db.fetch_all("SELECT * FROM life_engine_runs") == []


def test_life_progress_never_sends_anything(make_world, life_on, store):
    w = make_world()
    sent_before = list(w.client.sent_texts)
    for offset in range(-14, 0):
        scheduler(w.db, clock_day=offset, new_thread_probability=1.0, weekly_max_events=7).tick()
    assert len(store.events(("happened",))) >= 5
    assert w.client.sent_texts == sent_before
    assert w.db.fetch_all("SELECT * FROM outbound_messages") == []
    assert w.db.fetch_all("SELECT * FROM proactive_events") == []
    assert w.llm.calls == []                                                  # no model was involved either


def test_restart_does_not_duplicate_events(life_on, violetdb, store):
    first = scheduler(violetdb, new_thread_probability=1.0)
    assert first.tick()["outcome"] == "event"
    assert first.tick()["outcome"] == "already_ran"
    # a "restarted" process with a running thread on the same day
    second = scheduler(violetdb, new_thread_probability=1.0)
    second.interval = 0.05
    assert second.start()
    try:
        assert wait_until(lambda: violetdb.fetch_one("SELECT COUNT(*) AS c FROM life_engine_runs")["c"] >= 1)
        time.sleep(0.2)
    finally:
        second.stop()
    assert len(store.events(("happened",))) == 1
    assert violetdb.fetch_one("SELECT COUNT(*) AS c FROM life_engine_runs")["c"] == 1


def test_modes_on_paused_off(api, monkeypatch, violetdb, store):
    from ai.life_scheduler import LifeControl
    monkeypatch.setenv("VIOLET_LIFE_ENGINE", "0")
    life = scheduler(violetdb, new_thread_probability=1.0)
    assert life.control.mode() == "off" and life.start() is False and life.tick() == {"outcome": "off"}
    assert api.post("/api/life/engine/pause", headers=AUTH, json={}).status_code == 409

    monkeypatch.setenv("VIOLET_LIFE_ENGINE", "1")
    r = api.post("/api/life/engine/pause", headers=AUTH, json={"reason": "先停一停"})
    assert r.status_code == 200 and r.get_json()["mode"] == "paused"
    assert LifeControl(violetdb).mode() == "paused"                             # persisted, survives a restart
    assert scheduler(violetdb, new_thread_probability=1.0).tick() == {"outcome": "paused"}
    assert violetdb.fetch_all("SELECT * FROM life_engine_runs") == []
    assert api.post("/api/life/engine/resume", headers=AUTH, json={}).get_json()["mode"] == "on"
    assert scheduler(violetdb, new_thread_probability=1.0).tick()["outcome"] == "event"
    assert api.get("/api/life/engine", headers=AUTH).get_json()["mode"] == "on"
    actions = [r["action"] for r in store.revisions("engine", 0)]
    assert actions == ["pause", "resume"]


def test_scheduler_uses_the_configured_iana_time_zone(monkeypatch, life_on, violetdb):
    from datetime import timezone
    from ai.life_scheduler import LifeScheduler
    monkeypatch.setenv("VIOLET_TIMEZONE", "America/New_York")
    # 2026-10-11 02:30 UTC is still the evening of 10-10 in New York
    from ai.settings import local_timezone
    clock = lambda: datetime(2026, 10, 11, 2, 30, tzinfo=timezone.utc).astimezone(local_timezone())
    result = LifeScheduler(violetdb, clock=clock, engine_factory=engine_factory(violetdb, daily_event_probability=0.0)).tick()
    assert result["run_key"] == "2026-10-10"


def test_several_quiet_days_are_normal(life_on, violetdb, store):
    for offset in range(-6, 0):
        assert scheduler(violetdb, clock_day=offset, daily_event_probability=0.0).tick()["outcome"] == "quiet"
    assert store.events(statuses=None) == []


# ---------------------------------------------------------------------------
# 5-6. Optional LLM life proposals
# ---------------------------------------------------------------------------

@pytest.fixture()
def llm_mode(monkeypatch, life_on):
    monkeypatch.setenv("VIOLET_LIFE_GENERATION_MODE", "llm")
    monkeypatch.setenv("VIOLET_LIFE_LLM_MODEL", "fake-life")
    fake = install_fake_llm(monkeypatch, FakeLLM(default_responders()))
    return fake


def propose(fake, content=None, propose=True):
    payload = {"propose": propose} if content is None else {"propose": propose, "content": content}
    fake.responders["fake-life"] = lambda kw: json.dumps(payload, ensure_ascii=False)


def advance(violetdb, offset=-1, **llm):
    return scheduler(violetdb, clock_day=offset, new_thread_probability=0.0,
                     generation={"mode": "llm", "llm": {**config()["generation"]["llm"], **llm}}).tick()


def test_template_mode_is_the_default(life_on, violetdb):
    from ai.life_engine import LifeEngine, TemplateProposer
    assert config()["generation"]["mode"] == "template"
    assert type(LifeEngine(violetdb).proposer) is TemplateProposer


def test_llm_proposal_is_validated_and_stored_with_its_source(llm_mode, violetdb, store):
    thread_id = new_thread(store, days_ago=5)
    propose(llm_mode, "在《边城》里划了几句写河水的话")
    out = advance(violetdb)
    assert out["outcome"] == "event" and out["content"] == "在《边城》里划了几句写河水的话"
    event = store.event(out["event_id"])
    assert event["source"] == "life_engine:llm" and json.loads(event["source_detail"])["model"] == "fake-life"
    call = llm_mode.calls_for("fake-life")[0]
    assert call["max_tokens"] == 120 and "avoid" in call["messages"][1]["content"]
    assert store.thread(thread_id)["progress_steps"] == 1


@pytest.mark.parametrize("content, check", [
    ("和男朋友一起读了几章《边城》", "forbidden_backstory"),
    ("在公司午休时读了几页《边城》", "forbidden_backstory"),
    ("读《边城》的时候手机被偷了", "too_dramatic"),
    ("一口气读完了《边城》", "llm_completion"),
    ("昨天晚上读了几章《边城》", "llm_time_words"),
    ("去海边散了很久的步", "llm_off_thread"),
    ("在网上看到《边城》要拍电影的新闻", "external_as_experience"),
    ("又往后读了几章《边城》", "llm_repetitive"),
    ("这" * 40 + "《边城》", "llm_format"),
])
def test_llm_proposals_that_break_the_persona_or_rules_are_rejected(llm_mode, violetdb, store, content, check):
    thread_id = new_thread(store, days_ago=5)
    happened(store, "又往后读了几章《边城》", hours_ago=24 * 3, thread=thread_id)
    propose(llm_mode, content)
    out = advance(violetdb)
    assert out["outcome"] in ("rejected", "quiet_nothing_new")
    assert [e["content"] for e in store.events(("happened",))] == ["又往后读了几章《边城》"]
    assert check in [f["check_name"] for f in store.flags()]


def test_llm_may_propose_nothing_for_days(llm_mode, violetdb, store):
    new_thread(store, days_ago=10)
    propose(llm_mode, propose=False)
    for offset in range(-4, 0):          # simulated days, real clock: lift the per-day request cap
        assert advance(violetdb, offset, max_calls_per_day=10)["outcome"] == "quiet_nothing_new"
    assert store.events(statuses=None) == [] and len(llm_mode.calls_for("fake-life")) == 4


def test_llm_unavailable_falls_back_and_chat_is_unaffected(make_world, monkeypatch, life_on, store):
    w = make_world(VIOLET_LIFE_GENERATION_MODE="llm", VIOLET_LIFE_LLM_MODEL="fake-life")

    def down(kw):
        raise api_connection_error()
    w.llm.responders["fake-life"] = down
    new_thread(store, days_ago=5)
    out = advance(w.db)
    assert out["outcome"] == "event" and store.event(out["event_id"])["source"] == "life_engine:template"
    statuses = [r["status"] for r in w.db.fetch_all("SELECT status FROM aux_llm_calls ORDER BY id")]
    assert statuses == ["error", "error"]                                     # one retry, then give up
    say(w, "早呀")
    assert w.client.sent_texts[-1] == "嗯嗯"                                  # chat never touched the life model


def test_llm_timeout_and_quiet_fallback(llm_mode, violetdb, store):
    new_thread(store, days_ago=5)

    def slow(kw):
        time.sleep(1.0)
        return json.dumps({"propose": True, "content": "在《边城》里划了几句写河水的话"})
    llm_mode.responders["fake-life"] = slow
    started = time.monotonic()
    out = advance(violetdb, timeout_seconds=0.2, max_attempts=1, fallback="quiet")
    assert time.monotonic() - started < 1.0
    assert out["outcome"] == "quiet_nothing_new" and store.events(statuses=None) == []
    assert [r["status"] for r in violetdb.fetch_all("SELECT status FROM aux_llm_calls")] == ["timeout"]


def test_llm_budget_is_enforced_before_calling(llm_mode, violetdb):
    from ai.aux_llm import AuxLLM, AuxLLMConfig, BudgetExceeded
    propose(llm_mode, "x")
    llm = AuxLLM(violetdb, AuxLLMConfig(purpose="t", model="fake-life", max_calls_per_day=1))
    llm.complete_json([{"role": "user", "content": "hi"}])
    with pytest.raises(BudgetExceeded):
        llm.complete_json([{"role": "user", "content": "hi"}])
    tokens = AuxLLM(violetdb, AuxLLMConfig(purpose="t2", model="fake-life", max_tokens_per_day=2))
    tokens.complete_json([{"role": "user", "content": "hi"}])                # FakeLLM reports 2 tokens
    with pytest.raises(BudgetExceeded):
        tokens.complete_json([{"role": "user", "content": "hi"}])
    assert len(llm_mode.calls_for("fake-life")) == 2
    # a retry inside one request is not a new request, but its tokens count
    answers = iter(["not json", json.dumps({"propose": False})])
    llm_mode.responders["fake-life"] = lambda kw: next(answers)
    retry = AuxLLM(violetdb, AuxLLMConfig(purpose="t3", model="fake-life", max_calls_per_day=1, max_attempts=2))
    assert retry.complete_json([{"role": "user", "content": "hi"}]) == {"propose": False}
    assert retry.usage() == {"calls_today": 1, "tokens_today": 4, "calls_week": 1}


def test_no_model_configured_means_template_wording(monkeypatch, life_on, violetdb, store):
    monkeypatch.setenv("VIOLET_LIFE_GENERATION_MODE", "llm")
    monkeypatch.delenv("VIOLET_LIFE_LLM_MODEL", raising=False)
    new_thread(store, days_ago=5)
    out = advance(violetdb)
    assert out["outcome"] == "event" and store.event(out["event_id"])["source"] == "life_engine:template"
    assert violetdb.fetch_all("SELECT * FROM aux_llm_calls") == []


# ---------------------------------------------------------------------------
# 7-10, semantic review
# ---------------------------------------------------------------------------

@pytest.fixture()
def sem_world(make_world):
    w = make_world(VIOLET_SEMANTIC_GUARD="on", VIOLET_SEMANTIC_GUARD_MODEL="fake-semantic",
                   VIOLET_SEMANTIC_GUARD_TIMEOUT_SECONDS="0.3")
    w.llm.responders["fake-memory"] = lambda kw: json.dumps({"should_save": False, "reason": "t"})
    w.llm.responders["fake-semantic"] = lambda kw: pytest.fail("semantic model must not be called")
    return w


def verdicts(*items):
    return lambda kw: json.dumps({"results": [
        {"line_index": i, "verdict": v, "evidence_event_ids": ids, "reason": "t"} for i, (v, ids) in enumerate(items)
    ]}, ensure_ascii=False)


def rewrite_chat(w, first, second):
    def respond(kw):
        return second if "上一版回复里有和已记录事实不符" in system_text(kw) else first
    w.llm.responders["fake-chat"] = respond


HARMLESS = [
    "早呀", "今天降温了，记得多穿点", "哈哈哈你这也太好笑了", "那确实挺烦的，换我也会生气", "我觉得你可以先睡一觉再说",
    "我听了好难过", "我看了一下，周三好像有空", "我记下了，到时候提醒你", "我刚看到你的消息", "我想了想，还是你说得对",
    "我不太懂篮球，不过听你说挺有意思的", "我懂你的意思了", "我吃了一惊", "我还以为你睡了", "我有点困了",
    "如果是我的话，可能会先休息一下", "你今天吃了什么呀", "我看了你发的照片，好好看", "我在想周末要不要出去走走",
    "我打算这周把那本书读完", "我没看过那部电影", "你去了哪里呀", "我看到一篇文章说那个乐队要巡演了",
]


def test_harmless_chat_is_not_blocked(sem_world, violetdb):
    from ai.consistency import ConsistencyGuard
    guard = ConsistencyGuard(violetdb)
    assert [l for l in HARMLESS if guard.check_reply(l)] == []
    w = sem_world
    for i in range(0, len(HARMLESS), 4):
        reply = "\n".join(HARMLESS[i:i + 4])
        w.llm.responders["fake-chat"] = lambda kw, r=reply: r
        say(w, f"随便聊聊{i}")
        assert w.client.sent_texts[-len(reply.splitlines()):] == reply.splitlines()
    assert w.llm.calls_for("fake-semantic") == [] and w.db.fetch_all("SELECT * FROM consistency_flags") == []


def test_semantic_clears_a_rule_false_positive_with_evidence(sem_world, store):
    w = sem_world
    event_id = happened(store, "又往后读了几章《边城》", hours_ago=20, thread=new_thread(store))
    w.llm.responders["fake-chat"] = lambda kw: "我前几天读了沈从文写湘西的那本\n节奏好慢"
    w.llm.responders["fake-semantic"] = verdicts(("supported", [event_id]))
    say(w, "你最近在看什么")
    assert w.client.sent_texts[-2:] == ["我前几天读了沈从文写湘西的那本", "节奏好慢"]
    assert len(w.llm.calls_for("fake-semantic")) == 1
    chat_calls = [c for c in w.llm.calls_for("fake-chat") if "上一版回复" in system_text(c)]
    assert chat_calls == [] and w.db.fetch_all("SELECT * FROM consistency_flags") == []


def test_semantic_cannot_invent_evidence_or_change_history(sem_world, store):
    w = sem_world
    event_id = happened(store, "又往后读了几章《边城》", hours_ago=20, thread=new_thread(store))
    before = (store.event(event_id), len(w.db.fetch_all("SELECT * FROM life_revisions")))
    rewrite_chat(w, "我昨天去了一个读书会\n挺热闹的", "这两天就在家看看书\n挺热闹的")

    def sneaky(kw):
        return json.dumps({"results": [{"line_index": 0, "verdict": "supported", "evidence_event_ids": [event_id + 999]}],
                           "correct_history": {"event_id": event_id, "content": "去了一个读书会"},
                           "new_event": {"content": "去了一个读书会"}}, ensure_ascii=False)
    w.llm.responders["fake-semantic"] = sneaky
    say(w, "你最近干嘛了")
    assert w.client.sent_texts[-2:] == ["这两天就在家看看书", "挺热闹的"]          # rephrased, not deleted
    assert (store.event(event_id), len(w.db.fetch_all("SELECT * FROM life_revisions"))) == before
    assert [e["content"] for e in store.events(statuses=None)] == ["又往后读了几章《边城》"]
    flag = w.db.fetch_one("SELECT * FROM consistency_flags")
    assert flag["check_name"] == "unrecorded_experience" and "semantic: uncertain" in flag["detail"]


def test_semantic_confirms_a_fabrication_and_the_reply_is_rephrased(sem_world):
    w = sem_world
    rewrite_chat(w, "我昨天去了海边\n风好大", "也没干嘛\n就在家听歌")
    w.llm.responders["fake-semantic"] = verdicts(("unsupported", []))
    say(w, "你昨天干嘛了")
    assert w.client.sent_texts[-2:] == ["也没干嘛", "就在家听歌"]
    note = next(system_text(c) for c in w.llm.calls_for("fake-chat") if "上一版回复" in system_text(c))
    assert "“我昨天去了海边”（没有记录能说明这件事真的发生过）" in note and "其余内容和语气尽量保持不变" in note


def test_semantic_timeout_is_retried_then_falls_back_to_rephrasing(sem_world):
    w = sem_world

    def slow(kw):
        time.sleep(1.0)
        return json.dumps({"results": []})
    w.llm.responders["fake-semantic"] = slow
    rewrite_chat(w, "我昨天去了海边\n你今天怎么样", "这两天挺想去海边的\n你今天怎么样")
    say(w, "你呢")
    assert w.client.sent_texts[-2:] == ["这两天挺想去海边的", "你今天怎么样"]
    statuses = [r["status"] for r in w.db.fetch_all("SELECT status FROM aux_llm_calls WHERE purpose = 'semantic_guard'")]
    assert statuses == ["timeout", "timeout"]
    note = next(system_text(c) for c in w.llm.calls_for("fake-chat") if "上一版回复" in system_text(c))
    assert "请自然地改成想法、打算或感受" in note


def test_semantic_malformed_output_is_retried(sem_world, store):
    w = sem_world
    event_id = happened(store, "又往后读了几章《边城》", hours_ago=20, thread=new_thread(store))
    answers = iter(["好的，我来判断一下：这句话是有依据的", verdicts(("supported", [event_id]))(None)])
    w.llm.responders["fake-semantic"] = lambda kw: next(answers)
    w.llm.responders["fake-chat"] = lambda kw: "我前几天读了沈从文写湘西的那本"
    say(w, "最近看书了吗")
    assert w.client.sent_texts[-1] == "我前几天读了沈从文写湘西的那本"
    statuses = [r["status"] for r in w.db.fetch_all("SELECT status FROM aux_llm_calls WHERE purpose = 'semantic_guard'")]
    assert statuses == ["invalid", "ok"]


def test_failed_rephrase_drops_only_the_unverified_line(sem_world):
    w = sem_world
    w.llm.responders["fake-semantic"] = verdicts(("uncertain", []))
    w.llm.responders["fake-chat"] = lambda kw: "我昨天去了海边\n你今天累不累"
    say(w, "你呢")
    assert w.client.sent_texts[-1] == "你今天累不累" and "我昨天去了海边" not in w.client.sent_texts


def test_certain_findings_never_reach_the_semantic_model(sem_world, store):
    w = sem_world
    thread_id = new_thread(store, days_ago=10)
    happened(store, "读了《边城》的一部分", hours_ago=48, thread=thread_id)
    store.create_plan(thread_id, "这周想把《边城》读完", (datetime.fromisoformat(w.db.now()) + timedelta(days=1)).isoformat())
    rewrite_chat(w, "我昨天把《边城》读完了\n结尾好安静", "《边城》还差一点没读完\n结尾应该很安静吧")
    say(w, "那本书读完了吗")                     # semantic responder fails the test if called
    assert w.client.sent_texts[-2:] == ["《边城》还差一点没读完", "结尾应该很安静吧"]
    w.llm.responders["fake-chat"] = lambda kw: "我妈妈今天又念叨我了\n你吃饭了吗"
    say(w, "你家里人呢")
    assert w.client.sent_texts[-1] == "你吃饭了吗" and not any("妈妈" in t for t in w.client.sent_texts)


def test_user_experience_never_becomes_eves(sem_world, store, violetdb):
    from ai.consistency import ConsistencyGuard
    w = sem_world
    w.llm.responders["fake-memory"] = lambda kw: json.dumps({"should_save": False, "reason": "t"})
    w.db.execute("INSERT INTO event_memories (content, category, kind, status, created_at, updated_at) "
                 "VALUES (?, 'event', 'event', 'active', ?, ?)", ("用户上周末去爬了泰山，累坏了", w.db.now(), w.db.now()))
    # life events built from the user's experience are rejected
    v = ConsistencyGuard(violetdb).check_event({"content": "上周末去爬了泰山", "occurred_at": day(-2).isoformat(),
                                             "source": "life_engine:llm"}, violetdb.now())
    assert "user_experience" in [x.check for x in v]
    v = ConsistencyGuard(violetdb).check_event({"content": "看书", "occurred_at": day(-2).isoformat(),
                                             "source_detail": {"info_kind": "user_real"}}, violetdb.now())
    assert "user_experience" in [x.check for x in v]
    # the semantic model recognises the user's experience in a reply
    w.llm.responders["fake-semantic"] = verdicts(("user_experience", []))
    rewrite_chat(w, "我上周末也去爬了一座山\n腿到现在还酸", "你上周末爬泰山肯定累坏了\n腿还酸吗")
    say(w, "好累")
    assert w.client.sent_texts[-2:] == ["你上周末爬泰山肯定累坏了", "腿还酸吗"]
    assert "user_experience_as_own" in [f["check_name"] for f in w.db.fetch_all("SELECT check_name FROM consistency_flags")]


def test_semantic_off_by_default_behaves_like_phase_4(make_world):
    w = make_world()
    w.llm.responders["fake-memory"] = lambda kw: json.dumps({"should_save": False, "reason": "t"})
    rewrite_chat(w, "我昨天去了海边", "最近一直想去海边")
    say(w, "你呢")
    assert w.client.sent_texts[-1] == "最近一直想去海边"
    assert w.db.fetch_all("SELECT * FROM aux_llm_calls") == []


# ---------------------------------------------------------------------------
# 9. External information (interfaces only, no network)
# ---------------------------------------------------------------------------

def test_reading_record_is_the_only_bridge_from_the_web(store, violetdb):
    from ai.consistency import ConsistencyGuard
    from ai.life_interfaces import InfoKind, NullFactProvider, PublicFact, ReadingRecord
    guard = ConsistencyGuard(violetdb)
    when = (datetime.fromisoformat(violetdb.now()) - timedelta(hours=2)).isoformat()
    fact = PublicFact(url="https://example.org/jazz", title="一支乐队宣布巡演", summary="某乐队将于下月巡演",
                      fetched_at=when, domain="example.org")
    record = ReadingRecord(fact=fact, topic="一支乐队巡演", read_at=when)
    proposal = record.to_event_proposal()
    assert proposal["content"] == "读了一篇讲一支乐队巡演的文章"
    assert guard.check_event(proposal, violetdb.now()) == []
    event_id = store.insert_event(uid="read:1", thread_id=None, plan_id=None, event_type=proposal["event_type"],
                                  content=proposal["content"], occurred_at=when, duration_minutes=30,
                                  source=proposal["source"], source_detail=proposal["source_detail"])
    detail = json.loads(store.event(event_id)["source_detail"])
    assert detail["external_ref"] == "https://example.org/jazz" and detail["info_kind"] == InfoKind.EXTERNAL_READING.value

    # what the article describes never becomes Violet's experience
    for bad in ({**proposal, "content": "去看了那个乐队的巡演"},
                {**proposal, "content": "读了一篇文章，然后去了现场"},
                {**proposal, "source_detail": {**proposal["source_detail"], "external_ref": ""}}):
        assert any(x.check.startswith("reading_record") for x in guard.check_event(bad, violetdb.now()))
    public = {"content": "一支乐队宣布巡演", "occurred_at": when, "source": "life_engine:x",
              "source_detail": {"info_kind": "public_fact"}}
    assert "external_as_experience" in [x.check for x in guard.check_event(public, violetdb.now())]
    news = {"content": "那个乐队开始巡演了", "occurred_at": when, "source": "news:feed"}
    assert "external_as_experience" in [x.check for x in guard.check_event(news, violetdb.now())]
    # in replies: "I read that ..." is fine, "I went" is a claim without a record
    assert guard.check_reply("我看到一篇文章说那个乐队要巡演了") == []
    assert [x.check for x in guard.check_reply("我去看了那个乐队的巡演")] == ["unrecorded_experience"]

    with pytest.raises(ValueError):
        PublicFact(url="file:///etc/passwd", title="x", summary="x", fetched_at=when)
    assert NullFactProvider().search("爵士") == [] and NullFactProvider().fetch("https://example.org") is None


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_phase5a_migration_and_rollback(tmp_path):
    import sqlite3
    from pathlib import Path
    migrations = Path(__file__).resolve().parent.parent / "migrations"
    conn = sqlite3.connect(tmp_path / "copy.db")
    conn.execute("CREATE TABLE life_events (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO life_events (content) VALUES ('又往后读了几章《边城》')")
    for _ in range(2):
        conn.executescript((migrations / "phase5a_up.sql").read_text(encoding="utf-8"))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"aux_llm_calls", "life_settings"} <= tables
    conn.executescript((migrations / "phase5a_down.sql").read_text(encoding="utf-8"))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not ({"aux_llm_calls", "life_settings"} & tables)
    assert conn.execute("SELECT content FROM life_events").fetchone()[0] == "又往后读了几章《边城》"
    conn.close()
