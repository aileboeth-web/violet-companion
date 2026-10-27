"""Real-path integration using a fake SDK only; all evidence remains local and synthetic."""
import json
import os
import subprocess
import sys
import uuid

import pytest

from tools.synthetic_rounds import PROJECT, build_parser, validate_endpoint


# Runs in a fresh interpreter, before any production DB imports. Never constructs a network SDK.
BOOTSTRAP = r'''
import json, os, sys
from pathlib import Path
from unittest.mock import patch
from tools import harness as h
from tools.synthetic_rounds import run_specialized, build_parser
options = json.loads(sys.argv[1])
key = "fake-b113-credential-never-persist-123456789"
os.environ.pop("VIOLET_TEST_API_KEY", None)
os.environ["VIOLET_TEST_ALLOW_REAL_API"] = options.get("allow", "1")
os.environ["VIOLET_TEST_API_URL"] = "http://127.0.0.1:18991/v1"
seen, confirms = [], []
class FakeSDK(h.MockLLM):
    def __init__(self, **kwargs):
        assert kwargs["api_key"] == key
        assert kwargs["base_url"] == "http://127.0.0.1:18991/v1"
        assert kwargs["max_retries"] == 0
        super().__init__()
    def _create(self, **kwargs):
        agent = h._AGENT.get()
        reminder = any(m["role"] == "system" and "这是用户之前明确要求的提醒" in str(m.get("content")) for m in kwargs["messages"])
        seen.append({"agent": agent, "reminder": reminder})
        if reminder and options.get("failure"):
            raise RuntimeError("provider echoed " + key)
        if agent == "ChatAgent":
            user = next((m.get("content") for m in reversed(kwargs["messages"]) if m["role"] == "user"), "")
            text = "该交作业了。" if reminder else "早。" if user == "早" else "好，那先不聊了。"
            if reminder and options.get("bad_reminder"): text = "今天阳光不错。"
            if options.get("echo"): text += key
            response = self._completion(text, kwargs["model"])
        else:
            response = super()._create(**kwargs)
        if agent == "MemoryAgent" and options.get("preference_attempt") and not any(m["role"] == "tool" for m in kwargs["messages"]):
            from openai.types.chat import ChatCompletion
            response = ChatCompletion.model_validate({**response.model_dump(), "choices": [{"index": 0,
                "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "pref-attempt", "type": "function", "function": {
                    "name": "set_interaction_preference", "arguments": json.dumps({"key": "proactive_chat",
                    "value": "no", "evidence": "算了不想聊了"})}}]}}]})
        response.usage.prompt_tokens = 100
        response.usage.completion_tokens = 20
        response.usage.total_tokens = options.get("usage", 120)
        return response
original = h.write_report
def writer(name, report, recorder, directory):
    report["verification_environment"] = "fake_sdk_offline"
    report["fake_requests"] = seen
    report["confirmations"] = len(confirms)
    return original(name, report, recorder, directory)
def confirm(prompt):
    confirms.append(True)
    return options.get("answer", "yes")
args = build_parser().parse_args(options["argv"])
with patch("openai.OpenAI", FakeSDK), patch.object(h, "write_report", writer):
    run_specialized(args.workdir, args=args, input_fn=confirm,
                    key_prompt=lambda prompt: key if options.get("key", True) else "")
'''


def run_case(*flags, **options):
    path = PROJECT.parent / "local/b113/test-cases" / uuid.uuid4().hex
    argv = ["--workdir", str(path), *flags]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", BOOTSTRAP,
                             json.dumps({"argv": argv, **options})], cwd=PROJECT,
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, (result.stdout, result.stderr)
    report = json.loads((path / "specialized_report.json").read_text(encoding="utf-8"))
    # Check every artifact, including SQLite and redirected logs, not only the JSON writer.
    secret = b"fake-b113-credential-never-persist-123456789"
    assert secret not in (result.stdout + result.stderr).encode()
    assert "fak…89" not in result.stdout
    assert all(secret not in f.read_bytes() for f in path.rglob("*") if f.is_file())
    return path, report


@pytest.fixture(scope="module")
def fake_real():
    return run_case("--real")


def test_default_stays_mock_even_when_gate_environment_allows_real():
    _, report = run_case()
    assert report["status"] == "通过" and not report["real"]
    assert not report["fake_requests"] and report["confirmations"] == 0
    assert "模拟" in report["notice"]
    assert report["network_guard"]["allowed_endpoints"] == []


