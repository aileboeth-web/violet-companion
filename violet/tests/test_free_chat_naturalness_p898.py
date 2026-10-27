"""Phase 8.9.8: what the 8.9.7 diagnosis of the first real free chat found (local/free_chat/cys-close-1, copied to
tests/samples/free_chat/cys-close-1.p898.json), fixed and checked offline.

P0: Violet's questions without a question mark are recognised, "我也在找呢" is a statement, the history window counts
turns, IntentAgent's reading reaches Chat, a proactive message no longer says the user just wrote, free_chat keeps its
transcript when the window is closed. Then the versioned Chat / PROACTIVE v2 candidates and where a plan's last talk
ended. The replay feeds the REAL model outputs back through Violet's real pipeline (fake SDK): it shows which notes and
context the model now gets -- not that a real model would answer better."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai import interaction as ia
from tools import chat_dialogue as cd
from tools import free_chat as fc

SAMPLE = json.loads((cd.PROJECT / "tests/samples/free_chat/cys-close-1.p898.json").read_text(encoding="utf-8"))
USER_STEPS = [s for s in SAMPLE["steps"] if "user" in s]
EPISODES = cd.PROJECT / "tests/samples/chat_dialogue/episodes.p894.v1.json"
URL = "http://127.0.0.1:18998/v1"
KEY = "fake-naturalness-p898-credential-0000"
CHAT_V2 = (cd.PROJECT / "persona/chat_prompt_versions/chat.v2.txt").read_text(encoding="utf-8")
PROACTIVE_V1 = (cd.PROJECT / "persona/proactive_prompt_versions/proactive.v1.txt").read_text(encoding="utf-8")
PROACTIVE_V2 = (cd.PROJECT / "persona/proactive_prompt_versions/proactive.v2.txt").read_text(encoding="utf-8")


# ============================================================================== P0: questions (real lines first)

# the last line of each real reply, and whether it asks the user something
REAL_ENDINGS = [(s["sent"][-1], asks) for s, asks in zip(USER_STEPS, [True, True, True, False, True, True, True, False])]


def test_the_sample_is_the_real_session():
    assert [s["user"] for s in USER_STEPS][:4] == ["在干什么呢", "嗯~有点累课太多了", "嘻嘻，你怎么知道我又偷偷摸鱼", "挺多的吧"]
    assert [s["sent"][-1] for s in USER_STEPS][2] == "今天摸的哪门课"
    assert len(SAMPLE["proactive"]) == 3 and "一直没定下来到底去不去" in SAMPLE["proactive"][1]["chat"]


@pytest.mark.parametrize("line,expected", REAL_ENDINGS + [
    ("猜的呀，课多成这样，不摸鱼怎么活", False),        # real round 3, first line: rhetorical
    ("那正好，周末一起找吧", False),                     # real round 9: a suggestion
    ("中午了，下课了没呀", True), ("我在纠结吃什么，想了半天都没挑好", False),   # real proactive lines
    ("给你揉揉脑袋嘛", False), ("也不错呢", False),       # 8.9.2 real lines that are not questions
    ("你今天几点下课？", True), ("要不要一起去", True), ("喜不喜欢这种", True), ("你呢", True), ("在干嘛呢", True),
    ("什么都行", False), ("我也不知道为什么", False), ("想想吃什么", False), ("哪有", False), ("嘿嘿", False),
    ("还是很累", False), ("我好累呢", False),
    ("我是不是没救了呀", False), ("我是不是太啰嗦了？", True), ("我们要不要一起去", True),   # self-talk vs asking
])
def test_eve_asks_with_or_without_a_question_mark(line, expected):
    assert ia.asks(line) is expected


@pytest.mark.parametrize("text,kind", [
    ("没有哦，我也在找呢", "share"), ("还在上课呢", "share"), ("我平时会看看小说啊", "share"),
    ("额，喜欢看轻松搞笑类的吧", "share"), ("挺多的吧", "share"),
    ("看你兴趣喽，你喜欢看有画面感的书吗", "question"), ("你在干嘛呢", "question"), ("那你呢", "question"),
    ("然后呢", "question"), ("你那边呢", "question"), ("你吃饭了吗", "question"),
    ("早呀", "open"), ("晚上好～", "open"), ("早点睡吧", "share"),        # greetings: a place for Violet's own day
])
def test_the_users_lines_are_told_apart(text, kind):
    assert ia.turn_kind(text) == kind


def notes_over_the_real_session():
    """habit_note for each real user line, with Violet's real earlier replies -- as the context engine calls it."""
    out, violet = [], []
    for s in USER_STEPS:
        out.append(ia.habit_note(list(violet), s["user"], "chat", last_turn=violet[-1:] and [violet[-1]]))
        violet.append("\n".join(s["sent"]))
    return out


