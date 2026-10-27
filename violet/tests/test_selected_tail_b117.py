"""Controlled 30–32 execution and boundary-observed audit, all offline/fake SDK."""
import csv
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tools.synthetic_rounds import PROJECT, digest
from tools import synthetic_tail as tail
from tests.test_synthetic_tail_b115 import BOOTSTRAP as LEGACY_BOOTSTRAP


BOOTSTRAP = LEGACY_BOOTSTRAP.replace('requests.append(agent)', '''requests.append(agent)
        if agent=="IntentAgent" and "答辩你还记得" in user and not any(m["role"]=="tool" for m in kwargs["messages"]):
            if options.get("query_parameters"):
                return self.tools(kwargs["model"],[("query_memory",options["query_parameters"])])
            if options.get("echo_query"):
                return self.tools(kwargs["model"],[("query_memory",{"query":"答辩 "+key,"category":"event","extra":{"token":key}})])
        if agent=="MemoryAgent" and "答辩你还记得" in user and options.get("memory_query") and not any(m["role"]=="tool" for m in kwargs["messages"]):
            return self.tools(kwargs["model"],[("query_memory",{"query":user,"category":"event","limit":2,"min_similarity":.3})])''').replace(
    'result=original_say(self,text,number,**kwargs)', '''if number==30:
        if options.get("different_normalization"):
            import ai.agents.intent_agent as ia
            ia.recall_search_text=lambda q:"泰山"
        if options.get("handler_failure"):
            def fail(args): raise RuntimeError("offline retrieval failure")
            self.agents["intent"]._handlers["query_memory"]=fail
        if options.get("no_word_recall"):
            os.environ["VIOLET_MEMORY_WORD_RECALL"]="0"
    result=original_say(self,text,number,**kwargs)''')


