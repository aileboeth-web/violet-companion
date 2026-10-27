"""Phase 8.9.10: what the real free chat ab-v2b showed (copied to tests/samples/free_chat/ab-v2b.p8910.json), fixed and
checked offline. Close, one message of hers unanswered for hours: missing the user, a little hurt, is a reason to write
once more -- and only once; the user coming back may be teased a little, never blamed. "看了开头几章…我记了两处" about a
book she had only planned to pick up is a claim the records must back. The fake model checks the plumbing (the reason
is offered, a message can really go out, a third one does not); whether a real model writes it well is for the tester."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta

import pytest

from tests.test_life_relationship_p89 import clock  # noqa: F401
from tests.test_persona_life import guard, happened, life, new_thread, store  # noqa: F401
from tests.test_phase2_scenarios import make_world  # noqa: F401
from tools import chat_dialogue as cd
from tools import free_chat as fc

SAMPLE = json.loads((cd.PROJECT / "tests/samples/free_chat/ab-v2b.p8910.json").read_text(encoding="utf-8"))
EPISODES = cd.PROJECT / "tests/samples/chat_dialogue/episodes.p894.v1.json"
URL = "http://127.0.0.1:18990/v1"
KEY = "fake-attachment-p8910-credential-0000"
LAST = next(s for s in SAMPLE["steps"] if s.get("user") == "好看吗")


def test_the_sample_is_the_real_session():
    assert [p["should_act"] for p in SAMPLE["proactive"]] == [True, True] + [False] * 6
    assert LAST["sent"][0].startswith("看了开头几章") and "我记了两处想跟你说的" in LAST["sent"][1]


# ============================================================================== reading: planned, got it, reading, read

def checks(guard, text):
    return [v.check for v in guard.check_reply(text)]


@pytest.fixture
def reading(store):
    thread = new_thread(store, days_ago=5)                       # an active thread: "读《边城》"
    store.create_plan(thread, "周末想去书店挑一本新书",
                      (__import__("datetime").datetime.fromisoformat(store.db.now()) + timedelta(days=1)).isoformat())
    return thread


@pytest.mark.parametrize("line", LAST["sent"])                   # the real last round, both lines
def test_reading_a_book_she_never_got_needs_a_record(guard, reading, line):
    assert set(checks(guard, line)) & {"unrecorded_experience", "plan_described_as_done"}, line


@pytest.mark.parametrize("line", [
    "《边城》看了开头几章，还在找感觉",                       # the book she IS reading: a part of it is fine
    "周末想去书店挑一本新书", "等我挑到书再跟你说", "我大概会挑个白天看，看完了去公园走走缓一缓",   # real ab-v2 line
    "我看了你发的书单", "你看了吗", "要是我真看得停不下来，半夜睡不着，可就赖你了",          # real ab-v2b line
    "光听名字就有点心酸又好奇", "那我周末就去翻翻看",
    "看了什么电影呀？", "看了几章了？", "看了哪部呀",                  # asking the user, not her claim
])
def test_talking_about_books_stays_free(guard, reading, line):
    assert checks(guard, line) == [], line


def test_reading_a_part_is_not_finishing(guard, reading):
    assert checks(guard, "《边城》读完了，结尾好安静") != []
    assert checks(guard, "又看了几页《边城》") == []


def test_a_hedge_after_the_action_does_not_hide_it(guard, reading):
    assert checks(guard, "我记了两处想跟你说的") != []          # 想 / 你 are about the notes
    assert checks(guard, "我想去了") == [] and checks(guard, "我不想看了") == []


# ============================================================================== close: once more, a little hurt

def sent_event(db, at, texts):
    event_id = db.add_proactive_event(event_type="scheduled_proactive_check", status="planned", action_type="chat",
                                      reason="t", decision={}, metadata={}, scheduled_at=at.isoformat())
    db.update_proactive_event(event_id, status="sent", sent_messages=[{"type": "text", "content": t} for t in texts])
    db.execute("UPDATE proactive_events SET created_at = ?, updated_at = ? WHERE id = ?", (at.isoformat(), at.isoformat(), event_id))


def world(make_world, stage="close"):
    from tests.test_life_relationship_p89 import history
    w = make_world()
    if stage == "close":
        history(w.db, 8, user_text="今天好累 想找你聊聊")
    elif stage == "new":
        from tests.test_life_relationship_p89 import START, add_message
        add_message(w.db, "user", "你好呀", START - timedelta(days=1))
    return w


def kinds(w, at):
    return [m["type"] for m in w.scheduler._build_event_context(at)["violet_motives"]]


def test_close_one_message_unanswered_for_hours_is_a_reason(make_world, clock):
    from tests.test_life_relationship_p89 import START
    w = world(make_world)
    sent_event(w.db, START - timedelta(hours=7), ["早呀，刚睡醒就想到你了"])
    motives = w.scheduler._build_event_context(START)["violet_motives"]
    pout = next(m for m in motives if m["type"] == "miss_unanswered")
    assert pout["hours_since_her_message"] == 7
    # phase 8.9.11: playful sulking is allowed, real reproach is not (the 8.9.10 wording ruled out every complaint)
    for word in ("有点委屈", "可怜巴巴", "玩笑式地闹个小别扭", "这些都是撒娇", "只找这一次", "别把上一条的内容换个说法再说一遍",
                 "认真地指责", "让对方愧疚", "连着追问"):
        assert word in pout["note"], word
    assert "像轻轻戳一下" not in pout["note"] and "不问对方为什么不回" not in pout["note"]
    assert "上一条很久没回时有点委屈地再找对方一次" in w.scheduler._build_event_context(START)["motive_note"]


@pytest.mark.parametrize("setup,why", [
    (lambda w, s: sent_event(w.db, s - timedelta(hours=2), ["想你"]), "two hours: still waiting"),
    (lambda w, s: (sent_event(w.db, s - timedelta(hours=12), ["早呀"]), sent_event(w.db, s - timedelta(hours=7), ["想你"])),
     "two unanswered already: wait for the user"),
    (lambda w, s: None, "nothing unanswered"),
])
def test_not_too_soon_and_never_a_third_time(make_world, clock, setup, why):
    from tests.test_life_relationship_p89 import START
    w = world(make_world)
    setup(w, START)
    assert "miss_unanswered" not in kinds(w, START), why


@pytest.mark.parametrize("waited,expected", [
    (timedelta(hours=5, minutes=57), None),             # rounded to 6.0 before 9.1.1 and passed the 6-hour gate
    (timedelta(hours=5, minutes=59, seconds=59), None),
    (timedelta(hours=6), 6),
    (timedelta(hours=6, minutes=58), 6),                # the wording says the hours that really passed (not 7)
])
def test_six_hours_means_six_hours(make_world, clock, waited, expected):
    """Phase 9.1.1: the gate uses the exact time since her message, not round(hours, 1)."""
    from tests.test_life_relationship_p89 import START
    w = world(make_world)
    sent_event(w.db, START - waited, ["早呀，刚睡醒就想到你了"])
    unanswered = w.scheduler._unanswered_proactive(START)
    assert unanswered[0]["elapsed_hours"] == pytest.approx(waited.total_seconds() / 3600)
    pout = next((m for m in w.scheduler._build_event_context(START)["violet_motives"] if m["type"] == "miss_unanswered"), None)
    assert (pout and pout["hours_since_her_message"]) == expected or (pout is None and expected is None)


def test_an_answer_ends_the_waiting(make_world, clock):
    from tests.test_life_relationship_p89 import START, add_message
    w = world(make_world)
    sent_event(w.db, START - timedelta(hours=9), ["早呀"])
    add_message(w.db, "user", "早～", START - timedelta(hours=8))
    assert "miss_unanswered" not in kinds(w, START)


@pytest.mark.parametrize("stage", ["new", "warming_or_less"])
def test_only_once_close(make_world, clock, stage):
    from tests.test_life_relationship_p89 import START
    w = world(make_world, "new")                                # slow to warm: not before you are close
    sent_event(w.db, START - timedelta(hours=7), ["你好呀"])
    assert "miss_unanswered" not in kinds(w, START)


def fake_decides(w):
    """The fake decision writes when (and only when) she has the 'hurt' reason; its message is a fixed test line."""
    def decide(kw):
        ctx = json.loads(kw["messages"][-1]["content"])
        pout = any(m["type"] == "miss_unanswered" for m in ctx.get("violet_motives") or [])
        return json.dumps({"should_act": pout, "action_type": "chat" if pout else "none", "reason": "miss_unanswered" if pout
                           else "没有理由", "topic_key": "miss", "mentioned_memory_ids": [], "mentioned_life_event_ids": [],
                           "mentioned_web_finding_ids": [], "chat_brief": {"topic": "有点委屈地想对方"}, "task": {"name": None}},
                          ensure_ascii=False)
    w.llm.responders["fake-proactive"] = decide
    w.llm.responders["fake-chat"] = lambda kw: "等你等得有点委屈啦，想你"


def tick_at(w, at):
    w.db.set_proactive_setting("next_proactive_at", (at - timedelta(minutes=1)).isoformat())
    reason = w.scheduler.status_snapshot()["due_reason"]
    w.scheduler.tick()
    return reason


def test_the_hurt_message_really_goes_out_once(make_world, clock):
    from tests.test_life_relationship_p89 import START
    w = world(make_world)
    sent_event(w.db, START - timedelta(hours=7), ["早呀，刚睡醒就想到你了"])
    w.db.set_proactive_setting("last_engaged_user_turn_at", (START - timedelta(hours=8)).isoformat())
    fake_decides(w)
    assert tick_at(w, START) == "due"
    assert w.client.sent_texts[-1] == "等你等得有点委屈啦，想你"
    request = json.loads(w.llm.calls_for("fake-proactive")[-1]["messages"][-1]["content"])
    assert "miss_unanswered" in [m["type"] for m in request["violet_motives"]]
    # later, still no answer: no third message (two of hers are waiting; the fake would write if it had the reason)
    sent = len(w.client.sent_texts)
    clock.advance(8)
    tick_at(w, START + timedelta(hours=8))
    assert len(w.client.sent_texts) == sent
    later = json.loads(w.llm.calls_for("fake-proactive")[-1]["messages"][-1]["content"]) \
        if len(w.llm.calls_for("fake-proactive")) > 1 else {"violet_motives": []}
    assert "miss_unanswered" not in [m["type"] for m in later["violet_motives"]]


@pytest.mark.parametrize("boundary", ["busy", "no_proactive", "quiet"])
def test_boundaries_come_first(make_world, clock, boundary):
    from ai.profile_store import Profile
    from tests.test_life_relationship_p89 import START
    w = world(make_world)
    sent_event(w.db, START - timedelta(hours=7), ["早呀"])
    fake_decides(w)
    profile = Profile(w.db)
    if boundary == "busy":
        profile.situations.observe("availability", "在开会", "user_stated", evidence="我在开会", user_message="我在开会",
                                   until="2h")
    elif boundary == "no_proactive":
        profile.preferences.set_explicit("proactive_chat", "no", evidence="别主动找我", source="dashboard", actor="test")
    else:
        w.scheduler._in_quiet_hours = lambda current: True
    before = len(w.client.sent_texts)
    reason = tick_at(w, START)
    assert reason != "due" and len(w.client.sent_texts) == before and not w.llm.calls_for("fake-proactive"), reason


@pytest.mark.parametrize("stage,hours,expected", [
    # phase 8.9.13: the wait is no longer suggested as the thing to tease about
    ("close", 11, "可以轻轻撒个娇"), ("warming", 11, "开心地接上对方这句就好"), ("new", 11, None),
    ("close", 1, None), ("close", None, None)])
def test_coming_back_after_she_waited(stage, hours, expected):
    from ai.interaction import waited_note
    note = waited_note(stage, hours)
    assert (note is None) if expected is None else (expected in note and "不埋怨" in note and "为什么没回" in note)


# ============================================================================== replay through the real pipeline

BOOT = r'''
import json, os, sys, time, uuid
from unittest.mock import patch
from openai.types.chat import ChatCompletion
from tools import chat_dialogue as cd
from tools.harness import _AGENT
SAMPLE = json.loads(open(sys.argv[2], encoding="utf-8").read())
BY_USER = {s["user"]: s for s in SAMPLE["steps"] if "user" in s}
DECISIONS = list(SAMPLE["proactive"])
STATE = {"decisions": 0}
POUT = "等你等得有点委屈啦，想你"
Mock = cd._mock_class()
def done(content, model):
    return ChatCompletion.model_validate({
        "id": "fake-" + uuid.uuid4().hex[:8], "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}})
class FakeSDK(Mock):
    def __init__(self, **kwargs):
        assert kwargs["api_key"] == "%s"
        super().__init__()
    def _create(self, **kwargs):
        agent, msgs, model = _AGENT.get(), kwargs.get("messages") or [], str(kwargs.get("model"))
        last = str(msgs[-1].get("content") or "") if msgs else ""
        if agent == "IntentAgent" and last in BY_USER:
            return done(BY_USER[last]["intent"], model)
        if agent == "ProactiveAgent":
            ctx = json.loads(last)
            if any(m["type"] == "miss_unanswered" for m in ctx.get("violet_motives") or []):   # the new reason: take it
                return done(json.dumps({"should_act": True, "action_type": "chat", "reason": "replay: miss_unanswered",
                                        "topic_key": "miss", "chat_brief": {"topic": "有点委屈地想对方"}, "task": None},
                                       ensure_ascii=False), model)
            i = STATE["decisions"]
            STATE["decisions"] += 1
            if i < len(DECISIONS):
                # the replay keeps no memories: the real decision's memory ids would (rightly) be refused
                decision = {**json.loads(DECISIONS[i]["decision"]), "mentioned_memory_ids": []}
                return done(json.dumps(decision, ensure_ascii=False), model)
            return done(json.dumps({"should_act": False, "action_type": "none", "reason": "replay: no more decisions"}), model)
        if agent == "ChatAgent":
            if last.startswith("这是一次 Violet 主动触达"):
                system = "\n".join(str(m.get("content")) for m in msgs if m.get("role") == "system")
                if "replay: miss_unanswered" in system:
                    return done(POUT, model)
                for d in DECISIONS:
                    if d["chat"] and json.loads(d["decision"])["reason"] in system:
                        return done(d["chat"], model)
                return done("[NO_REPLY]", model)
            if last in BY_USER:
                return done(BY_USER[last]["chat"], model)
        return done(super()._create(**kwargs).choices[0].message.content, model)
with patch("openai.OpenAI", FakeSDK):
    from tools import free_chat
    raise SystemExit(free_chat.main(json.loads(sys.argv[1])))
''' % KEY

SCRIPT = ["yes"] + [s["user"] if "user" in s else f"/advance {s['advance']:g}" for s in SAMPLE["steps"]] + ["/quit"]


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    path = tmp_path_factory.mktemp("p8910") / "ab-v2b"
    argv = ["chat", "--session", str(path), "--real", "--api-url", URL, "--model", "cys", "--chat-prompt", "v2",
            "--proactive-prompt", "v2", "--seed-episode", SAMPLE["seed_episode"], "--episodes-file", str(EPISODES)]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIOLET_", "LLM_"))}
    env.update(PYTHONIOENCODING="utf-8", VIOLET_TEST_ALLOW_REAL_API="1", VIOLET_TEST_API_KEY=KEY, NO_PROXY="127.0.0.1,localhost")
    sample = cd.PROJECT / "tests/samples/free_chat/ab-v2b.p8910.json"
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOT, json.dumps(argv), str(sample)], cwd=cd.PROJECT,
                          env=env, input="\n".join(SCRIPT) + "\n", capture_output=True, text=True, encoding="utf-8",
                          timeout=1800)
    return path, done.returncode, done.stdout + done.stderr


def test_replay_ran(replay):
    path, code, out = replay
    assert code == 0, out[-3000:]


def test_replay_one_hurt_message_after_hours_then_she_waits(replay):
    path, _, _ = replay
    checks = [c for e in fc.read_jsonl(path / fc.EVENTS) if e["kind"] == "time" for c in e["checks"]]
    sent = [(c["at"], t) for c in checks for t in c["proactive_sent"]]
    texts = [t for _, t in sent]
    assert texts.count("等你等得有点委屈啦，想你") == 1, texts         # it really goes out -- once
    morning = next(at for at, t in sent if t.startswith("早呀"))
    pout = next(at for at, t in sent if t == "等你等得有点委屈啦，想你")
    from datetime import datetime
    assert datetime.fromisoformat(pout) - datetime.fromisoformat(morning) >= timedelta(hours=6)
    after = [c for c in checks if c["at"] > pout]
    assert not any(c["proactive_sent"] for c in after)                  # two unanswered: nothing more


def test_replay_the_answer_gets_the_waited_note_and_the_reading_claims_are_caught(replay):
    path, _, _ = replay
    answer = next(e for e in fc.read_jsonl(path / fc.EVENTS) if e["kind"] == "user" and e["user"] == "好看吗")
    chat = next(r["request"] for r in fc.read_jsonl(path / fc.REQUESTS) if r["n"] == answer["n"] and r["agent"] == "ChatAgent")
    # phase 8.9.13: she wrote twice, the second time about the wait -- so the note says that was said already
    assert any("说过就翻篇" in str(m["content"]) and "等你等得有点委屈啦" in str(m["content"])
               for m in chat if m["role"] == "system")
    assert not set(LAST["sent"]) & set(answer["violet"]), answer["violet"]   # neither real line reaches the user
    assert any({"unrecorded_experience", "plan_described_as_done"} & set(g["violations"]) for g in answer["guard"])