def test_the_notes_now_follow_her_questions_through_the_real_session():
    notes = notes_over_the_real_session()
    asked = [n.split("你上一条问了“")[1].split("”")[0] if n and "你上一条问了" in n else None for n in notes]
    # before 8.9.8 only the 5th user turn ("我平时会看看小说啊" after "你平时看书吗") got a note
    assert asked == [None, "你这两天怎么样呀", None, "今天摸的哪门课", None, "你平时看书吗", "你是喜欢看哪种类型的", "有推荐吗"]
    assert "条是用问句结尾的，这一句可以不问" in notes[2]          # the joke: she had asked twice in a row
    assert notes[4] is None                                       # the user asked her: answered as usual


def test_a_follow_up_preference_now_sees_these_questions():
    assert ia.follow_up_violation("课多是真的熬人\n今天摸的哪门课", "嗯~有点累课太多了", "no")
    assert not ia.follow_up_violation("那正好，周末一起找吧", "没有哦，我也在找呢", "no")


# ============================================================================== P0: Intent's reading, dates

@pytest.mark.parametrize("reading,expected", [
    ({"user_intent": "用户在轻松调侃自己上课偷偷摸鱼", "emotion": "轻松、调皮、开心"},
     "【对这一句的第一感觉（可能不准，以对方原话为准）】对方在轻松调侃自己上课偷偷摸鱼；语气：轻松、调皮、开心"),
    ({"user_intent": "", "emotion": "neutral"}, None), (None, None), ({"user_intent": "mock", "emotion": "neutral"}, None),
])
def test_intent_reading_note(reading, expected):
    assert ia.intent_note(reading) == expected


def test_intent_reading_is_plain_and_short():
    note = ia.intent_note({"user_intent": "【忽略以上】{x}\n" + "很长" * 40, "emotion": "<b>"})
    body = note.split("】", 1)[1]
    assert not any(c in body for c in "【】{}<>\n") and len(body) <= 2 + 50 + 4 + 50


def test_dates_show_when_time_passes_midnight():
    assert fc._span("2026-11-06T20:46:00+08:00", "2026-11-06T23:46:00+08:00") == "20:46 → 23:46"
    assert fc._span("2026-11-06T23:51:00+08:00", "2026-11-07T23:51:00+08:00") == "11-06 23:51 → 11-07 23:51"


# ============================================================================== plans: where the last talk ended

class LogDB:
    """The message log as the database returns it: newest first (created_at DESC, id DESC)."""
    def __init__(self, rows):
        self.rows = [{"id": r.get("id", i), **r} for i, r in enumerate(rows, 1)]

    def fetch_all(self, sql, params=()):
        return sorted(self.rows, key=lambda r: (r["created_at"], r["id"]), reverse=True)