@pytest.mark.parametrize("options", [{"allow": "0"}, {"answer": "no"}, {"key": False}])
def test_unauthorized_real_path_never_constructs_or_calls_sdk(options):
    _, report = run_case("--real", **options)
    assert report["status"] == "未执行"
    assert report["execution_state"] == "authorization_refused"
    assert not report["fake_requests"] and report["total_calls"] == 0


def test_real_path_uses_confirmation_and_actual_memory_agent(fake_real):
    _, report = fake_real
    assert report["status"] == "通过" and report["real"]
    assert report["confirmations"] == 1
    assert report["checks"]["no_long_term_preference_write"]
    r27 = report["rounds"][0]
    assert r27["agent_call_review"]["by_agent"] == {"IntentAgent": 1, "ChatAgent": 1, "MemoryAgent": 1}
    assert not r27["preference_attempts"]
    assert r27["user"] == "算了不想聊了" and report["manual_scores"]["27"] is None


def test_real_memory_tool_attempt_cannot_persist_temporary_ending():
    _, report = run_case("--real", preference_attempt=True)
    assert report["status"] == "通过" and report["checks"]["no_long_term_preference_write"]
    memory_calls = [c for c in report["rounds"][0]["calls"] if c["agent"] == "MemoryAgent"]
    assert "set_interaction_preference" in memory_calls[0]["tool_calls"]
    assert "session_ending_not_durable_preference" in json.dumps(memory_calls, ensure_ascii=False)


def test_scheduler_call_is_attributed_and_all_totals_reconcile(fake_real):
    _, report = fake_real
    assert report["total_calls"] == len(report["fake_requests"]) == 7
    assert report["accounting"]["by_round"] == {"27": 3, "28": 4}
    stages = {s["id"]: s for s in report["stages"]}
    [reminder] = stages["round28.reminder_due"]["calls"]
    assert reminder["round"] == 28 and reminder["agent"] == "ChatAgent"
    assert len(report["rounds"][1]["calls"]) == 4
    assert sum(report["accounting"]["by_stage"].values()) == report["call_summary"]["calls"] == 7
    assert report["checks"]["calls_fully_attributed"]


def test_usage_and_reserves_come_from_existing_budget(fake_real):
    _, report = fake_real
    assert report["budget"]["used_tokens"] == 7 * 120
    assert all(c["prompt_tokens"] == 100 and c["completion_tokens"] == 20 and c["total_tokens"] == 120 for c in report["calls"])
    decisions = report["budget_reservations"]
    assert len(decisions) == 7 and all(d["approved"] for d in decisions)
    assert decisions[0]["reserved_tokens"] == 16000
    assert decisions[1]["reserved_tokens"] == 132
    assert report["network_guard"]["allowed_endpoints"] == ["127.0.0.1:18991"]
    assert report["network_guard"]["active_during_run"] and report["network_guard"]["allowed_connections"] == 0


def test_real_path_reminder_delivery_quiet_dedup_and_delay(fake_real):
    _, report = fake_real
    assert all(report["checks"].values())
    row = report["rounds"][1]
    assert not row["reminder_used_fallback"]
    reminder = row["reminder"]["reminders"][0]
    assert reminder["delivery"] == reminder["event_status"] == "sent"
    assert reminder["commitment_id"] and reminder["proactive_event_id"]
    assert "时间跳跃" in reminder["late_cause"] and reminder["late_minutes"] >= 253
    assert not row["repeat"]["sent"] and not row["quiet_probe"]["sent"]
    assert row["quiet_reminder_probe"]["reminders"][0]["delivery"] == "postponed_quiet_hours"
    assert not row["on_time_real_delivery_verified"]


@pytest.mark.parametrize("flags,expected", [
    (["--max-calls", "2"], 2), (["--max-tokens", "1"], 0),
    (["--max-calls-per-round", "3"], 6),
    (["--price-per-1k", "1", "--max-cost", "0.01"], 1),
])
def test_budget_refusal_stops_and_keeps_execution_evidence(flags, expected):
    _, report = run_case("--real", *flags)
    assert report["status"] == "失败" and report["execution_state"] == "stopped_budget"
    assert report["total_calls"] == len(report["fake_requests"]) == expected
    assert report["budget"]["refused_calls"]
    assert any(not d["approved"] for d in report["budget_reservations"])
    assert sum(report["accounting"]["by_round"].values()) == expected


