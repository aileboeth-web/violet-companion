"""Phase 8.9.4: the proactive chain on the p894 scenarios (tests/samples/chat_dialogue/episodes.p894.v1.json), offline.

The fake SDK replaces openai.OpenAI in each child process; Violet's real scheduler, pipeline, guards and outbox run.
The fake ProactiveAgent acts only when event_context offers Violet a reason (violet_motives) and takes the first one,
so these tests check the plumbing -- the scheduler gate, the decision reaching ChatAgent, the message being sent
and logged, the user's reply being answered with it in view -- not the judgement of a real model."""
from __future__ import annotations

import json
import subprocess
import sys
import uuid

import pytest

from tools import chat_dialogue as cd

URL = "http://127.0.0.1:18997/v1"
KEY = "fake-proactive-p894-credential-0000"
EPISODES = cd.PROJECT / "tests/samples/chat_dialogue/episodes.p894.v1.json"
# phase 8.9.10: the share line claims nothing of its own -- "刚又读了几章书" was a fabrication in A2 (whose recorded life is
# piano music), caught once the guard also reads claims without "我"
TEXTS = {"share": "突然有件小事想跟你分享", "ask_opinion": "周末想去书店挑本新书，你觉得我挑什么好呀？",
         "miss": "有点想你了，今天忙不忙呀？"}

BOOT = r'''
import ast, json, sys, uuid, time
from unittest.mock import patch
from openai.types.chat import ChatCompletion
from tools import chat_dialogue as cd
from tools.harness import _AGENT
TEXTS = json.loads(%r)
Mock = cd._mock_class()
class FakeSDK(Mock):
    def __init__(self, **kwargs):
        assert kwargs["api_key"] == "%s"
        super().__init__()
    def _create(self, **kwargs):
        agent, msgs = _AGENT.get(), kwargs.get("messages") or []
        last = str(msgs[-1].get("content") or "") if msgs else ""
        reply = None
        if agent == "ProactiveAgent":
            ctx = json.loads(last)
            motives = ctx.get("violet_motives") or []
            if motives:
                kind = motives[0]["type"]
                events = (ctx.get("conversation_state") or {}).get("allowed_life_events") or []
                reply = json.dumps({"should_act": True, "action_type": "chat", "reason": "motive:" + kind, "topic_key": kind,
                                    "mentioned_memory_ids": [], "mentioned_web_finding_ids": [],
                                    "mentioned_life_event_ids": [events[0]["id"]] if kind == "share" and events else [],
                                    "chat_brief": {"tone": "轻松", "topic": kind, "time_bucket": "other", "must_not_say": []},
                                    "task": {"name": None, "description": None, "task_info": {}, "constraints": {}}},
                                   ensure_ascii=False)
            else:
                reply = json.dumps({"should_act": False, "action_type": "none", "reason": "没有想找对方的理由",
                                    "topic_key": "none", "mentioned_memory_ids": [], "mentioned_life_event_ids": [],
                                    "mentioned_web_finding_ids": [], "chat_brief": {}, "task": {"name": None}},
                                   ensure_ascii=False)
        elif agent == "ChatAgent" and last.startswith("这是一次 Violet 主动触达"):
            decision = next((m.get("content") for m in msgs if str(m.get("content") or "").startswith("主动触达决策")), "")
            kind = next((k for k in TEXTS if "'topic_key': '" + k + "'" in decision), None)
            reply = TEXTS.get(kind, "[NO_REPLY]")
        if reply is None:
            reply = super()._create(**kwargs).choices[0].message.content
        return ChatCompletion.model_validate({
            "id": f"fake-{uuid.uuid4().hex[:8]}", "object": "chat.completion", "created": int(time.time()),
            "model": str(kwargs.get("model")), "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": reply}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}})
with patch("openai.OpenAI", FakeSDK):
    raise SystemExit(cd.main(["_child", *json.loads(sys.argv[1])]))
''' % (json.dumps(TEXTS, ensure_ascii=False), KEY)


