"""Phase 8.9.13: what the real free chat ab-v2e showed (copied to tests/samples/free_chat/ab-v2e.p8913.json), fixed and
checked offline. The guard took "我怕我看完难受好几天" and "这名字我听过好多回了" for experiences and left only
"不过你说的余味…"; the "waited" note invited the wait again after she had already said it; and a 14-day free_chat run
showed the life engine "completing" a plan that was not its own with "读完了《None》". Mechanism and model input are
checked here; whether real replies read better is not."""
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

SAMPLE = json.loads((cd.PROJECT / "tests/samples/free_chat/ab-v2e.p8913.json").read_text(encoding="utf-8"))
EPISODES = cd.PROJECT / "tests/samples/chat_dialogue/episodes.p894.v1.json"
URL = "http://127.0.0.1:18993/v1"
KEY = "fake-coherence-p8913-credential-0000"
BOOK = next(s for s in SAMPLE["steps"] if s.get("user", "").startswith("那推荐你看看余华的《活着》"))
BACK = next(s for s in SAMPLE["steps"] if s.get("user", "").startswith("来了来了"))
REWRITE = "《活着》啊，这名字我听过好多回了，一直有点想看，又有点怕——感觉是那种看完要难受好一阵的故事"


def test_the_sample_is_the_real_session():
    assert BOOK["sent"] == ["不过你说的余味，我好像又有点想要了"]
    assert BACK["sent"][0].startswith("你可算来了，我早上那条发出去就一直等")


# ============================================================================== the guard: misjudgments and misses

def checks(guard, text):
    return [v.check for v in guard.check_reply(text)]


@pytest.mark.parametrize("line", BOOK["chat"].split("\n\n") + [REWRITE, BOOK["sent"][0], *BACK["sent"],
    "我怕我读完要缓好几天", "担心我看完会哭", "听说看完会难过好久", "大家都说读完很沉", "这本书我听过名字",
    "我也听人说起过这本"])
def test_hearsay_fear_and_plans_are_no_claim(guard, line):
    assert checks(guard, line) == [], line


@pytest.mark.parametrize("line", ["我看完了《活着》", "昨天看完了《活着》", "我听过这首歌好多遍", "我去过那家书店",
                                  "我又往后读了几章《活着》", "看完了，好难过"])
def test_real_unrecorded_experiences_are_still_caught(guard, line):
    assert set(checks(guard, line)) & {"unrecorded_experience", "plan_described_as_done"}, line


def test_a_recorded_experience_still_backs_the_claim(guard, store):
    thread = new_thread(store, days_ago=5)
    happened(store, "读了几章《边城》", hours_ago=3, thread=thread)
    assert checks(guard, "我刚读了几章《边城》") == []


@pytest.mark.parametrize("text,bad,expected", [
    ("《活着》……这本我早就看完了\n不过你说的余味，我好像又有点想要了", ["《活着》……这本我早就看完了"],
     "你说的余味，我好像又有点想要了"),                                         # no line left leaning on nothing
    ("我昨天去了海边\n但是有点冷", ["我昨天去了海边"], "有点冷"),
    ("挺好的\n我昨天去了海边\n不过有点冷", ["我昨天去了海边"], "挺好的\n不过有点冷"),   # it still has something before it
    ("不过还好\n嗯", [], "不过还好\n嗯"),                                                 # nothing dropped: untouched
])
def test_dropping_a_line_leaves_a_whole_message(text, bad, expected):
    from ai.consistency import ConsistencyGuard, Violation
    assert ConsistencyGuard.drop_lines(text, [Violation("unrecorded_experience", "t", b) for b in bad]) == expected


# ============================================================================== coming back after she said she waited

def test_once_said_the_wait_is_not_brought_up_again():
    from ai.interaction import waited_note
    said = waited_note("close", 11, said="喂，早上那条发出去就没影了 我一个人等啊等的，快把自己等蔫了")
    assert "等的事你已经跟对方说过了（“喂，早上那条发出去就没影了" in said and "说过就翻篇" in said
    assert "不用再提等了多久" in said and "亲近、俏皮一点" in said
    once = waited_note("close", 11)
    assert "不一定要提等了多久" in once and "调侃一句让你等了好久" not in once


def proactive(db, at, texts):
    from tests.test_close_attachment_p8910 import sent_event
    sent_event(db, at, texts)


