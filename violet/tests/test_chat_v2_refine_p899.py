"""Phase 8.9.9: what the real A/B (local/free_chat/ab-v1, ab-v2; ab-v2 copied to tests/samples/free_chat/ab-v2.p899.json)
showed, fixed and checked offline: a plan she already raised and nobody answered is not a new reason to write again;
"我出门啦，人已经站在书店门口了" / "我带回家了" are claims the records must back; the user answering one of her own messages
gets her real life next to it. The replay feeds the REAL outputs back through Violet's real pipeline (fake SDK) -- it shows
what the guard and the context now do, not how a real model would answer."""
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

SAMPLE = json.loads((cd.PROJECT / "tests/samples/free_chat/ab-v2.p899.json").read_text(encoding="utf-8"))
EPISODES = cd.PROJECT / "tests/samples/chat_dialogue/episodes.p894.v1.json"
URL = "http://127.0.0.1:18999/v1"
KEY = "fake-refine-p899-credential-0000"
PLAN = "周末想去书店挑一本新书"


def test_the_sample_is_the_real_session():
    sent = [p for p in SAMPLE["proactive"] if p["should_act"]]
    assert [p["should_act"] for p in SAMPLE["proactive"]] == [True, True, False, False, True]
    assert "人已经站在书店门口了" in sent[-1]["chat"]
    assert next(s for s in SAMPLE["steps"] if s.get("user", "").startswith("没事的，先把他带回家"))["sent"][0] == "听你的，我带回家了"


# ============================================================================== the guard: being there, having it

def checks(guard, text):
    return [(v.check, v.line) for v in guard.check_reply(text)]


@pytest.fixture
def bookstore_plan(store):
    thread = new_thread(store, days_ago=5)
    store.create_plan(thread, PLAN, (__import__("datetime").datetime.fromisoformat(store.db.now()) + timedelta(days=1)).isoformat())
    return thread


@pytest.mark.parametrize("line", ["我出门啦，人已经站在书店门口了", "听你的，我带回家了", "我已经在书店里了", "买到啦",
                                  "刚到公园了", "我到家了", "我站在书店门口了", "坐在咖啡馆里了"])
def test_being_there_or_having_it_needs_a_record(guard, bookstore_plan, line):
    found = checks(guard, line)
    assert found and {c for c, _ in found} <= {"plan_described_as_done", "unrecorded_experience"}, found


@pytest.mark.parametrize("line", [
    "等会儿八成又要在小说区磨蹭很久，还在纠结要不要拿《活着》",    # real 14:50 line 3: not a claim
    "今天晚点打算去书店逛逛", "等下就要出门去书店了", "等我到了书店给你拍封面", "待会儿出门啦",
    "你到家了吗", "你到家了，好好休息", "你已经在书店里了，帮我挑一本新书",   # where the user is, even next to her plan
    "那就先带回家吧", "我站在你这边", "周末想去书店挑一本新书", "现在只能凭想象馋一下",
    "还没去呢，得等周末才有机会站到书架前",                           # real round 5
    "小满，我周末想去书店挑本新书，可是站在书架前肯定又要纠结半天……",   # real 22:01 message: a supposition
    "我每次站在书店门口都想进去", "到了书店的话再跟你说",
])
def test_plans_suppositions_and_the_user_are_not_claims(guard, bookstore_plan, line):
    assert checks(guard, line) == []


def test_a_recorded_trip_backs_being_there(guard, store, bookstore_plan):
    happened(store, "下午去书店转了一圈，挑了一本《活着》", hours_ago=1, thread=bookstore_plan)
    assert checks(guard, "人已经站在书店门口了") == []
    assert checks(guard, "听你的，我带回家了") == []
    assert checks(guard, "我到公园了") != []                       # a recorded bookstore trip is not the park


# ============================================================================== the scheduler: raised, unanswered

def sent_event(db, at, texts, decision=None):
    event_id = db.add_proactive_event(event_type="scheduled_proactive_check", status="planned", action_type="chat",
                                      reason="t", decision=decision or {}, metadata={}, scheduled_at=at.isoformat())
    db.update_proactive_event(event_id, status="sent", sent_messages=[{"type": "text", "content": t} for t in texts])
    db.execute("UPDATE proactive_events SET created_at = ?, updated_at = ? WHERE id = ?", (at.isoformat(), at.isoformat(), event_id))