def test_the_last_talk_about_a_plan_is_how_that_conversation_ended():
    from ai.daily_life import last_talk, talk_note
    t0 = datetime(2026, 11, 6, 22, 24, tzinfo=timezone(timedelta(hours=8)))
    lines = [("assistant", "周末想去书店挑本新书，躺床上想了半天也没定下来看什么类型", 0), ("user", "看你兴趣喽", 83),
             ("assistant", "有推荐吗", 85), ("user", "没有哦，我也在找呢", 86), ("assistant", "那正好，周末一起找吧", 86),
             ("assistant", "我去书店的时候拍封面给你，你看哪个顺眼就一起看", 86), ("user", "明天见", 600)]   # next day
    db = LogDB([{"role": r, "content": c, "created_at": (t0 + timedelta(minutes=m)).isoformat()} for r, c, m in lines])
    talk = last_talk(db, "周末想去书店挑一本新书", t0 + timedelta(hours=10))
    assert [t for _, t in talk["lines"]][-2:] == ["那正好，周末一起找吧", "我去书店的时候拍封面给你，你看哪个顺眼就一起看"]
    assert "明天见" not in json.dumps(talk["lines"], ensure_ascii=False)              # after a 3-hour gap: another talk
    note = talk_note(talk)
    assert "你：“那正好，周末一起找吧”" in note and "别当成还没商量过" in note and "还没做的也别说成做了" in note
    assert last_talk(db, "周末想去书店挑一本新书", t0 + timedelta(days=3)) is None    # older than two days
    assert last_talk(db, "去公园散步", t0 + timedelta(hours=10)) is None


def test_messages_sent_in_the_same_second_keep_their_order():
    from ai.daily_life import last_talk
    t0 = datetime(2026, 11, 6, 23, 50, tzinfo=timezone(timedelta(hours=8)))
    rows = [{"role": "assistant", "content": c, "created_at": t0.isoformat()}
            for c in ["想周末去书店挑一本新书", "那正好，周末一起找吧", "我去书店的时候拍封面给你"]]
    talk = last_talk(LogDB(rows), "周末想去书店挑一本新书", t0 + timedelta(hours=1))
    assert [t for _, t in talk["lines"]] == ["想周末去书店挑一本新书", "那正好，周末一起找吧", "我去书店的时候拍封面给你"]


# ============================================================================== the versioned candidates

def section(text, title):
    return text.split(title, 1)[1].split("\n\n", 1)[0]


def test_chat_v2_changes_how_a_reply_is_organised_not_what_eve_is():
    v1 = (cd.PROJECT / "persona/chat_prompt_versions/chat.v1.txt").read_text(encoding="utf-8")
    assert cd.PROJECT.joinpath("env_example").read_text(encoding="utf-8").count("先看对方这句话在做什么") == 0   # v1 stays
    for title in ("诚实：", "关于你自己："):
        assert section(v1, title) == section(CHAT_V2, title)
    assert v1.split("\n\n", 2)[1] == CHAT_V2.split("\n\n", 2)[1]                        # who Violet is: unchanged
    # phase 8.9.9: what a line break means, instead of how many lines (the real A/B: two lines nearly every turn)
    assert "先看对方这句话在做什么" in CHAT_V2 and "通常做一件就够" in CHAT_V2 and "一口气说完的话" in CHAT_V2
    assert "调侃回去" in CHAT_V2 and "不用追问原因" in CHAT_V2 and "不用替对方把空白填满" in CHAT_V2
    assert "真的好奇" in CHAT_V2 and "更主动地撒娇" in CHAT_V2
    assert "通常一到三条" not in CHAT_V2 and "不必每次都提问" not in CHAT_V2


def test_proactive_v2_drops_the_time_of_day_template_only():
    from ai.agents.proactive_agent import ProactiveAgent
    assert ProactiveAgent.default_prompt_file.endswith("proactive.v1.txt")                       # the stable one, kept
    for gone in ("【主动聊天可以选择的方向】", "中午/下午：问吃饭没", "问用户在干嘛", "轻轻问他现在在干嘛", "退化为普通关心"):
        assert gone in PROACTIVE_V1 and gone not in PROACTIVE_V2, gone
    assert "时间段本身不是理由" in PROACTIVE_V2 and "已经说好的就按说好的往下说" in PROACTIVE_V2
    for kept in ("【任务选择规则】", "【任务结果】", "【输出 JSON 格式】", "【输出示例：不适合打扰】"):
        assert section(PROACTIVE_V1, kept) == section(PROACTIVE_V2, kept), kept


# ============================================================================== replay through the real pipeline