def test_the_context_knows_what_she_already_said(make_world, clock):
    from ai.context_engine import ContextEngine
    from tests.test_life_relationship_p89 import START, history
    w = make_world()
    history(w.db, 8, user_text="今天好累 想找你聊聊")
    proactive(w.db, START - timedelta(hours=11), ["早上醒了没事干，突然想到你推荐的那本《活着》"])
    engine = ContextEngine(w.db)
    note = engine.build("chat", "来了来了，刚才没看到消息", {"route": "chat"}).turn_note or ""
    assert "等了好一阵对方才回" in note and "说过就翻篇" not in note                 # one message: nothing said yet
    proactive(w.db, START - timedelta(hours=4), ["喂，早上那条发出去就没影了", "我一个人等啊等的，快把自己等蔫了"])
    note = engine.build("chat", "来了来了，刚才没看到消息", {"route": "chat"}).turn_note or ""
    assert "说过就翻篇" in note and "快把自己等蔫了" in note


# ============================================================================== her life: whose plan, which book

def test_finishing_a_thread_completes_only_the_engines_own_plan(life, store):
    from datetime import datetime
    thread = new_thread(store, days_ago=10)
    now = datetime.fromisoformat(store.db.now())
    seeded = store.create_plan(thread, "周末想去书店挑一本新书", (now - timedelta(days=2)).isoformat(), source="synthetic_seed")
    own = store.create_plan(thread, "这周想把《边城》读完", (now - timedelta(days=1)).isoformat())   # source life_engine
    engine = life(daily_event_probability=1.0, new_thread_probability=0.0,
                  plan_resolution={"complete_probability": 1.0, "abandon_after_days_overdue": 10})
    t = store.thread(thread)
    for _ in range(3):
        engine._advance(t, now, __import__("random").Random(1))
    status = {p["id"]: p["status"] for p in store.plans()}
    assert status[seeded] == "planned" and status[own] == "completed"


def test_a_seeded_thread_knows_its_item():
    assert cd._seed_item("读《小王子》") == "小王子"
    assert cd._seed_item("听几首钢琴曲") == "几首钢琴曲"
    assert cd._seed_item("周末去公园走走") is None


# ============================================================================== through the real pipeline

BOOT = r'''
import json, os, sys, time, uuid
from unittest.mock import patch
from openai.types.chat import ChatCompletion
from tools import chat_dialogue as cd
from tools.harness import _AGENT
SAMPLE = json.loads(open(sys.argv[2], encoding="utf-8").read())
BY_USER = {s["user"]: s for s in SAMPLE["steps"] if "user" in s}
DECISIONS = list(SAMPLE["proactive"]) if os.environ.get("P8913_MODE") == "replay" else []
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
            ctx = json.loads(last)
            shares = (ctx.get("conversation_state") or {}).get("allowed_life_events") or []
            if not DECISIONS and shares:                         # life mode: share what really happened
                return done(json.dumps({"should_act": True, "action_type": "chat", "reason": "life:" + shares[0]["content"],
                                        "topic_key": "life", "mentioned_life_event_ids": [shares[0]["id"]],
                                        "chat_brief": {"topic": shares[0]["content"]}, "task": None},
                                       ensure_ascii=False), model)
            i = STATE["decisions"]
            STATE["decisions"] += 1
            if i < len(DECISIONS):
                return done(json.dumps({**json.loads(DECISIONS[i]["decision"]), "mentioned_memory_ids": []},
                                       ensure_ascii=False), model)
            return done(json.dumps({"should_act": False, "action_type": "none", "reason": "fake: nothing"}), model)
        if agent == "ChatAgent":
            if last.startswith("这是一次 Violet 主动触达"):
                system = "\n".join(str(m.get("content")) for m in msgs if m.get("role") == "system")
                for d in DECISIONS:
                    if d["chat"] and json.loads(d["decision"])["reason"] in system:
                        return done(d["chat"], model)
                if "'reason': 'life:" in system:
                    return done("刚" + system.split("'reason': 'life:")[1].split("'")[0] + "，突然想跟你说一声", model)
                return done("[NO_REPLY]", model)
            if last in BY_USER:
                return done(BY_USER[last]["chat"], model)
        return done(super()._create(**kwargs).choices[0].message.content, model)
with patch("openai.OpenAI", FakeSDK):
    from tools import free_chat
    raise SystemExit(free_chat.main(json.loads(sys.argv[1])))
''' % KEY