@pytest.fixture(scope="module")
def run_p894(tmp_path_factory):
    import os
    os.environ["VIOLET_TEST_ALLOW_REAL_API"] = "1"
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ.pop("VIOLET_TEST_API_KEY", None)

    def launch(argv, env, log):
        done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOT, json.dumps(argv)], cwd=cd.PROJECT, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=1800)
        log.write_text(done.stdout + done.stderr, encoding="utf-8")
        return done.returncode
    out = tmp_path_factory.mktemp("p894") / f"run-{uuid.uuid4().hex[:8]}"
    try:
        summary = cd.run(out, label="p894-chain", episodes_path=EPISODES, real=True, api_url=URL, memory="scripted",
                         launch=launch, input_fn=lambda p: "yes", key_prompt=lambda p: KEY,
                         environment="fake_sdk_offline", max_calls=300, max_tokens=2_000_000)
    finally:
        os.environ.pop("VIOLET_TEST_ALLOW_REAL_API", None)
    episodes = json.loads((out / "debug.json").read_text(encoding="utf-8"))["episodes"]
    return out, summary, episodes


def step(episodes, eid, n):
    return next(s for s in episodes[eid]["steps"] if s["n"] == n)


def decision_context(s):
    req = next(r["request"] for r in s["requests"] if r["agent"] == "ProactiveAgent")
    return json.loads(req[-1]["content"]), req


def test_all_scenarios_complete(run_p894):
    out, summary, _ = run_p894
    assert summary["state"] == "complete", summary["episode_states"]
    assert not [p for p in out.rglob("*") if p.is_file() and KEY.encode() in p.read_bytes()]


def test_a1_close_next_day_message_is_sent_for_a_reason(run_p894):
    _, _, eps = run_p894
    tick = step(eps, "A1", 5)
    chain = tick["proactive_chain"]
    assert chain["scheduler_allowed"] and chain["model_called"] and chain["model_decided_to_send"]
    assert chain["message_generated"] and chain["sent"] and chain["outcome"] == "sent"
    ctx, req = decision_context(tick)
    assert ctx["relationship_stage"] == "close"
    assert {m["type"] for m in ctx["violet_motives"]} == {"share", "miss", "ask_opinion"}
    assert any("已经很亲近了" in (m["content"] or "") for m in req if m["role"] == "system")
    assert chain["sent"] == [TEXTS[chain["topic"]]]


def test_a1_the_users_reply_is_answered_with_the_message_in_view(run_p894):
    _, _, eps = run_p894
    sent = step(eps, "A1", 5)["proactive_chain"]["sent"][0]
    reply = step(eps, "A1", 6)
    chat = next(r["request"] for r in reply["requests"] if r["agent"] == "ChatAgent")
    assert {"role": "assistant", "content": sent} in chat                 # her own message, as a real turn
    assert reply["violet"] and reply["status"] == "ok"


def test_raw_model_output_is_kept_next_to_what_was_sent(run_p894):
    # phase 8.9.5: so a strange sent line can be traced to the model or to Violet's own processing
    _, _, eps = run_p894
    tick = step(eps, "A1", 5)
    raw = [r for r in tick["responses"] if r["agent"] == "ChatAgent"]
    assert raw and raw[-1]["content"] == "\n".join(tick["proactive_chain"]["sent"])
    assert any(r["agent"] == "ProactiveAgent" and '"should_act": true' in r["content"] for r in tick["responses"])
    reply = step(eps, "A1", 6)
    assert [r["agent"] for r in reply["responses"]][:2] == ["IntentAgent", "ChatAgent"]


def test_a1_right_after_the_user_spoke_the_scheduler_waits(run_p894):
    _, _, eps = run_p894
    chain = step(eps, "A1", 10)["proactive_chain"]
    assert not chain["scheduler_allowed"] and chain["scheduler_reason"] == "too soon after user message"
    assert not chain["model_called"] and chain["sent"] == []