@pytest.fixture
def close_with_plan(make_world, clock):
    from ai.life_store import LifeStore
    from tests.test_life_relationship_p89 import START, add_message, history
    w = make_world()
    history(w.db, 8, user_text="今天好累 想找你聊聊")
    store = LifeStore(w.db)
    thread = store.create_thread("t:书", "reading", "读《小王子》", "小王子", (START - timedelta(days=5)).isoformat())
    store.create_plan(thread, PLAN, (START + timedelta(days=1)).isoformat())
    add_message(w.db, "user", "看完之后心里会有点空落落的吧，还是挺好的一本书", START - timedelta(hours=12))
    add_message(w.db, "assistant", "那周末去书店的时候我挑挑看", START - timedelta(hours=12))
    return w, START, store, thread


def motive_types(w, at):
    context = w.scheduler._build_event_context(at)
    return [m["type"] for m in context["violet_motives"]], context


def test_a_plan_she_raised_unanswered_is_not_a_new_reason(close_with_plan):
    w, start, _, _ = close_with_plan
    kinds, _ = motive_types(w, start)
    assert "continue" in kinds and "miss" in kinds
    # 08:58 (real): the text alone does not name the plan clearly -- the decision's topic does
    sent_event(w.db, start + timedelta(hours=1), ["早上好呀", "今天晚点打算去书店逛逛，还在犹豫要不要真的拿起《活着》"],
               {"reason": "接着周末去书店挑书的事往下说", "chat_brief": {"topic": "接着上次说的书店和新书往下聊"}})
    kinds, context = motive_types(w, start + timedelta(hours=6))
    assert "continue" not in kinds and "ask_opinion" not in kinds
    assert context["already_raised"] == [{"about": PLAN, "at": (start + timedelta(hours=1)).strftime("%m-%d %H:%M")}]
    assert "不要换个说法再拿它找对方" in context["already_raised_note"]


def test_missing_the_user_stays_a_reason(close_with_plan):
    # only the raised plan's own reason goes; "miss" (no plan) is never filtered (whether it is offered at all after an
    # unanswered message is the cold-streak rule of 8.9.3, unchanged)
    w, start, _, _ = close_with_plan
    plan_id = w.db.fetch_one("SELECT id FROM life_plans")["id"]
    w.scheduler._raised_unanswered = lambda plans: {plan_id: start - timedelta(hours=1)}
    kinds, context = motive_types(w, start)
    assert "miss" in kinds and "continue" not in kinds and context["already_raised"][0]["about"] == PLAN


def test_the_user_answering_or_something_new_makes_it_a_reason_again(close_with_plan):
    from tests.test_life_relationship_p89 import add_message
    w, start, store, thread = close_with_plan                     # the user last wrote 12 hours before `start` (= now)
    sent_event(w.db, start - timedelta(hours=8), ["周末想去书店挑一本新书，你说挑什么好"])
    assert "continue" not in motive_types(w, start)[0]
    add_message(w.db, "user", "看小说吧", start - timedelta(hours=7))                      # answered
    kinds, context = motive_types(w, start)
    assert "continue" in kinds and "already_raised" not in context
    sent_event(w.db, start - timedelta(hours=6), ["周末去书店挑新书的事，我想好了挑小说"])
    assert "already_raised" in motive_types(w, start)[1]
    happened(store, "去书店挑了一本新书", hours_ago=2, thread=thread)                      # new since: it happened
    assert "already_raised" not in motive_types(w, start)[1]


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
            i = STATE["decisions"]
            STATE["decisions"] += 1
            if i < len(DECISIONS):
                return done(DECISIONS[i]["decision"], model)
            return done(json.dumps({"should_act": False, "action_type": "none", "reason": "replay: no more decisions"}), model)
        if agent == "ChatAgent":
            if last.startswith("这是一次 Violet 主动触达"):
                # the recorded message of the decision in this request (a rewrite gets the same words again)
                system = "\n".join(str(m.get("content")) for m in msgs if m.get("role") == "system")
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
    path = tmp_path_factory.mktemp("p899") / "ab-v2"
    argv = ["chat", "--session", str(path), "--real", "--api-url", URL, "--model", "cys", "--chat-prompt", "v2",
            "--proactive-prompt", "v2", "--seed-episode", SAMPLE["seed_episode"], "--episodes-file", str(EPISODES)]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIOLET_", "LLM_"))}
    env.update(PYTHONIOENCODING="utf-8", VIOLET_TEST_ALLOW_REAL_API="1", VIOLET_TEST_API_KEY=KEY, NO_PROXY="127.0.0.1,localhost")
    sample = cd.PROJECT / "tests/samples/free_chat/ab-v2.p899.json"
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOT, json.dumps(argv), str(sample)], cwd=cd.PROJECT,
                          env=env, input="\n".join(SCRIPT) + "\n", capture_output=True, text=True, encoding="utf-8",
                          timeout=1800)
    return path, done.returncode, done.stdout + done.stderr


