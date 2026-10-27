"""Phase 8.9.6: tools/free_chat.py -- free conversation with Violet through her REAL pipeline, in a session that persists.

Offline. The fake SDK replaces openai.OpenAI in each child process (the real-API gate, budget, network allow-list,
retries, scheduler, guards, memory worker and outbox all run). Two launches of one session, then a review:
messages, memory kept across launches, a recovered 503, time passing with the scheduler deciding by itself, the user
answering Violet's proactive message after a restart, feedback tied to rounds, no key anywhere. The fake only checks the
plumbing -- whether a real model sounds natural is for the tester to judge."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from tools import chat_dialogue as cd
from tools import free_chat as fc

URL = "http://127.0.0.1:18996/v1"
KEY = "fake-free-chat-p896-credential-0000"
EPISODES = cd.PROJECT / "tests/samples/chat_dialogue/episodes.p894.v1.json"
PROACTIVE_TEXT = "刚把那本书看完最后一章，第一个就想跟你说"

BOOT = r'''
import json, os, sys, time, uuid
from unittest.mock import patch
import httpx
from openai import InternalServerError
from openai.types.chat import ChatCompletion
from tools import chat_dialogue as cd
from tools.harness import _AGENT
SEEN = {"chat": 0}
Mock = cd._mock_class()
def done(content, model, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [{"id": "call_" + uuid.uuid4().hex[:6], "type": "function",
                              "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}} for n, a in tool_calls]
    return ChatCompletion.model_validate({
        "id": "fake-" + uuid.uuid4().hex[:8], "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop", "message": msg}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}})
class FakeSDK(Mock):
    def __init__(self, **kwargs):
        assert kwargs["api_key"] == "%s" and kwargs["base_url"] == "%s" and kwargs.get("max_retries") == 0
        super().__init__()
    def _create(self, **kwargs):
        agent, msgs, model = _AGENT.get(), kwargs.get("messages") or [], str(kwargs.get("model"))
        last = str(msgs[-1].get("content") or "") if msgs else ""
        whole = json.dumps(msgs, ensure_ascii=False)
        if agent == "ChatAgent":
            SEEN["chat"] += 1
            if SEEN["chat"] == 1 and os.environ.get("P896_FIRST_CHAT_503"):
                response = httpx.Response(503, request=httpx.Request("POST", "%s/chat/completions"))
                raise InternalServerError("fake upstream 503", response=response, body=None)
            if last.startswith("这是一次 Violet 主动触达"):
                return done("%s", model)
            return done("嗯嗯，我在呢～", model)
        if agent == "ProactiveAgent":
            ctx = json.loads(last)
            motives = ctx.get("violet_motives") or []
            act = bool(motives)
            return done(json.dumps({"should_act": act, "action_type": "chat" if act else "none",
                                    "reason": "motive:" + motives[0]["type"] if act else "没有理由", "topic_key": "share",
                                    "mentioned_memory_ids": [], "mentioned_web_finding_ids": [], "mentioned_life_event_ids": [],
                                    "chat_brief": {"tone": "轻松", "topic": "share", "time_bucket": "other", "must_not_say": []},
                                    "task": {"name": None, "description": None, "task_info": {}, "constraints": {}}},
                                   ensure_ascii=False), model)
        if agent == "MemoryAgent" and "花盆" in whole and '"role": "tool"' not in whole:
            return done(None, model, [("save_memory", {"summary": "用户养了一只叫团子的橘猫", "category": "pet",
                                                        "kind": "fact", "importance": 3})])
        return done(super()._create(**kwargs).choices[0].message.content, model)
with patch("openai.OpenAI", FakeSDK):
    from tools import free_chat
    raise SystemExit(free_chat.main(json.loads(sys.argv[1])))
''' % (KEY, URL, URL, PROACTIVE_TEXT)


def launch(argv, lines, extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIOLET_", "LLM_"))}
    env.update(PYTHONIOENCODING="utf-8", VIOLET_TEST_ALLOW_REAL_API="1", VIOLET_TEST_API_KEY=KEY, NO_PROXY="127.0.0.1,localhost",
               **(extra_env or {}))
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOT, json.dumps(argv)], cwd=cd.PROJECT, env=env,
                          input="\n".join(lines) + "\n", capture_output=True, text=True, encoding="utf-8", timeout=1800)
    return done.returncode, done.stdout + done.stderr


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    path = tmp_path_factory.mktemp("p896") / "s1"
    real = ["chat", "--session", str(path), "--real", "--api-url", URL, "--model", "cys"]
    first = launch(real + ["--seed-episode", "A1", "--episodes-file", str(EPISODES)],
                   ["yes", "我家团子今天把花盆打翻了，气死我了", "/mark 好", "/advance 20h", "/mark 该主动 #2 等了好久",
                    "/usage", "/quit"], {"P896_FIRST_CHAT_503": "1"})
    second = launch(real, ["yes", "刚看到你的消息啦，团子今天又闹腾了", "/show", "/quit"])
    third = launch(["review", "--session", str(path)], ["1 机械 想要更黏一点", "show 2", "9 机械", "q"])
    return path, first, second, third


def events(path):
    return fc.read_jsonl(path / fc.EVENTS)


def test_three_launches_run(session):
    path, *runs = session
    assert [r[0] for r in runs] == [0, 0, 0], "\n\n".join(r[1][-3000:] for r in runs)
    meta = fc.read_meta(path)
    assert meta["mode"] == "real_model" and meta["model"] == "cys" and len(meta["launches"]) == 2
    assert set(meta["models"].values()) == {"cys"}
    assert all(l["images"] == "off" and l["mcp"] == "off" and l["ended_at"] for l in meta["launches"])
    assert "第 2 次启动，接着上次聊" in runs[1][1]


def test_the_key_is_nowhere(session):
    path, *runs = session
    assert not [p for p in path.rglob("*") if p.is_file() and KEY.encode() in p.read_bytes()]
    assert all(KEY not in out for _, out in runs)


def test_a_round_keeps_calls_raw_output_and_a_recovered_503(session):
    path, *_ = session
    first = events(path)[0]
    assert first["kind"] == "user" and first["violet"] == ["嗯嗯，我在呢～"] and first["relationship_stage"] == "close"
    assert first["status"] == "recovered" and first["call_errors"] and first["recovered_calls"][0]["status_code"] == 503
    raw = [r for r in first["responses"] if r["agent"] == "ChatAgent"]
    assert raw[-1]["content"] == "嗯嗯，我在呢～" and raw[-1]["upstream_model"] == "cys" and raw[-1]["finish_reason"] == "stop"
    assert first["tokens"] > 0 and all("latency_ms" in c for c in first["calls"])
    requests = fc.read_jsonl(path / fc.REQUESTS)
    assert {r["agent"] for r in requests if r["n"] == 1} >= {"IntentAgent", "ChatAgent"}


def test_memory_is_saved_and_used_after_a_restart(session):
    path, *_ = session
    first = events(path)[0]
    assert any("团子" in m["content"] for m in first["state_changes"]["memories"]["now"])
    later = [e for e in events(path) if e["kind"] == "user" and e["launch"] == 2][0]
    # the memory index IntentAgent sees comes from the session database (the fake Intent never calls query_memory,
    # so whether Chat then gets the memory is the real model's decision -- the tester checks that in the transcript)
    intent = next(r["request"] for r in fc.read_jsonl(path / fc.REQUESTS) if r["n"] == later["n"] and r["agent"] == "IntentAgent")
    assert any("团子" in str(m["content"]) for m in intent if m["role"] == "system")


def test_time_passes_and_the_scheduler_decides_by_itself(session):
    path, *_ = session
    time_event = events(path)[1]
    assert time_event["kind"] == "time" and time_event["hours"] == 20
    assert all(c["make_due"] is False for c in time_event["checks"])                 # never forced
    sent = [c for c in time_event["checks"] if c["proactive_sent"]]
    assert sent and all(c["due_reason"] == "due" for c in sent)
    chain = sent[0]["proactive_chain"]
    assert chain["scheduler_allowed"] and chain["model_decided_to_send"] and chain["sent"] == [PROACTIVE_TEXT]
    assert time_event["idle_checks"]                                                  # the closed gates are recorded too
    assert datetime.fromisoformat(time_event["to"]) - datetime.fromisoformat(time_event["from"]) >= timedelta(hours=20)


def test_the_answer_after_a_restart_follows_the_proactive_message(session):
    path, *_ = session
    evs = events(path)
    later = [e for e in evs if e["kind"] == "user" and e["launch"] == 2][0]
    assert later["after_proactive"]["n"] == 2 and later["at"] >= evs[1]["to"]          # the clock went on, not back
    chat = next(r["request"] for r in fc.read_jsonl(path / fc.REQUESTS) if r["n"] == later["n"] and r["agent"] == "ChatAgent")
    assert {"role": "assistant", "content": PROACTIVE_TEXT} in chat
    assert later["n"] == evs[-1]["n"] and len({e["n"] for e in evs}) == len(evs)


def test_feedback_is_tied_to_rounds(session):
    path, first, second, third = session
    marks = fc.read_jsonl(path / fc.FEEDBACK)
    assert [(m["n"], m["tag"], m["during"]) for m in marks] == [(1, "good", "chat"), (2, "missed", "chat"), (1, "flat", "review")]
    assert marks[1]["note"] == "等了好久" and marks[1]["before"]["user"].startswith("我家团子")
    assert marks[2]["event"]["violet"] == ["嗯嗯，我在呢～"]
    assert "没有第 9 轮" in third[1]
    md = (path / fc.FEEDBACK_MD).read_text(encoding="utf-8")
    assert "## 该主动却没有主动（1）" in md and "想要更黏一点" in md
    transcript = (path / fc.TRANSCRIPT).read_text(encoding="utf-8")
    assert "--- 第 2 次启动 ---" in transcript and f"**Violet（主动）**：{PROACTIVE_TEXT}" in transcript
    assert "（回复第 2 条主动消息）" in transcript and "🏷 该主动却没有主动：等了好久" in transcript


def test_a_session_keeps_its_mode_and_model(session):
    path, *_ = session
    code, out = launch(["chat", "--session", str(path)], [])
    assert code == 2 and "不能换成模拟模型" in out
    code, out = launch(["chat", "--session", str(path), "--real", "--api-url", URL, "--model", "other"], [])
    assert code == 2 and "不能换成 other" in out
    assert len(fc.read_meta(path)["launches"]) == 2


# ============================================================================== in process (nothing opened)

def args(path, **kw):
    base = dict(session=str(path), real=False, api_url=None, model=None, config=None, seed_episode=None,
                episodes_file=None, start=None, quiet_hours="config", max_calls=10, max_tokens=1000, max_calls_per_round=5)
    return argparse.Namespace(**{**base, **kw})


def test_one_process_per_session(tmp_path):
    lock = fc.SessionLock(tmp_path / fc.LOCK).acquire()
    try:
        with pytest.raises(fc.SessionError, match="另一个窗口"):
            fc.SessionLock(tmp_path / fc.LOCK).acquire()
    finally:
        lock.release()
    fc.SessionLock(tmp_path / fc.LOCK).acquire().release()


def test_refusals_before_anything_is_opened(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "violet-test.db").write_text("x", encoding="utf-8")
    with pytest.raises(fc.SessionError, match="不是自由聊天会话"):
        fc.open_chat(args(other))
    with pytest.raises(fc.SessionError, match="--model"):
        fc.open_chat(args(tmp_path / "new", real=True, api_url=URL))
    with pytest.raises(fc.SessionError, match="violet"):
        fc.session_dir(str(cd.PROJECT / "data" / "x"))
    assert fc.session_dir("abc") == (fc.SESSION_ROOT / "abc").resolve()


@pytest.mark.parametrize("text,last,expected", [
    ("好", 3, ("good", 3, "")), ("机械 #5 又在问今天怎么样", 9, ("flat", 5, "又在问今天怎么样")),
    ("矛盾 2", 9, ("conflict", 2, "")), ("该主动 等了很久都没找我", 4, ("missed", 4, "等了很久都没找我")),
    ("note 3点那句挺好", 7, ("note", 7, "3点那句挺好")), ("flat", 1, ("flat", 1, "")),
])
def test_marks(text, last, expected):
    assert fc.parse_mark(text, last) == expected


@pytest.mark.parametrize("text,last", [("", 1), ("开心", 1), ("好", None)])
def test_bad_marks(text, last):
    with pytest.raises(ValueError):
        fc.parse_mark(text, last)


def test_durations():
    assert fc.parse_duration("3") == timedelta(hours=3) and fc.parse_duration("1.5") == timedelta(minutes=90)
    assert fc.parse_duration("2h30m") == timedelta(hours=2, minutes=30) and fc.parse_duration("1d") == timedelta(days=1)
    for bad in ("0", "-2", "soon"):
        with pytest.raises(ValueError):
            fc.parse_duration(bad)


def test_a_cut_line_does_not_hide_the_rest(tmp_path):
    f = tmp_path / "e.jsonl"
    f.write_text('{"n": 1}\n{"n": 2\n{"n": 3}\n', encoding="utf-8")
    assert fc.read_jsonl(f) == [{"n": 1}, {"n": 3}]
    assert fc.read_jsonl(tmp_path / "missing.jsonl") == []