def test_a2_dont_contact_me_is_respected_then_lifted(run_p894):
    _, _, eps = run_p894
    blocked = step(eps, "A2", 3)["proactive_chain"]
    assert not blocked["scheduler_allowed"] and "asked Violet not to start" in blocked["scheduler_reason"]
    allowed = step(eps, "A2", 6)["proactive_chain"]
    assert allowed["scheduler_allowed"] and allowed["sent"]


def test_a2_no_answer_means_no_missing_and_then_silence(run_p894):
    _, _, eps = run_p894
    after = step(eps, "A2", 7)                                   # one unanswered message (cold streak 1)
    chain = after["proactive_chain"]
    assert chain["scheduler_allowed"] and chain["model_called"]
    ctx, _ = decision_context(after)
    assert "miss" not in [m["type"] for m in ctx["violet_motives"]]  # her other news may still come, missing may not
    assert chain["sent"] and chain["topic"] == "share"
    last = step(eps, "A2", 8)["proactive_chain"]                 # two unanswered: wait for the user
    assert not last["scheduler_allowed"] and "re-engage" in last["scheduler_reason"] and not last["model_called"]


def test_a2_the_user_coming_back_resets_the_wait(run_p894):
    _, _, eps = run_p894
    back = step(eps, "A2", 9)
    assert back["violet"] and back["status"] == "ok"
    chat = next(r["request"] for r in back["requests"] if r["agent"] == "ChatAgent")
    sent = [t for n in (6, 7) for t in step(eps, "A2", n)["proactive_chain"]["sent"]]
    assert all({"role": "assistant", "content": t} in chat for t in sent)   # both unanswered messages in view


def test_a3_after_a_month_no_missing_as_a_reason(run_p894):
    _, _, eps = run_p894
    tick = step(eps, "A3", 1)
    ctx, req = decision_context(tick)
    assert ctx["relationship_stage"] == "warming"
    assert "miss" not in [m["type"] for m in ctx["violet_motives"]]
    assert any("之前挺熟，但有好一阵子没聊了" in (m["content"] or "") for m in req if m["role"] == "system")


def chain(**kw):
    base = {"scheduler_allowed": True, "scheduler_reason": "due", "model_called": True, "model_decided_to_send": True,
            "decision_reason": "想分享", "topic": "share", "message_generated": True, "sent": ["…"], "outcome": "sent",
            "outcome_reason": None}
    return {**base, **kw}


@pytest.mark.parametrize("kw,expected", [
    (dict(scheduler_allowed=False, scheduler_reason="quiet hours", model_called=False), "调度不允许（quiet hours）"),
    (dict(model_decided_to_send=False, decision_reason="没什么想说的", message_generated=False, sent=[]),
     "调度允许；模型决定不发（没什么想说的）"),
    (dict(sent=[], outcome="skipped", outcome_reason="policy: proactive message contradicted recorded facts"),
     "模型决定发（话题 share；想分享）；生成了但没有发出（policy: proactive message contradicted recorded facts）"),
    (dict(), "模型决定发（话题 share；想分享）；已发出"),
])
def test_each_link_is_named_on_its_own(kw, expected):
    assert expected in cd.chain_md(chain(**kw))


def test_transcript_keeps_the_links_apart(run_p894):
    out, _, _ = run_p894
    text = (out / "transcript.md").read_text(encoding="utf-8")
    assert "主动联系：调度允许；模型决定发（话题" in text and "已发出" in text
    assert "主动联系：调度不允许（too soon after user message）" in text
    assert "主动联系：调度不允许（the user asked Violet not to start conversations）" in text
    blind = cd.blind([out], out.parent / "blind", seed=1)
    sheet = (out.parent / "blind" / "sheet.md").read_text(encoding="utf-8")
    assert blind["items"] and "主动联系：" not in sheet                    # raters see the conversation only