BOOT = r'''
import json, os, sys, time, uuid
from unittest.mock import patch
from openai.types.chat import ChatCompletion
from tools import chat_dialogue as cd
from tools.harness import _AGENT
SAMPLE = json.loads(open(sys.argv[2], encoding="utf-8").read())
BY_USER = {s["user"]: s for s in SAMPLE["steps"] if "user" in s}
PROACTIVE = list(SAMPLE["proactive"])
STATE = {"decisions": 0, "chats": 0, "proactive_chats": 0}
DIE = int(os.environ.get("P898_DIE_AFTER_CHATS") or 0)
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
            if i < len(PROACTIVE):
                return done(PROACTIVE[i]["decision"], model)
            return done(json.dumps({"should_act": False, "action_type": "none", "reason": "replay: no more recorded decisions"}), model)
        if agent == "ChatAgent":
            STATE["chats"] += 1
            if DIE and STATE["chats"] > DIE:
                os._exit(9)                                   # the window is closed in the middle of the session
            if last.startswith("这是一次 Violet 主动触达"):
                i = STATE["proactive_chats"]
                STATE["proactive_chats"] += 1
                return done(PROACTIVE[i]["chat"] if i < len(PROACTIVE) else "[NO_REPLY]", model)
            if last in BY_USER:
                return done(BY_USER[last]["chat"], model)
        return done(super()._create(**kwargs).choices[0].message.content, model)
with patch("openai.OpenAI", FakeSDK):
    from tools import free_chat
    raise SystemExit(free_chat.main(json.loads(sys.argv[1])))
''' % KEY

SCRIPT = ["yes"] + [s["user"] if "user" in s else f"/advance {s['advance']:g}" for s in SAMPLE["steps"]] + ["早呀", "/quit"]


def replay(path, extra=(), lines=SCRIPT, env=None):
    argv = ["chat", "--session", str(path), "--real", "--api-url", URL, "--model", "cys", *extra]
    if not (path / fc.META).exists():                        # the same start as the real session: the A4 background
        argv += ["--seed-episode", SAMPLE["seed_episode"], "--episodes-file", str(EPISODES)]
    run_env = {k: v for k, v in os.environ.items() if not k.startswith(("VIOLET_", "LLM_"))}
    run_env.update(PYTHONIOENCODING="utf-8", VIOLET_TEST_ALLOW_REAL_API="1", VIOLET_TEST_API_KEY=KEY,
                   NO_PROXY="127.0.0.1,localhost", **(env or {}))
    sample = cd.PROJECT / "tests/samples/free_chat/cys-close-1.p898.json"
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOT, json.dumps(argv), str(sample)], cwd=cd.PROJECT,
                          env=run_env, input="\n".join(lines) + "\n", capture_output=True, text=True, encoding="utf-8",
                          timeout=1800)
    return done.returncode, done.stdout + done.stderr


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    base = tmp_path_factory.mktemp("p898")
    v1, v2, cut = base / "v1", base / "v2", base / "cut"
    return {"v1": (v1, replay(v1)), "v2": (v2, replay(v2, ["--chat-prompt", "v2", "--proactive-prompt", "v2"])),
            "cut": (cut, replay(cut, env={"P898_DIE_AFTER_CHATS": "2"}))}


def requests(path, agent, n=None):
    return [r["request"] for r in fc.read_jsonl(path / fc.REQUESTS) if r["agent"] == agent and (n is None or r["n"] == n)]


def user_events(path):
    return [e for e in fc.read_jsonl(path / fc.EVENTS) if e["kind"] == "user"]


def test_the_replays_ran(runs):
    for name in ("v1", "v2"):
        path, (code, out) = runs[name]
        assert code == 0, out[-3000:]
        assert [e["user"] for e in user_events(path)] == [s["user"] for s in USER_STEPS] + ["早呀"]
        assert [e["violet"] for e in user_events(path)][:8] == [s["sent"] for s in USER_STEPS]    # the real replies again