def test_usage_over_reserve_stops_before_next_request():
    _, report = run_case("--real", "--max-tokens", "20000", usage=19000)
    assert report["execution_state"] == "stopped_budget" and report["total_calls"] == 1
    assert report["budget"]["used_tokens"] == 19000
    assert report["budget_reservations"][1]["reserved_tokens"] >= 20900


def test_failed_reminder_call_cannot_pass_despite_local_fallback():
    _, report = run_case("--real", failure=True)
    assert report["status"] == "失败" and report["execution_state"] == "failed_model_call"
    failed = [c for c in report["calls"] if not c["ok"]]
    assert failed and all(c["round"] == 28 and c["stage"] == "round28.reminder_due" for c in failed)
    assert report["stages"][-1]["id"] == "round28.reminder_due"
    assert report["rounds"][1]["reminder"]["reminders"][0]["delivery"] == "sent"


def test_invalid_reminder_content_is_not_accepted_as_model_success():
    _, report = run_case("--real", bad_reminder=True)
    assert report["status"] == "失败" and not report["checks"]["reminder_model_content_valid"]


def test_provider_echoed_key_is_scrubbed_before_database_and_logs():
    _, report = run_case("--real", echo=True)
    assert "<redacted>" in "".join(report["rounds"][0]["violet"])


def test_fake_real_report_and_scores_still_rejected_by_original_acceptance(fake_real):
    from tools.dialogue_eval import load_scores, recheck_report
    from tools.replay import ReplaySourceError, check_workdir
    path, report = fake_real
    assert report["synthetic"] and report["specialized_test"]
    assert not report["counts_toward_original_32"] and report["original_32_score"] is None
    with pytest.raises(ReplaySourceError): check_workdir(path, False, {27, 28}, True)
    with pytest.raises(ReplaySourceError): recheck_report(path / "specialized_report.json")
    with pytest.raises(ValueError): load_scores(path / "specialized_scores.csv")


@pytest.mark.parametrize("url", ["", "file:///test", "http://user:password@localhost/v1", "https://example.invalid/v1?key=x", "https://example.invalid/v1#key"])
def test_unsafe_endpoint_rejected_before_gate(url):
    with pytest.raises(ValueError): validate_endpoint(url)


def test_no_confirmation_bypass_flag():
    with pytest.raises(SystemExit): build_parser().parse_args(["--workdir", "x", "--real", "--yes"])


def test_network_whitelist_restricts_host_and_port_without_connecting():
    from tools.harness import NetworkGuard
    guard = NetworkGuard(["http://127.0.0.1:18991/v1"])
    guard._check(("127.0.0.1", 18991))
    for address in [("127.0.0.1", 18992), ("127.0.0.2", 18991), ("example.invalid", 443)]:
        with pytest.raises(ConnectionRefusedError): guard._check(address)
    assert len(guard.blocked) == 3


def test_sdk_error_type_status_and_body_survive_scrubbing():
    import httpx
    from openai import BadRequestError
    from tools.harness import Budget, BudgetedClient, CallRecorder
    key = "fake-private-provider-key"
    response = httpx.Response(400, request=httpx.Request("POST", "http://127.0.0.1/v1"))
    error = BadRequestError("echo " + key, response=response, body={"error": key})
    def fail(**kwargs): raise error
    inner = type("Inner", (), {"chat": type("Chat", (), {
        "completions": type("Completions", (), {"create": staticmethod(fail)})()})()})()
    recorder = CallRecorder(Budget(), secrets=[key])
    client = BudgetedClient(inner, recorder)
    with pytest.raises(BadRequestError) as caught:
        client.chat.completions.create(model="fake", messages=[])
    assert caught.value.status_code == 400 and key not in str(caught.value)
    assert key not in json.dumps(caught.value.body)
    assert recorder.calls[0].status_code == 400 and not recorder.calls[0].ok


def test_environment_forces_integrations_off(tmp_path):
    from tools.harness import prepare_environment
    env = {"VIOLET_IMAGE_ENABLED": "1", "MCP_SERVERS": "unsafe", "VIOLET_WEB_ENABLED": "1"}
    prepare_environment(tmp_path / "isolated", None, environ=env)
    assert env["VIOLET_IMAGE_ENABLED"] == "0"
    assert env["MCP_SERVERS"] == "" and env["VIOLET_WEB_ENABLED"] == "0"
    assert env["VIOLET_ENV_FILE"].endswith("empty.env")
