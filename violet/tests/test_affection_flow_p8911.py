"""Phase 8.9.11: what the real free chat ab-v2c showed (copied to tests/samples/free_chat/ab-v2c.p8911.json), fixed and
checked offline. "嗯~有点忙，忙着想你呢" was closed with one neat line -- Chat had been told (wrongly) that two of its two
last replies were questions; Chat v2 now says to answer closeness with her own reaction. The first message after that
warm moment was the bookstore -- the only reason offered; the moment is now a reason of its own. The "unanswered" reason
no longer rules out playful sulking, and its wording no longer gives a phrase to copy. The fake model checks the
plumbing; how a real model answers is for the tester."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta

import pytest

from ai import interaction as ia
from tests.test_life_relationship_p89 import clock  # noqa: F401
from tests.test_phase2_scenarios import make_world  # noqa: F401
from tools import chat_dialogue as cd
from tools import free_chat as fc

SAMPLE = json.loads((cd.PROJECT / "tests/samples/free_chat/ab-v2c.p8911.json").read_text(encoding="utf-8"))
EPISODES = cd.PROJECT / "tests/samples/chat_dialogue/episodes.p894.v1.json"
URL = "http://127.0.0.1:18991/v1"
KEY = "fake-affection-p8911-credential-0000"
CHAT_V2 = (cd.PROJECT / "persona/chat_prompt_versions/chat.v2.txt").read_text(encoding="utf-8")
PROACTIVE_V1 = (cd.PROJECT / "persona/proactive_prompt_versions/proactive.v1.txt").read_text(encoding="utf-8")
PROACTIVE_V2 = (cd.PROJECT / "persona/proactive_prompt_versions/proactive.v2.txt").read_text(encoding="utf-8")
FIRST, SECOND = (s for s in SAMPLE["steps"] if "user" in s)


def test_the_sample_is_the_real_session():
    assert SECOND["user"] == "嗯~有点忙，忙着想你呢" and SECOND["sent"] == ["那这份忙，我可得好好收下了"]
    assert "书店" in SAMPLE["proactive"][0]["chat"] and "记得来理我一下" in SAMPLE["proactive"][1]["chat"]


# ============================================================================== the note that told Chat not to ask

def test_thinking_aloud_is_not_a_question():
    assert not ia.asks("刚翻完一本书，正想着要不要去泡杯什么喝")       # real round 1, first line
    assert not ia.asks("还在纠结要不要换个类型")
    assert ia.asks("你呢，在忙吗") and ia.asks("要不要一起去")


def test_one_reply_of_two_lines_is_one_reply():
    from ai.context_engine import _replies
    session = [{"role": "user", "content": "在干啥"}, {"role": "assistant", "content": FIRST["sent"][0]},
               {"role": "assistant", "content": FIRST["sent"][1]}]
    replies = _replies(session)
    assert replies == ["\n".join(FIRST["sent"])]
    note = ia.habit_note(replies, SECOND["user"], "chat", last_turn=FIRST["sent"]) or ""
    assert "条是用问句结尾的" not in note                      # the real round 2 got "2 of your last 2" -- wrong


def test_the_context_counts_replies_not_lines(make_world, clock):
    """Through the context engine: ONE earlier reply of two question lines is one reply that asked -- not "2 of 2"."""
    from ai.context_engine import ContextEngine
    from tests.test_life_relationship_p89 import START, add_message
    w = make_world()
    add_message(w.db, "user", "在干啥", START - timedelta(minutes=3))
    add_message(w.db, "assistant", "刚翻完一本书，你呢？", START - timedelta(minutes=2))
    add_message(w.db, "assistant", "在忙吗？", START - timedelta(minutes=2))
    note = ContextEngine(w.db).build("chat", SECOND["user"], {"route": "chat"}).turn_note or ""
    assert "条是用问句结尾的" not in note, note


# ============================================================================== Chat v2: closeness gets a reaction

def test_chat_v2_answers_closeness_with_her_own_reaction():
    assert "别客气地“收下”就完了" in CHAT_V2 and "害羞、开心得藏不住、嘴硬、反过来撩一下、小小地闹个别扭" in CHAT_V2
    assert "别用一句漂亮的总结把话收掉" in CHAT_V2 and "给对方留着接着逗你的余地" in CHAT_V2
    assert "玩完就可以停" not in CHAT_V2
    # still: no questions to keep it going, no length to fill, one thing is enough
    assert "不用靠提问把话头递回去" in CHAT_V2 and "通常做一件就够" in CHAT_V2 and "不用凑条数" in CHAT_V2


def test_proactive_v2_brief_is_no_script_and_sulking_is_not_reproach():
    assert "不要替 ChatAgent 写好原句" in PROACTIVE_V2 and "不要把 violet_motives 的说明原样抄进去" in PROACTIVE_V2
    assert "撒娇式的小委屈、玩笑式地闹小别扭、讨关注都可以有" in PROACTIVE_V2
    assert "不要把撒娇一起禁掉" in PROACTIVE_V2 and "认真地责备、反复催问、让对方愧疚" in PROACTIVE_V2
    assert "你怎么突然安静啦" in PROACTIVE_V1                         # the stable prompt is kept as it was


# ============================================================================== the warm moment as a reason

def world(make_world, stage="close"):
    from tests.test_life_relationship_p89 import START, add_message, history
    w = make_world()
    if stage == "close":
        history(w.db, 8, user_text="今天好累 想找你聊聊")
    else:
        add_message(w.db, "user", "你好呀", START - timedelta(days=1))
    return w


def talk(w, at, user_text):
    from tests.test_life_relationship_p89 import add_message
    add_message(w.db, "user", "在干啥", at - timedelta(minutes=1))
    add_message(w.db, "assistant", "刚翻完一本书", at - timedelta(minutes=1))
    add_message(w.db, "user", user_text, at)
    add_message(w.db, "assistant", "那这份忙，我可得好好收下了", at)


def motives(w, at):
    return w.scheduler._build_event_context(at)["violet_motives"]


def test_a_warm_moment_is_a_reason_for_her_first_message(make_world, clock):
    from tests.test_life_relationship_p89 import START
    w = world(make_world)
    talk(w, START - timedelta(hours=1, minutes=30), SECOND["user"])
    glow = next(m for m in motives(w, START) if m["type"] == "afterglow")
    assert "忙着想你呢" in glow["note"] and "不用硬换成别的话题" in glow["note"] and "也不用提问" in glow["note"]
    context = w.scheduler._build_event_context(START)
    assert "接着那份情绪往往比换个新话题自然" in context["motive_note"]


@pytest.mark.parametrize("stage,expected", [("close", "害羞或开心的心情"), ("warming", "接着那会儿的心情说一句"),
                                            ("new", None), ("distance", None)])
def test_the_glow_follows_how_close_you_are(stage, expected):
    from ai.relationship import RelationshipView, proactive_motives
    found = [m for m in proactive_motives(RelationshipView(stage), 2, 0, [], [], afterglow={"at": "20:30", "quote": "忙着想你呢"})
             if m["type"] == "afterglow"]
    assert (not found) if expected is None else (expected in found[0]["note"] and "忙着想你呢" in found[0]["note"])


@pytest.mark.parametrize("case", ["plain talk", "long ago", "she wrote since"])
def test_no_glow_without_a_warm_recent_moment(make_world, clock, case):
    from tests.test_life_relationship_p89 import START
    from tests.test_close_attachment_p8910 import sent_event
    w = world(make_world)
    if case == "plain talk":
        talk(w, START - timedelta(hours=1), "嗯，刚下课")
    elif case == "long ago":
        talk(w, START - timedelta(hours=13), SECOND["user"])
    else:
        talk(w, START - timedelta(hours=3), SECOND["user"])
        sent_event(w.db, START - timedelta(hours=1), ["周末想去书店挑本新书"])
    assert "afterglow" not in [m["type"] for m in motives(w, START)], case


def test_the_unanswered_note_allows_sulking_and_gives_nothing_to_copy(make_world, clock):
    from tests.test_life_relationship_p89 import START
    from tests.test_close_attachment_p8910 import sent_event
    w = world(make_world)
    sent_event(w.db, START - timedelta(hours=10), ["周末想去书店挑本新书"])
    note = next(m for m in motives(w, START) if m["type"] == "miss_unanswered")["note"]
    assert "假装生气、嘟囔自己被晾着了、要对方哄一哄" in note and "这些都是撒娇，可以说出来" in note
    assert "认真地指责" in note and "让对方愧疚" in note and "像轻轻戳一下" not in note


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

SCRIPT = ["yes"] + [s["user"] if "user" in s else f"/advance {s['advance']:g}" for s in SAMPLE["steps"]] + ["/quit"]


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    path = tmp_path_factory.mktemp("p8911") / "ab-v2c"
    argv = ["chat", "--session", str(path), "--real", "--api-url", URL, "--model", "cys", "--chat-prompt", "v2",
            "--proactive-prompt", "v2", "--seed-episode", SAMPLE["seed_episode"], "--episodes-file", str(EPISODES)]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIOLET_", "LLM_"))}
    env.update(PYTHONIOENCODING="utf-8", VIOLET_TEST_ALLOW_REAL_API="1", VIOLET_TEST_API_KEY=KEY, NO_PROXY="127.0.0.1,localhost")
    sample = cd.PROJECT / "tests/samples/free_chat/ab-v2c.p8911.json"
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOT, json.dumps(argv), str(sample)], cwd=cd.PROJECT,
                          env=env, input="\n".join(SCRIPT) + "\n", capture_output=True, text=True, encoding="utf-8",
                          timeout=1800)
    return path, done.returncode, done.stdout + done.stderr


def requests(path, agent, n=None):
    return [r["request"] for r in fc.read_jsonl(path / fc.REQUESTS) if r["agent"] == agent and (n is None or r["n"] == n)]


def test_replay_ran(replay):
    path, code, out = replay
    assert code == 0, out[-3000:]


def test_replay_round_2_is_not_told_to_stop_asking_and_sees_the_new_v2(replay):
    path, _, _ = replay
    second = next(e for e in fc.read_jsonl(path / fc.EVENTS) if e["kind"] == "user" and e["user"] == SECOND["user"])
    chat = requests(path, "ChatAgent", second["n"])[0]
    system = "\n".join(str(m["content"]) for m in chat if m["role"] == "system")
    assert "条是用问句结尾的" not in system                            # the wrong note of the real run
    assert "对这一句的第一感觉" in system and "调皮" in system
    assert chat[0]["content"] == CHAT_V2.strip()


def test_replay_her_first_message_may_carry_the_warm_moment(replay):
    path, _, _ = replay
    first = json.loads(requests(path, "ProactiveAgent")[0][-1]["content"])
    kinds = [m["type"] for m in first["violet_motives"]]
    assert kinds[0] == "afterglow" and "ask_opinion" in kinds              # offered first; the plan is still there
    assert "忙着想你呢" in first["violet_motives"][0]["note"]


def test_replay_the_later_one_gets_the_new_unanswered_note(replay):
    path, _, _ = replay
    contexts = [json.loads(r[-1]["content"]) for r in requests(path, "ProactiveAgent")]
    notes = [m["note"] for c in contexts for m in c["violet_motives"] if m["type"] == "miss_unanswered"]
    assert notes and all("这些都是撒娇" in n and "像轻轻戳一下" not in n for n in notes)
    assert not any(m["type"] == "afterglow" for c in contexts[1:] for m in c["violet_motives"])   # once, before she writes
