"""Phase 8.9.12: what the real free chat ab-v2d showed (copied to tests/samples/free_chat/ab-v2d.p8912.json), fixed and
checked offline. "好啦，那我不打扰你看书了" is stepping back so as not to disturb her -- not leaving -- and now says so next
to the message (Intent had read it as "准备结束这轮对话", and v2 told her to answer an ending in one short line). Chat v2
lets an answer hand something back and lets her say she wants to keep talking. free_chat can run her life engine (its
own daily step, template wording, no model) while time passes, recorded and reproducible. The fake model checks the
plumbing; whether a real model keeps the talk going is for the tester."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from ai import interaction as ia
from tools import chat_dialogue as cd
from tools import free_chat as fc

SAMPLE = json.loads((cd.PROJECT / "tests/samples/free_chat/ab-v2d.p8912.json").read_text(encoding="utf-8"))
EPISODES = cd.PROJECT / "tests/samples/chat_dialogue/episodes.p894.v1.json"
URL = "http://127.0.0.1:18992/v1"
KEY = "fake-two-way-p8912-credential-0000"
CHAT_V2 = (cd.PROJECT / "persona/chat_prompt_versions/chat.v2.txt").read_text(encoding="utf-8")
YIELDING = next(s for s in SAMPLE["steps"] if s.get("user") == "好啦，那我不打扰你看书了")


def test_the_sample_is_the_real_session():
    assert YIELDING["sent"] == ["嗯嗯，那我去接着看，你也早点歇着"]
    assert "准备结束这轮对话" in YIELDING["intent"]


# ============================================================================== stepping back is not leaving

@pytest.mark.parametrize("text,kind", [
    ("好啦，那我不打扰你看书了", "yield"), ("那你先忙吧", "yield"), ("不吵你了", "yield"), ("你去看书吧", "yield"),
    ("那你忙你的～", "yield"),
    ("我去睡了", "closing"), ("晚安", "closing"), ("不打扰你了，我去洗澡了", "closing"), ("拜拜", "closing"),
    ("你先忙吗", "question"), ("我在想你", "share"),
])
def test_stepping_back_is_told_apart_from_leaving(text, kind):
    assert ia.turn_kind(text) == kind


@pytest.mark.parametrize("stage,expected", [
    ("close", ("不是对方自己要走", "更想跟对方多待一会儿", "撒个娇把对方留下", "开开心心地放对方走", "就顺着对方")),
    ("warming", ("不是对方自己要走", "更想跟对方多待一会儿")),
    ("new", ("不是对方自己要走", "接着聊就好", "不用强留")),
])
def test_stepping_back_gets_its_own_note(stage, expected):
    note = ia.turn_note(YIELDING["user"], None, stage=stage)
    assert all(e in note for e in expected), note


def test_leaving_still_gets_a_short_goodbye():
    note = ia.turn_note("我去睡了", None, stage="close")
    assert "回一句短的就好" in note and "不挽留" in note
    assert ia.turn_note("晚安", None, stage="new").startswith("【这一句】对方要去忙或去休息了")


# ============================================================================== Chat v2: answers that hand something back

def test_chat_v2_answers_hand_something_back_and_she_may_want_more():
    assert "答完停在那里也可以" not in CHAT_V2
    assert "答完可以顺手带出一点你自己的感受、一个小想法，或者你接下来想做的事，让对方好接，不一定要问回去" in CHAT_V2
    assert "有时候答一句也够" in CHAT_V2
    assert "也可以直接说你还想跟对方聊、想让对方陪你一会儿" in CHAT_V2
    assert "只是怕打扰你、让你先忙的，不算对方要走" in CHAT_V2
    # what earlier phases settled stays: no questions to keep it going, no length to fill, closeness answered
    for kept in ("不用靠提问把话头递回去", "通常做一件就够", "不用凑条数", "别客气地“收下”就完了", "对方要睡觉、要离开或想结束聊天时"):
        assert kept in CHAT_V2, kept


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
                return done(json.dumps({**json.loads(DECISIONS[i]["decision"]), "mentioned_memory_ids": []},
                                       ensure_ascii=False), model)
            return done(json.dumps({"should_act": False, "action_type": "none", "reason": "replay: no more decisions"}), model)
        if agent == "ChatAgent":
            if last.startswith("这是一次 Violet 主动触达"):
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


def run(path, lines, extra=()):
    argv = ["chat", "--session", str(path), "--real", "--api-url", URL, "--model", "cys", *extra]
    if not (path / fc.META).exists():
        argv += ["--chat-prompt", "v2", "--proactive-prompt", "v2", "--seed-episode", SAMPLE["seed_episode"],
                 "--episodes-file", str(EPISODES)]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIOLET_", "LLM_"))}
    env.update(PYTHONIOENCODING="utf-8", VIOLET_TEST_ALLOW_REAL_API="1", VIOLET_TEST_API_KEY=KEY, NO_PROXY="127.0.0.1,localhost")
    sample = cd.PROJECT / "tests/samples/free_chat/ab-v2d.p8912.json"
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOT, json.dumps(argv), str(sample)], cwd=cd.PROJECT,
                          env=env, input="\n".join(["yes", *lines, "/quit"]) + "\n", capture_output=True, text=True,
                          encoding="utf-8", timeout=1800)
    return done.returncode, done.stdout + done.stderr


SCRIPT = [s["user"] if "user" in s else f"/advance {s['advance']:g}" for s in SAMPLE["steps"]]


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    path = tmp_path_factory.mktemp("p8912") / "ab-v2d"
    return path, *run(path, SCRIPT)


def requests(path, agent, n=None):
    return [r["request"] for r in fc.read_jsonl(path / fc.REQUESTS) if r["agent"] == agent and (n is None or r["n"] == n)]


def test_replay_ran(replay):
    path, code, out = replay
    assert code == 0, out[-3000:]


def test_replay_stepping_back_reaches_chat_after_the_intent_reading(replay):
    path, _, _ = replay
    event = next(e for e in fc.read_jsonl(path / fc.EVENTS) if e["kind"] == "user" and e["user"] == YIELDING["user"])
    chat = requests(path, "ChatAgent", event["n"])[0]
    tail = [str(m["content"]) for m in chat[-4:] if m["role"] == "system"]
    joined = "\n".join(tail)
    assert "准备结束这轮对话" in joined and "不是对方自己要走" in joined
    assert joined.index("准备结束这轮对话") < joined.index("不是对方自己要走")        # the clarification comes last
    assert chat[0]["content"] == CHAT_V2.strip()


# ============================================================================== her life while time passes

@pytest.fixture(scope="module")
def life(tmp_path_factory):
    base = tmp_path_factory.mktemp("p8912life")
    a, b, off = base / "a", base / "b", base / "off"
    return {"a": (a, run(a, ["在干啥", "/advance 60"], ["--life", "on"]), run(a, ["/advance 30"])),
            "b": (b, run(b, ["在干啥", "/advance 60"], ["--life", "on"])),
            "off": (off, run(off, ["在干啥", "/advance 60"]))}


def life_runs(path):
    return [lr for e in fc.read_jsonl(path / fc.EVENTS) if e["kind"] == "time" for lr in e.get("life_runs") or []]


def test_life_runs_once_a_day_at_its_own_time_and_is_recorded(life):
    path, first, second = life["a"]
    assert first[0] == 0 and second[0] == 0, first[1][-2000:] + second[1][-2000:]
    runs = life_runs(path)
    keys = [r["run_key"] for r in runs]
    assert len(keys) == len(set(keys)) >= 3                         # one step per simulated day, across two launches
    assert all(r["at"][11:13] >= "21" for r in runs)               # after run_after_hour, as in production
    import sqlite3
    db = sqlite3.connect(path / "violet" / "violet-test.db")
    assert {k for (k,) in db.execute("SELECT run_key FROM life_engine_runs")} == set(keys)
    assert fc.read_meta(path)["life"] == "on"
    assert "生活引擎：on" in (path / fc.TRANSCRIPT).read_text(encoding="utf-8")


def test_life_is_reproducible_and_off_means_off(life):
    a, b = life["a"][0], life["b"][0]
    first = lambda p: [(r["run_key"], r["outcome"], [e["content"] for e in r["events"]]) for r in life_runs(p)][:3]
    assert first(a) == first(b)                                    # seeded by date: the same days give the same life
    off = life["off"][0]
    assert fc.read_meta(off)["life"] == "off" and life_runs(off) == []
    assert all("life_runs" not in e for e in fc.read_jsonl(off / fc.EVENTS))


def test_life_events_are_recorded_happenings_only(life):
    path = life["a"][0]
    import sqlite3
    from datetime import datetime
    db = sqlite3.connect(path / "violet" / "violet-test.db")
    for run_row in life_runs(path):
        for ev in run_row["events"]:
            assert ev["status"] == "happened"
            assert datetime.fromisoformat(ev["occurred_at"]) <= datetime.fromisoformat(run_row["at"])   # never ahead
    sources = {s for (s,) in db.execute("SELECT source FROM life_events WHERE source LIKE 'life_engine%'")}
    assert sources <= {"life_engine:template"}                      # no model wrote her life


def test_a_restart_after_the_days_step_does_not_run_it_again(tmp_path):
    path = tmp_path / "c"
    first = run(path, ["/advance 1"], ["--life", "on"])          # 20:30 -> 21:30: the step of 11-06 runs
    second = run(path, ["/advance 1"])                           # a new process at 21:30 the same day
    assert first[0] == 0 and second[0] == 0, first[1][-2000:] + second[1][-2000:]
    keys = [r["run_key"] for r in life_runs(path)]
    assert keys == ["2026-11-06"], keys


def test_a_session_keeps_its_life_setting(life):
    path = life["a"][0]
    code, out = run(path, [], ["--life", "off"])
    assert code == 2 and "不能换成 off" in out