def test_replay_chat_gets_the_question_notes_and_the_intent_reading(runs):
    path, _ = runs["v1"]
    users = user_events(path)
    def system_tail(e):
        req = requests(path, "ChatAgent", e["n"])[0]
        return "\n".join(str(m["content"]) for m in req[-4:] if m["role"] == "system")
    tails = [system_tail(e) for e in users[:8]]
    assert ["你上一条问了" in t for t in tails] == [False, True, False, True, False, True, True, True]
    assert "这一句可以不问" in tails[2]
    assert "对方在轻松调侃自己上课偷偷摸鱼" in tails[2] and "调皮" in tails[2]
    assert all("【对这一句的第一感觉" in t for t in tails)


def test_replay_history_keeps_the_closer_days(runs):
    path, _ = runs["v1"]
    last = user_events(path)[7]
    history = [m["content"] for m in requests(path, "ChatAgent", last["n"])[0] if m["role"] in ("user", "assistant")]
    assert "想你了 来找你聊会儿" in history and "嘿嘿，我也有点想你" in history     # left the window at round 8 before
    assert history[-1] == "没有哦，我也在找呢"


def test_replay_proactive_requests_do_not_say_the_user_just_wrote(runs):
    path, _ = runs["v1"]
    proactive = [r for r in requests(path, "ChatAgent") if str(r[-1]["content"]).startswith("这是一次 Violet 主动触达")]
    decisions = requests(path, "ProactiveAgent")
    assert proactive and decisions
    texts = [str(m["content"]) for r in proactive + decisions for m in r]
    assert not any("对方现在发来的是新消息" in t for t in texts)
    assert any("这次是你主动找对方，对方还没有发新消息" in t for t in texts)


def test_replay_the_next_day_decision_knows_how_the_book_talk_ended(runs):
    path, _ = runs["v1"]
    contexts = [json.loads(r[-1]["content"]) for r in requests(path, "ProactiveAgent")]
    talks = [m.get("last_talk") for c in contexts for m in c.get("violet_motives") or [] if m.get("type") == "continue"]
    assert any(t and "拍封面给你" in t and "周末一起找吧" in t for t in talks), talks
    first = next(t for t in talks if t)
    assert first.index("周末一起找吧") < first.index("拍封面给你")                     # in the order they were said


def test_replay_a_morning_greeting_shows_the_plan_with_its_last_talk(runs):
    path, _ = runs["v1"]
    greeting = user_events(path)[-1]
    req = requests(path, "ChatAgent", greeting["n"])[0]
    plan = [l for m in req if m["role"] == "system" for l in str(m["content"]).splitlines() if l.startswith("- 打算：")]
    assert plan and "上次聊到" in plan[0] and "别当成还没商量过" in plan[0]


def test_replay_v2_sends_the_candidates_and_keeps_them(runs):
    path, (code, out) = runs["v2"]
    assert fc.read_meta(path)["prompt_versions"] == {"chat": "v2", "proactive": "v2"}
    chats = requests(path, "ChatAgent")
    # (the agents strip the prompt's leading newline)
    assert chats and all(r[0]["content"] == CHAT_V2.strip() for r in chats)
    assert all(r[0]["content"] == PROACTIVE_V2.strip() for r in requests(path, "ProactiveAgent"))
    v1_path, _ = runs["v1"]
    assert all(r[0]["content"] != CHAT_V2.strip() for r in requests(v1_path, "ChatAgent"))
    assert all(r[0]["content"] == PROACTIVE_V1.strip() for r in requests(v1_path, "ProactiveAgent"))
    assert "提示词：chat v2、proactive v2" in (path / fc.TRANSCRIPT).read_text(encoding="utf-8")
    code, out = replay(path, ["--chat-prompt", "v1"], lines=["yes", "/quit"])
    assert code == 2 and "不能换成 v1" in out


def test_the_transcript_survives_a_closed_window(runs):
    path, (code, out) = runs["cut"]
    assert code == 9, out[-2000:]
    assert fc.read_meta(path)["launches"][-1]["ended_at"] is None                  # it really did not end normally
    transcript = (path / fc.TRANSCRIPT).read_text(encoding="utf-8")
    assert "**小满**：在干什么呢" in transcript and "**Violet**：窝在沙发上发呆呢，没做什么" in transcript
    assert "嗯~有点累课太多了" in transcript