def run(path, lines, mode):
    argv = ["chat", "--session", str(path), "--real", "--api-url", URL, "--model", "cys"]
    if not (path / fc.META).exists():
        argv += ["--chat-prompt", "v2", "--proactive-prompt", "v2", "--life", "on", "--seed-episode", SAMPLE["seed_episode"],
                 "--episodes-file", str(EPISODES)]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIOLET_", "LLM_"))}
    env.update(PYTHONIOENCODING="utf-8", VIOLET_TEST_ALLOW_REAL_API="1", VIOLET_TEST_API_KEY=KEY, NO_PROXY="127.0.0.1,localhost",
               P8913_MODE=mode)
    sample = cd.PROJECT / "tests/samples/free_chat/ab-v2e.p8913.json"
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOT, json.dumps(argv), str(sample)], cwd=cd.PROJECT,
                          env=env, input="\n".join(["yes", *lines, "/quit"]) + "\n", capture_output=True, text=True,
                          encoding="utf-8", timeout=1800)
    return done.returncode, done.stdout + done.stderr


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    path = tmp_path_factory.mktemp("p8913") / "ab-v2e"
    script = [s["user"] if "user" in s else f"/advance {s['advance']:g}" for s in SAMPLE["steps"]]
    return path, *run(path, script, "replay")


def event_of(path, user):
    return next(e for e in fc.read_jsonl(path / fc.EVENTS) if e["kind"] == "user" and e["user"] == user)


def test_replay_ran(replay):
    path, code, out = replay
    assert code == 0, out[-3000:]


def test_replay_the_book_reply_now_goes_out_whole(replay):
    path, _, _ = replay
    event = event_of(path, BOOK["user"])
    assert event["violet"] == BOOK["chat"].split("\n\n")                    # the model's first reply, untouched
    assert all(not g["violations"] for g in event["guard"])


def test_replay_coming_back_is_told_the_wait_was_already_said(replay):
    path, _, _ = replay
    event = event_of(path, BACK["user"])
    chat = next(r["request"] for r in fc.read_jsonl(path / fc.REQUESTS) if r["n"] == event["n"] and r["agent"] == "ChatAgent")
    system = "\n".join(str(m["content"]) for m in chat[-4:] if m["role"] == "system")
    assert "说过就翻篇" in system and "早上那条发出去就没影了" in system


@pytest.fixture(scope="module")
def days(tmp_path_factory):
    path = tmp_path_factory.mktemp("p8913life") / "days"
    code, out = run(path, ["在干嘛", *["/advance 24"] * 10, "在干嘛呢"], "life")
    return path, code, out


def test_days_of_life_are_continuous_and_honest(days):
    import sqlite3
    from datetime import datetime
    path, code, out = days
    assert code == 0, out[-3000:]
    db = sqlite3.connect(path / "violet" / "violet-test.db")
    runs = [r for e in fc.read_jsonl(path / fc.EVENTS) if e["kind"] == "time" for r in e.get("life_runs") or []]
    assert len({r["run_key"] for r in runs}) == len(runs) >= 10            # one step a day
    assert any(r["outcome"].startswith("quiet") for r in runs)              # ordinary days stay ordinary
    events = [e for r in runs for e in r["events"]]
    assert events and all("None" not in e["content"] for e in events)
    assert all(datetime.fromisoformat(e["occurred_at"]) <= datetime.fromisoformat(r["at"]) for r in runs for e in r["events"])
    plan = db.execute("SELECT status FROM life_plans WHERE content = '周末想去书店挑一本新书'").fetchone()[0]
    assert plan in ("planned", "abandoned")                                 # never "completed" by finishing a book


def test_days_of_life_reach_her_and_are_told_once(days):
    import sqlite3
    path, _, _ = days
    db = sqlite3.connect(path / "violet" / "violet-test.db")
    engine = [r[0] for r in db.execute("SELECT content FROM life_events WHERE source LIKE 'life_engine%' AND status = 'happened'")]
    sent = [t for e in fc.read_jsonl(path / fc.EVENTS) for c in e.get("checks") or [] for t in c.get("proactive_sent") or []]
    shared = [t for t in sent if any(c in t for c in engine)]
    assert shared                                                           # something of her own life went out
    assert len(shared) == len(set(shared))                                  # and each thing only once
    counts = [n for (n,) in db.execute("SELECT share_count FROM life_events WHERE source LIKE 'life_engine%'")]
    assert all(n <= 1 for n in counts)
    last = [e for e in fc.read_jsonl(path / fc.EVENTS) if e["kind"] == "user"][-1]
    chat = next(r["request"] for r in fc.read_jsonl(path / fc.REQUESTS) if r["n"] == last["n"] and r["agent"] == "ChatAgent")
    life_lines = [l for m in chat if m["role"] == "system" for l in str(m["content"]).splitlines() if "（已发生）" in l]
    assert any(any(c in l for c in engine) for l in life_lines)             # and her chat sees what happened