def requests(path, agent, n=None):
    return [r for r in fc.read_jsonl(path / fc.REQUESTS) if r["agent"] == agent and (n is None or r["n"] == n)]


def test_replay_ran(replay):
    path, code, out = replay
    assert code == 0, out[-3000:]


def test_replay_after_she_raised_the_bookstore_it_is_no_reason_until_the_user_answers(replay):
    path, _, _ = replay
    contexts = [json.loads(r["request"][-1]["content"]) for r in requests(path, "ProactiveAgent")]
    sends = [i for i, c in enumerate(contexts) if i < len(SAMPLE["proactive"]) and SAMPLE["proactive"][i]["should_act"]]
    morning = sends[1]                                       # the 08:58 message: the bookstore, then no answer
    for later in contexts[morning + 1:]:
        assert "continue" not in [m["type"] for m in later["violet_motives"]], later["violet_motives"]
        assert later["already_raised"][0]["about"] == PLAN


def test_replay_the_bookstore_door_never_reaches_the_user(replay):
    path, _, _ = replay
    events = fc.read_jsonl(path / fc.EVENTS)
    sent = [t for e in events for c in e.get("checks") or [] for t in c.get("proactive_sent") or []]
    assert not any("站在书店门口" in t for t in sent), sent
    guard = [g for e in events for g in e.get("guard") or [] if g["entity"] == "proactive_reply" and g["violations"]]
    assert any({"plan_described_as_done", "unrecorded_experience"} & set(g["violations"]) for g in guard), guard
    # and nothing else she wrote on her own was touched (22:01 "站在书架前肯定又要纠结" is a supposition)
    assert sent[:2] == ["小满，我周末想去书店挑本新书，可是站在书架前肯定又要纠结半天……", "你希望我挑什么样的呀，故事书还是小说"]
    assert "等会儿八成又要在小说区磨蹭很久，还在纠结要不要拿《活着》" in sent


def test_replay_taking_it_home_is_not_told_as_done(replay):
    path, _, _ = replay
    answer = next(e for e in fc.read_jsonl(path / fc.EVENTS) if e["kind"] == "user" and e["user"].startswith("没事的，先把他带回家"))
    assert "听你的，我带回家了" not in answer["violet"]
    assert any(set(g["violations"]) & {"plan_described_as_done", "unrecorded_experience"} for g in answer["guard"])


def test_replay_answering_her_message_brings_her_real_life(replay):
    path, _, _ = replay
    answer = next(e for e in fc.read_jsonl(path / fc.EVENTS) if e["kind"] == "user" and e["user"].startswith("没事的，先把他带回家"))
    chat = requests(path, "ChatAgent", answer["n"])[0]["request"]
    plan = [l for m in chat if m["role"] == "system" for l in str(m["content"]).splitlines() if l.startswith("- 打算：")]
    assert plan and PLAN in plan[0] and "计划，还没做" in plan[0]


def test_v2_says_what_a_line_break_means_without_counts():
    v2 = (cd.PROJECT / "persona/chat_prompt_versions/chat.v2.txt").read_text(encoding="utf-8")
    assert "每换一行，就是另外发出去的一条微信" in v2 and "一口气说完的话" in v2 and "不用凑条数" in v2
    assert "最多三条" not in v2 and "一到三条" not in v2