def run_case(*flags, **options):
    directory = PROJECT.parent / "local/b117/test-cases" / uuid.uuid4().hex
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOTSTRAP, json.dumps({
        "argv": ["--suite", tail.PURPOSE, "--rounds", "30-32", "--workdir", str(directory), *flags], **options})],
        cwd=PROJECT, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, (result.stdout, result.stderr)
    secret = b"fake-b115-private-test-credential-0000"
    assert secret not in (result.stdout + result.stderr).encode()
    assert all(secret not in p.read_bytes() for p in directory.rglob("*") if p.is_file())
    return directory, json.loads((directory / "specialized_report.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def selected():
    return run_case()


def test_selected_rounds_only_with_historical_skip_source(selected):
    directory, report = selected
    assert report["status"] == "通过" and not report["real"]
    assert report["network_guard"]["allowed_connections"] == 0 and report["network_guard"]["allowed_endpoints"] == []
    assert [r["n"] for r in report["rounds"]] == report["executed_rounds"] == [30, 31, 32]
    assert report["manual_scores"] == {"30": None, "31": None, "32": None}
    assert set(report["accounting"]["by_round"]) == {"30", "31", "32"}
    assert all(c["round"] != 29 for c in report["calls"])
    assert not any(s["round"] == 29 for s in report["stages"])
    assert sum(report["accounting"]["by_round"].values()) == len(report["calls"]) == report["total_calls"] == 10
    [skipped] = report["skipped_rounds"]
    assert skipped["n"] == 29 and skipped["status"] == "未执行" and not skipped["history_imported"]
    evidence = skipped["historical_evidence"]
    assert evidence["sha256"] == digest(PROJECT.parent / evidence["path"])
    historical = json.loads((PROJECT.parent / evidence["path"]).read_text(encoding="utf-8"))
    assert historical["status"] == "失败"  # R30 failed; R29 itself is valid evidence.
    row = historical["rounds"][int(evidence["pointer"].split("/")[-1])]
    assert row["n"] == 29 and row["status"] == evidence["historical_status"] == "通过"
    with (directory / "specialized_scores.csv").open(encoding="utf-8-sig", newline="") as f:
        assert [r["round"] for r in csv.DictReader(f)] == ["30", "31", "32"]


def test_no_round29_history_or_future_answer_seeded_and_shared_correction(selected):
    directory, report = selected
    initial = report["initial_state"]
    assert initial["messages"] == []
    assert [f["value"] for f in initial["facts"] if f["fact_key"] == "study.major"] == ["天文"]
    assert not any("交了作业" in r["content"] for r in initial["events"])
    r31, r32 = report["rounds"][1:]
    [new] = r31["correction_evidence"]["active"]
    assert new["value"] == "物理" and r31["background_complete"]
    assert r31["correction_evidence"]["old"]["status"] == "superseded"
    assert r32["profile_context_evidence"]["active"] == [{"id": new["id"], "value": "物理"}]
    assert new["id"] in r32["profile_context_evidence"]["fact_ids"]
    assert all(r31["checks"].values()) and all(r32["checks"].values())
    import sqlite3
    with sqlite3.connect(directory / "violet-test.db") as db:
        assert db.execute("SELECT value FROM user_profile_facts WHERE fact_key='study.major' AND status='active'").fetchall() == [("物理",)]


def test_actual_query_search_and_chat_audit(selected):
    _, report = selected
    [entry] = [a for a in report["memory_query_audit"] if a["agent"] == "IntentAgent"]
    assert entry["parameters"] == {"query": "上次说的答辩你还记得吗"}
    assert entry["round"] == 30 and entry["stage"] == "round30.chat"
    [search] = entry["searches"]
    assert search["normalized_query"] == search["effective_parameters"]["query"] == "答辩"
    assert search["storage"] == "event_memories"
    assert search["effective_parameters"] == {"query": "答辩", "category": None, "status": None,
        "limit": 5, "min_similarity": .35, "include_disabled": False, "recall": True}
    assert search["returned_count"] == entry["returned_count"] == 1
    target = report["rounds"][0]["recall_evidence"]["target_id"]
    assert entry["returned_events"][0]["id"] == target and entry["returned_events"][0]["progress"] == "done"
    assert search["returned_events"] == entry["returned_events"]
    call = next(c for c in report["calls"] if c["call_no"] == entry["call_no"])
    assert call["run_id"] == entry["run_id"] and "query_memory" in call["tool_calls"]
    [chat] = [a for a in report["chat_context_audit"] if a["round"] == 30]
    assert chat["target_event_id"] == target and chat["target_event_in_context"]
    assert any(line.startswith(f"- id={target}; ") and "顺利" in line and "已结束" in line for line in chat["event_lines"])


def test_effective_params_not_guessed_from_model_arguments():
    parameters = {"query": "上次说的答辩你还记得吗", "category": "event", "limit": 2, "min_similarity": .99}
    _, report = run_case("--real", query_parameters=parameters, memory_query=True)
    assert report["status"] == "通过" and report["verification_environment"] == "fake_sdk_offline"
    intent = next(a for a in report["memory_query_audit"] if a["agent"] == "IntentAgent")
    memory = next(a for a in report["memory_query_audit"] if a["agent"] == "MemoryAgent")
    assert intent["parameters"] == parameters
    assert intent["searches"][0]["effective_parameters"]["min_similarity"] == .35  # Intent ignores this argument.
    assert memory["searches"][0]["effective_parameters"]["min_similarity"] == .3
    assert memory["searches"][0]["effective_parameters"]["include_disabled"] is True
    assert all(a["searches"][0]["effective_parameters"]["category"] == "event" for a in (intent, memory))


# Phase 9.1: the user's own words ("答辩") now bring the target into Chat even when Intent's search misses. The two audit
# tests below are about recording the miss itself, so they run without that word recall (VIOLET_MEMORY_WORD_RECALL=0).


def test_audit_observes_actual_changed_query_and_absent_chat_target():
    _, report = run_case(different_normalization=True, no_word_recall=True)
    assert report["status"] == "失败"
    entry = report["memory_query_audit"][0]
    assert entry["parameters"]["query"] == "上次说的答辩你还记得吗"
    assert entry["searches"][0]["normalized_query"] == "泰山"
    assert entry["returned_events"][0]["id"] != report["rounds"][0]["recall_evidence"]["target_id"]
    assert not report["chat_context_audit"][0]["target_event_in_context"]
    assert [r["status"] for r in report["rounds"]] == ["失败", "未执行", "未执行"]
    assert report["executed_rounds"] == [30] and set(report["accounting"]["by_round"]) == {"30"}


def test_word_recall_brings_the_target_into_chat_when_intent_search_misses():
    _, report = run_case(different_normalization=True)                       # phase 9.1 default
    entry = report["memory_query_audit"][0]
    target = report["rounds"][0]["recall_evidence"]["target_id"]
    assert entry["returned_events"][0]["id"] != target                       # Intent still missed it
    chat = report["chat_context_audit"][0]
    assert chat["target_event_in_context"]
    assert any(line.startswith(f"- id={target}; ") and "已结束" in line and "按这句话里的词找到" in line
               for line in chat["event_lines"])


def test_tool_failure_is_audited_without_invented_search_result():
    _, report = run_case(handler_failure=True)
    entry = report["memory_query_audit"][0]
    assert entry["status"] == "失败" and entry["error_code"] == "LOCAL_TOOL_ERROR"
    assert entry["searches"] == [] and "returned_count" not in entry
    assert report["status"] == "失败" and report["rounds"][1]["status"] == "未执行"


def test_credentials_scrubbed_in_actual_query_audit():
    _, report = run_case("--real", echo_query=True)
    parameters = report["memory_query_audit"][0]["parameters"]
    assert parameters["query"] == "答辩 <redacted>" and parameters["extra"]["token"] == "<redacted>"


@pytest.mark.parametrize("fault,expected", [("wrong_target", ["失败", "未执行", "未执行"]),
    ("correction_declined", ["通过", "失败", "未执行"]), ("old_active", ["通过", "失败", "未执行"]),
    ("background_incomplete", ["通过", "失败", "未执行"]), ("memory_failure", ["通过", "失败", "未执行"])])
def test_failure_stops_selected_dependencies(fault, expected):
    _, report = run_case("--real", fault=fault)
    assert report["status"] == "失败" and [r["status"] for r in report["rounds"]] == expected
    assert report["rounds"][-1]["calls"] == [] and all(c["round"] != 32 for c in report["calls"])


@pytest.mark.parametrize("flags", [("--max-calls", "2"), ("--max-tokens", "1"), ("--max-calls-per-round", "1")])
def test_selected_budget_stops_and_only_actual_rounds_count(flags):
    _, report = run_case("--real", *flags)
    assert report["status"] == "失败" and report["budget"]["refused_calls"]
    assert all(c["round"] == 30 for c in report["calls"])
    assert set(report["accounting"]["by_round"]) <= {"30"}
    assert all(r["status"] == "未执行" for r in report["rounds"][1:])


@pytest.mark.parametrize("options", [{"allow": "0"}, {"answer": "no"}])
def test_selected_real_requires_original_authorization(options):
    _, report = run_case("--real", **options)
    assert report["status"] == "未执行" and report["execution_state"] == "authorization_refused"
    assert report["total_calls"] == report["fake_sdk_requests"] == 0
    assert report["accounting"]["by_round"] == {}


def test_empty_query_does_not_get_invented_normalized_topic():
    _, report = run_case("--real", query_parameters={"query": ""})
    entry = report["memory_query_audit"][0]
    assert entry["parameters"] == {"query": ""} and entry["searches"] == []
    assert entry["status"] == "通过" and entry["returned_count"] == 0
    assert report["status"] == "失败" and report["executed_rounds"] == [30]


def test_actual_category_filter_with_empty_result_is_preserved():
    _, report = run_case("--real", query_parameters={"query": "上次说的答辩你还记得吗", "category": "missing"},
                         no_word_recall=True)
    entry = report["memory_query_audit"][0]
    [search] = entry["searches"]
    assert search["effective_parameters"]["category"] == "missing" and search["normalized_query"] == "答辩"
    assert entry["returned_count"] == search["returned_count"] == 0
    assert entry["returned_events"] == search["returned_events"] == []
    assert report["status"] == "失败" and not report["chat_context_audit"][0]["target_event_in_context"]


# "31-32" became a gated selection in B.11.10 (tests/test_rounds31_32_b1110.py).
@pytest.mark.parametrize("value,suite", [("31", tail.PURPOSE), ("32", tail.PURPOSE), ("30", tail.PURPOSE),
    ("29,30,31,32", tail.PURPOSE), ("30-32", "round27_28_specialized_test")])
def test_unsupported_selections_refuse_before_directory_or_model(value, suite):
    directory = PROJECT.parent / "local/b117/test-cases" / uuid.uuid4().hex
    result = subprocess.run([sys.executable,"-B","-X","utf8","-m","tools.synthetic_rounds","--suite",suite,
        "--rounds",value,"--workdir",str(directory)],cwd=PROJECT,capture_output=True,text=True,encoding="utf-8",timeout=30)
    assert result.returncode != 0 and not directory.exists()


@pytest.mark.parametrize("fault", ["mock", "failed_round", "no_calls", "failed_call", "failed_check", "wrong_input"])
def test_round29_evidence_validation_without_modifying_source(monkeypatch, fault):
    seed = tail.build_tail_seed()
    path = PROJECT.parent / tail.ROUND29_REPORT
    source = json.loads(path.read_text(encoding="utf-8"))
    row = source["rounds"][0]
    if fault == "mock": source["real"] = False
    if fault == "failed_round": row["status"] = "失败"
    if fault == "no_calls": row["calls"] = []
    if fault == "failed_call": row["calls"][0]["ok"] = False
    if fault == "failed_check": row["checks"]["execution_complete"] = False
    if fault == "wrong_input": row["user"] = "unrelated"
    original = Path.read_text
    monkeypatch.setattr(Path,"read_text",lambda p,*a,**k:json.dumps(source) if p==path else original(p,*a,**k))
    with pytest.raises(ValueError,match="evidence|successful"):
        tail.selection_evidence(seed,(30,31,32))


def test_selected_scope_rejected_by_original_acceptance(selected):
    from tools.replay import require_continuous_scope
    directory, report = selected
    with pytest.raises(ValueError): require_continuous_scope({"purpose": report["purpose"]})
    result = subprocess.run([sys.executable,"-B","-X","utf8","-m","tools.dialogue_eval",
        "--rounds","30-32","--workdir",str(directory)],cwd=PROJECT,capture_output=True,text=True,encoding="utf-8",timeout=30)
    assert result.returncode == 2 and "合成" in result.stdout


def test_selected_cli_failed_round30_stops_without_round31_32_calls():
    directory = PROJECT.parent / "local/b117/test-cases" / uuid.uuid4().hex
    code = r'''
import runpy,sys
from tools import synthetic_tail as t,harness as h
base=t.tail_mock()
class Wrong(base):
    def _create(self,**kw):
        user=next((m.get("content") for m in reversed(kw["messages"]) if m["role"]=="user"),"")
        if h._AGENT.get()=="IntentAgent" and "答辩你还记得" in user and not any(m["role"]=="tool" for m in kw["messages"]):
            return self.tools(kw["model"],[("query_memory",{"query":"泰山"})])
        return super()._create(**kw)
t.tail_mock=lambda:Wrong
sys.argv=["tools.synthetic_rounds","--suite",t.PURPOSE,"--rounds","30-32","--workdir",sys.argv[1]]
runpy.run_module("tools.synthetic_rounds",run_name="__main__")
'''
    result = subprocess.run([sys.executable,"-B","-X","utf8","-c",code,str(directory)],cwd=PROJECT,
        env=dict(os.environ,PYTHONIOENCODING="utf-8"),capture_output=True,text=True,encoding="utf-8",timeout=30)
    assert result.returncode == 1
    report = json.loads((directory / "specialized_report.json").read_text(encoding="utf-8"))
    assert report["execution_state"] == "failed_check" and report["status"] == "失败"
    assert [r["status"] for r in report["rounds"]] == ["失败","未执行","未执行"]
    assert all(c["round"] == 30 for c in report["calls"])
