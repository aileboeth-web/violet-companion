"""Phase 8.9.1: tools/chat_dialogue.py no longer stops an episode on a 502/503 that Violet's own retry recovered
(the 8.9.1 real run local/p891/real-20261020-054202 stopped all three episodes that way), and still stops on
everything else. Offline: the fake SDK replaces openai.OpenAI in each child process and raises the SDK's own
InternalServerError, so Violet's real retry code (ai/agents/base_agent.py, real 3 s / 6 s delays) handles it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import pytest

from tools import chat_dialogue as cd

URL = "http://127.0.0.1:18995/v1"
KEY = "fake-recovery-p891-credential-0000"

# ============================================================================== the classifier


def call(no, agent="IntentAgent", run=1, ok=False, status=503, model="m", attempt=None):
    return {"call_no": no, "agent": agent, "run_id": run, "ok": ok, "status_code": None if ok else status,
            "error_type": None if ok else ("InternalServerError" if status and status >= 500 else "APIError"),
            "model": model, "step": attempt or no, "latency_ms": 1000 * no}


def runs(*items):
    return [{"run_id": r, "agent": a, "ok": ok} for r, a, ok in items]


def test_503_then_success_is_recovered():
    rec, unrec = cd.classify_failed_calls([call(1), call(2, ok=True)], runs((1, "IntentAgent", True)))
    assert unrec == [] and rec == [{"call_no": 1, "agent": "IntentAgent", "run_id": 1, "attempt": 1, "status_code": 503,
                                    "error_type": "InternalServerError", "latency_ms": 1000, "recovered_by_call": 2}]


def test_502_503_then_success_is_recovered_twice():
    calls = [call(1, "ChatAgent", status=502), call(2, "ChatAgent"), call(3, "ChatAgent", ok=True)]
    rec, unrec = cd.classify_failed_calls(calls, runs((1, "ChatAgent", True)))
    assert unrec == [] and [(c["status_code"], c["recovered_by_call"]) for c in rec] == [(502, 3), (503, 3)]


@pytest.mark.parametrize("calls,run_rows,why", [
    ([call(1), call(2), call(3)], runs((1, "IntentAgent", False)), "retries exhausted: the run failed"),
    ([call(1, status=500), call(2, ok=True)], runs((1, "IntentAgent", True)), "500 is not a recoverable status"),
    ([call(1, status=429), call(2, ok=True)], runs((1, "IntentAgent", True)), "429 is not a recoverable status"),
    ([call(1, status=None), call(2, ok=True)], runs((1, "IntentAgent", True)), "a timeout has no status"),
    ([call(1, "MemoryAgent"), call(2, "MemoryAgent", ok=True)], runs((1, "MemoryAgent", True)), "another agent"),
    ([call(1), call(2, ok=True, model="fallback")], runs((1, "IntentAgent", True)), "rescued by a fallback model"),
    ([call(1, ok=True), call(2)], runs((1, "IntentAgent", True)), "no LATER success"),
    ([call(1), call(2, ok=True, run=2)], runs((1, "IntentAgent", True), (2, "IntentAgent", True)), "success in another run"),
    ([call(1, run=None), call(2, ok=True, run=None)], [], "no run recorded"),
    ([call(1), call(2, ok=True)], runs((1, "ChatAgent", True)), "the run belongs to another agent"),
    ([call(1), call(2, ok=True)], runs((1, "IntentAgent", None)), "run never finished"),
])
def test_everything_else_stays_a_failure(calls, run_rows, why):
    rec, unrec = cd.classify_failed_calls(calls, run_rows)
    assert rec == [] and [c["call_no"] for c in unrec] == [c["call_no"] for c in calls if not c["ok"]], why


# ============================================================================== through the real path, fake SDK

BOOT = r'''
import json, os, sys, uuid, time
from unittest.mock import patch
import httpx
from openai import InternalServerError
from openai.types.chat import ChatCompletion
from tools import chat_dialogue as cd
from tools.harness import _AGENT
FAULTS = json.loads(os.environ.get("P891_FAULTS") or "[]")
SEEN = {}
Mock = cd._mock_class()
class FakeSDK(Mock):
    def __init__(self, **kwargs):
        assert kwargs["api_key"] == "%s" and kwargs["base_url"] == "%s" and kwargs.get("max_retries") == 0
        super().__init__()
    def _create(self, **kwargs):
        agent, msgs = _AGENT.get(), kwargs.get("messages") or []
        last = str(msgs[-1].get("content") or "") if msgs else ""
        whole = "\n".join(str(m.get("content") or "") for m in msgs)
        reply = None
        for i, fault in enumerate(FAULTS):
            if fault["agent"] == agent and fault["text"] in (whole if fault.get("anywhere") else last):
                n = SEEN[i] = SEEN.get(i, 0) + 1
                codes = fault.get("codes") or []
                if n <= len(codes):
                    response = httpx.Response(codes[n - 1], request=httpx.Request("POST", "%s/chat/completions"))
                    raise InternalServerError(f"fake upstream {codes[n - 1]}", response=response, body=None)
                if fault.get("empty"):
                    reply = ""
                if fault.get("say"):                 # phase 8.9.5: a fixed reply (e.g. one the guard must drop)
                    reply = fault["say"]
        if reply is None:
            reply = super()._create(**kwargs).choices[0].message.content
        return ChatCompletion.model_validate({
            "id": f"fake-{uuid.uuid4().hex[:8]}", "object": "chat.completion", "created": int(time.time()),
            "model": str(kwargs.get("model")), "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": reply}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}})
with patch("openai.OpenAI", FakeSDK):
    raise SystemExit(cd.main(["_child", *json.loads(sys.argv[1])]))
''' % (KEY, URL, URL)


def fake_run(tmp_path, monkeypatch, episodes, faults, **kw):
    monkeypatch.setenv("VIOLET_TEST_ALLOW_REAL_API", "1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.delenv("VIOLET_TEST_API_KEY", raising=False)

    def launch(argv, env, log):
        done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOT, json.dumps(argv)], cwd=cd.PROJECT,
                              env={**env, "P891_FAULTS": json.dumps(faults, ensure_ascii=False)}, capture_output=True,
                              text=True, encoding="utf-8", timeout=900)
        log.write_text(done.stdout + done.stderr, encoding="utf-8")
        return done.returncode
    out = tmp_path / f"run-{uuid.uuid4().hex[:8]}"
    summary = cd.run(out, label="p891-recovery", episode_ids=episodes, real=True, api_url=URL, memory="scripted",
                     launch=launch, input_fn=lambda p: "yes", key_prompt=lambda p: KEY,
                     environment="fake_sdk_offline", **{"max_calls": 80, "max_tokens": 600_000, **kw})
    results = {e: json.loads((out / "results" / f"{e}.json").read_text(encoding="utf-8")) for e in episodes}
    return out, summary, results


def step(result, n):
    return next(s for s in result["steps"] if s["n"] == n)


def test_recovered_steps_go_on_and_an_exhausted_intent_degrades(tmp_path, monkeypatch):
    faults = [{"text": "还行吧 就是有点长", "agent": "IntentAgent", "codes": [503]},          # 503 -> ok
              {"text": "你今天干嘛了", "agent": "ChatAgent", "codes": [502, 503]},           # 502 -> 503 -> ok
              {"text": "我去洗澡了", "agent": "IntentAgent", "codes": [503, 503, 503]}]      # retries exhausted
    out, summary, results = fake_run(tmp_path, monkeypatch, ["E1"], faults)
    e1 = results["E1"]
    # phase 8.9.5: IntentAgent gave up, ai.chat fell back to a plain chat turn (by design) and Violet really replied:
    # "degraded" -- recorded, the episode goes on, and it is not "complete" (never rated as a clean run)
    assert e1["state"] == "degraded" and summary["state"] == "degraded" and e1["degraded_steps"] == [6]
    assert [s["status"] for s in e1["steps"][:6]] == ["ok", "recovered", "ok", "recovered", "ok", "degraded"]
    assert e1["not_run"] == [] and all(s["status"] == "ok" for s in e1["steps"][6:])

    two, four, six = step(e1, 2), step(e1, 4), step(e1, 6)
    assert two["violet"] and two["reply"] == "sent" and two["problems"] == []
    assert [(c["agent"], c["attempt"], c["status_code"]) for c in two["recovered_calls"]] == [("IntentAgent", 1, 503)]
    assert two["call_errors"] == ["InternalServerError (HTTP 503)"]                  # the failure is still recorded
    assert [(c["agent"], c["attempt"], c["status_code"]) for c in four["recovered_calls"]] == [
        ("ChatAgent", 1, 502), ("ChatAgent", 2, 503)]
    assert four["violet"] and four["problems"] == []
    assert six["recovered_calls"] == [] and six["problems"] == [] and six["violet"]
    assert [(c["agent"], c["status_code"]) for c in six["degraded_calls"]] == [("IntentAgent", 503)] * 3
    assert six["call_errors"] == ["InternalServerError (HTTP 503)"] * 3                # evidence kept

    # every call is kept: failed ones with status, latency and no tokens; budget counts them as calls
    calls = e1["calls"]
    failed = [c for c in calls if not c["ok"]]
    assert [c["status_code"] for c in failed] == [503, 502, 503, 503, 503, 503]
    assert all(c["latency_ms"] is not None and c["total_tokens"] is None and c["usage_present"] is False for c in failed)
    assert e1["budget"]["calls"] == len(calls) and e1["budget"]["used_tokens"] == sum(c["total_tokens"] or 0 for c in calls)
    assert summary["recovered_steps"] == {"E1": [2, 4]} and summary["failed_calls"] == {"E1": 6}
    assert summary["degraded_steps"] == {"E1": [6]}
    assert summary["used_calls"] == len(calls) and summary["tool"] == "p87.v3"

    transcript = (out / "transcript.md").read_text(encoding="utf-8")
    assert "ℹ 第 2 步模型调用出错后由 Violet 的重试恢复：IntentAgent 第 1 次 HTTP 503" in transcript
    assert "ChatAgent 第 1 次 HTTP 502" in transcript and "ChatAgent 第 2 次 HTTP 503" in transcript
    assert "⚠ 第 6 步降级：IntentAgent 重试用完" in transcript and "失败的调用 6 次" in transcript
    assert not [p for p in out.rglob("*") if p.is_file() and KEY.encode() in p.read_bytes()]
    blind = cd.blind([out], tmp_path / "blind", seed=1)                             # degraded: not a clean run
    assert "E1" not in blind["items"]


def test_chat_retries_exhausted_stop_the_episode(tmp_path, monkeypatch):
    out, summary, results = fake_run(tmp_path, monkeypatch, ["E5"],
                                     [{"text": "好无聊", "agent": "ChatAgent", "codes": [503, 503, 503]}])
    e5 = results["E5"]
    first = step(e5, 1)
    assert e5["state"] == "failed_model_call" and first["status"] == "failed" and first["recovered_calls"] == []
    assert [x["n"] for x in e5["not_run"]] == list(range(2, 11)) and summary["recovered_steps"] == {"E5": []}
    blind = cd.blind([out], tmp_path / "blind", seed=1)                              # never offered for rating
    assert "E5" not in blind["items"] and blind["skipped"]["E5"] == "not completed by every run"


def test_a_recovered_episode_completes_and_is_rated_without_the_note(tmp_path, monkeypatch):
    out, summary, results = fake_run(tmp_path, monkeypatch, ["E5"], [{"text": "好无聊", "agent": "IntentAgent", "codes": [502]}])
    e5 = results["E5"]
    assert e5["state"] == "complete" and summary["state"] == "complete" and e5["recovered_steps"] == [1]
    assert all(s["status"] == "ok" for s in e5["steps"][1:]) and e5["not_run"] == []
    assert "ℹ 第 1 步" in (out / "transcript.md").read_text(encoding="utf-8")
    blind = cd.blind([out], tmp_path / "blind", seed=1)
    sheet = (tmp_path / "blind" / "sheet.md").read_text(encoding="utf-8")
    assert "E5" in blind["items"] and "ℹ" not in sheet and "HTTP" not in sheet


def test_a_retry_the_budget_refuses_is_a_budget_stop_not_a_recovery(tmp_path, monkeypatch):
    out, summary, results = fake_run(tmp_path, monkeypatch, ["E1"],
                                     [{"text": "今天去看了场电影", "agent": "IntentAgent", "codes": [503]}], max_calls=1)
    e1 = results["E1"]
    first = step(e1, 1)
    assert e1["state"] == "stopped_budget" and first["status"] == "failed" and first["recovered_calls"] == []
    assert any(p.startswith("budget refused") for p in first["problems"])
    assert [c["status_code"] for c in e1["calls"]] == [503]


def test_a_recovered_call_without_a_reply_is_not_consistent(tmp_path, monkeypatch):
    # Intent recovers, then Chat answers nothing: no reply and no decided silence -- the step must not pass
    faults = [{"text": "今天去看了场电影", "agent": "IntentAgent", "codes": [503]},
              {"text": "今天去看了场电影", "agent": "ChatAgent", "empty": True}]
    out, summary, results = fake_run(tmp_path, monkeypatch, ["E1"], faults)
    first = step(results["E1"], 1)
    assert first["reply"] == "no_reply" and first["status"] == "failed" and results["E1"]["state"] == "failed_step"
    assert "no reply after a recovered model error" in first["problems"] and len(first["recovered_calls"]) == 1


def test_the_canned_fallback_is_a_failure_even_without_failed_calls(tmp_path, monkeypatch):
    # phase 8.9.5: every line contradicts the records (rewrite says the same) -> ai.chat marks chat_failed and the
    # runtime sends its canned fallback; no model call failed, the user still did not get a real reply
    out, summary, results = fake_run(tmp_path, monkeypatch, ["E1"],
                                     [{"text": "今天去看了场电影", "agent": "ChatAgent", "say": "我昨天也去爬了泰山"}])
    first = step(results["E1"], 1)
    assert first["violet"] == [cd.FALLBACK_REPLY] and first["call_errors"] == []
    assert first["status"] == "failed" and "the canned fallback reply was sent (ChatAgent failed)" in first["problems"]
    assert results["E1"]["state"] == "failed_step"


def test_scheduler_steps_recover_like_user_steps(tmp_path, monkeypatch):
    # E3 step 6 is a scheduler check that sends a due reminder through ChatAgent. Phase 8.9.5: a 503 that the same
    # run recovered no longer stops the episode (8.9.4 real A1 stopped after its message had been sent)
    out, summary, results = fake_run(tmp_path, monkeypatch, ["E3"],
                                     [{"text": "要提醒的事", "agent": "ChatAgent", "codes": [503], "anywhere": True}])
    e3 = results["E3"]
    tick = step(e3, 6)
    assert tick["kind"] == "scheduler" and tick["status"] == "recovered" and tick["reminder_sent"]
    assert [(c["agent"], c["status_code"]) for c in tick["recovered_calls"]] == [("ChatAgent", 503)]
    assert e3["state"] == "complete" and e3["not_run"] == []
    assert [c["status_code"] for c in tick["calls"] if not c["ok"]] == [503]


def test_scheduler_steps_still_fail_when_the_run_fails(tmp_path, monkeypatch):
    out, summary, results = fake_run(tmp_path, monkeypatch, ["E3"],
                                     [{"text": "要提醒的事", "agent": "ChatAgent", "codes": [503, 503, 503], "anywhere": True}])
    e3 = results["E3"]
    tick = step(e3, 6)
    assert tick["status"] == "failed" and e3["state"] == "failed_model_call"
    assert tick["problems"][:3] == ["InternalServerError (HTTP 503)"] * 3              # named, not "未知原因"
