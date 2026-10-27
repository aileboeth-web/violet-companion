"""29–32 specialized suite: real product pipeline, fake SDK only, persistent local evidence."""
import json
import os
import subprocess
import sys
import uuid

import pytest

from tools.synthetic_rounds import PROJECT, digest
from tools.synthetic_tail import PURPOSE, PLAN, REVISION, SENT_REPORT, build_tail_seed


BOOTSTRAP = r'''
import json, os, sys
from unittest.mock import patch
from tools import harness as h
from tools import synthetic_tail as t
from tools.synthetic_rounds import build_parser, run_specialized
options=json.loads(sys.argv[1])
fault=options.get("fault")
key="fake-b115-private-test-credential-0000"
os.environ.pop("VIOLET_TEST_API_KEY",None)
os.environ["VIOLET_TEST_ALLOW_REAL_API"]=options.get("allow","1")
os.environ["VIOLET_TEST_API_URL"]="http://127.0.0.1:18995/v1"
requests=[]
base=t.tail_mock()
class FakeSDK(base):
    def __init__(self,**kwargs):
        assert kwargs["api_key"]==key and kwargs["max_retries"]==0
        assert kwargs["base_url"]=="http://127.0.0.1:18995/v1"
        super().__init__()
    def _create(self,**kwargs):
        user=next((m.get("content") for m in reversed(kwargs["messages"]) if m["role"]=="user"),"")
        agent=h._AGENT.get()
        requests.append(agent)
        if fault=="memory_failure" and agent=="MemoryAgent" and "其实我不是学天文" in user:
            raise RuntimeError(key)
        if fault=="correction_declined" and agent=="MemoryAgent" and "其实我不是学天文" in user:
            return h.MockLLM._create(self,**kwargs)
        if agent=="IntentAgent" and "答辩你还记得" in user:
            if fault=="no_query": return h.MockLLM._create(self,**kwargs)
            if fault=="wrong_target" and not any(m["role"]=="tool" for m in kwargs["messages"]):
                return self.tools(kwargs["model"],[("query_memory",{"query":"泰山"})])
        if agent=="ChatAgent" and "我的专业是什么" in user and fault=="old_reply":
            return self._completion("你的专业是天文。",kwargs["model"])
        if agent=="ChatAgent" and "答辩你还记得" in user and fault=="unfinished_reply":
            return self._completion("还在准备答辩吧，准备得怎么样？",kwargs["model"])
        return super()._create(**kwargs)
original_say=h.VioletTestSession.say
def say(self,text,number=None,**kwargs):
    result=original_say(self,text,number,**kwargs)
    if number==31:
        if fault=="old_active":
            self.db.execute("UPDATE user_profile_facts SET status='active' WHERE fact_key='study.major' AND value='天文'")
        if fault=="old_not_superseded":
            self.db.execute("UPDATE user_profile_facts SET status='candidate' WHERE fact_key='study.major' AND value='天文'")
        if fault=="background_incomplete":
            result["background_complete"]=False
            result["execution_errors"].append({"kind":"memory_job","status":"failed"})
    if number==29 and fault=="recreated_reminder":
        row=self.db.list_commitments()[0]
        self.db.add_commitment(row["content"],row["due_at"],source_turn_id="fault:new-reminder")
    return result
original_tick=h.VioletTestSession.proactive_tick
def tick(self):
    result=original_tick(self)
    if fault=="duplicate_send":
        self.wechat.send_text("该交作业了。")
        result["sent"].append("该交作业了。")
    return result
original_write=h.write_report
def write(name,report,recorder,directory):
    report["verification_environment"]="fake_sdk_offline"
    report["fake_sdk_requests"]=len(requests)
    return original_write(name,report,recorder,directory)
args=build_parser().parse_args(options["argv"])
with patch("openai.OpenAI",FakeSDK),patch.object(h.VioletTestSession,"say",say),patch.object(h.VioletTestSession,"proactive_tick",tick),patch.object(h,"write_report",write):
    if fault=="missing_context":
        # Install after environment isolation, before any messages.
        original_init=h.VioletTestSession.__init__
        def initialize(self,*a,**kw):
            original_init(self,*a,**kw)
            from ai.context_engine import ContextEngine
            original_facts=ContextEngine._relevant_facts
            def facts(engine,mode,text):
                rows=original_facts(engine,mode,text)
                return [r for r in rows if r["fact_key"] != "study.major"] if "我的专业是什么" in text else rows
            ContextEngine._relevant_facts=facts
        with patch.object(h.VioletTestSession,"__init__",initialize):
            run_specialized(args.workdir,args=args,input_fn=lambda p:options.get("answer","yes"),key_prompt=lambda p:key)
    else:
        run_specialized(args.workdir,args=args,input_fn=lambda p:options.get("answer","yes"),key_prompt=lambda p:key)
'''


def run_case(*flags, **options):
    path = PROJECT.parent / "local/b115/test-cases" / uuid.uuid4().hex
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", BOOTSTRAP, json.dumps({
        "argv": ["--suite", PURPOSE, "--workdir", str(path), *flags], **options})], cwd=PROJECT,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"), capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, (result.stdout, result.stderr)
    secret = b"fake-b115-private-test-credential-0000"
    assert secret not in (result.stdout + result.stderr).encode()
    assert all(secret not in p.read_bytes() for p in path.rglob("*") if p.is_file())
    return path, json.loads((path / "specialized_report.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def simulated():
    return run_case()


@pytest.fixture(scope="module")
def fake_real():
    return run_case("--real")


def test_plan_requirements_and_source_hashes_are_bound():
    seed = build_tail_seed()
    root = PROJECT.parent
    assert digest(root / PLAN) == json.loads((root / REVISION).read_text(encoding="utf-8"))["base_sha256"]
    plan = json.loads((root / PLAN).read_text(encoding="utf-8"))
    assert [r["definition"]["value"] for r in seed["requirements"]] == plan["rounds"][28:32]
    def verify(node):
        if isinstance(node, dict):
            if "source" in node:
                assert node["source"] in {"report", "inferred", "synthetic", "unknown"}
                evidence = node["evidence"]
                if evidence:
                    assert digest(root / evidence["path"]) == evidence["sha256"]
                    value = json.loads((root / evidence["path"]).read_text(encoding="utf-8"))
                    for part in evidence["pointer"].strip("/").split("/"):
                        value = value[int(part)] if isinstance(value, list) else value[part]
                    if node["source"] == "report": assert value == node["value"]
                return
            for child in node.values(): verify(child)
        elif isinstance(node, list):
            for child in node: verify(child)
        else: raise AssertionError("Unattributed seed state")
    verify(seed["state"])
    assert all(v["source"] == "unknown" and v["value"] is None for v in seed["state"]["unknown"].values())


def test_seed_no_answers_or_earlier_control_probes(simulated):
    path, report = simulated
    state = report["initial_state"]
    assert [f["value"] for f in state["facts"] if f["fact_key"] == "study.major"] == ["天文"]
    assert not state["messages"] and not any("交了作业" in e["content"] for e in state["events"])
    assert not any(p["pref_key"] == "proactive_chat" for p in state["preferences"])
    assert len(state["commitments"]) == 1 and state["commitments"][0]["status"] == "sent"
    assert "静默探针" not in json.dumps(state, ensure_ascii=False)
    cfg = json.loads((path / "proactive.json").read_text(encoding="utf-8"))
    assert cfg["quiet_hours"] != {"start": "00:00", "end": "23:59"}
    materialized = json.loads((path / "seed_materialized.json").read_text(encoding="utf-8"))
    assert not materialized["tables"]["life_events"] and not materialized["tables"]["user_situations"]
    assert all("source" in v for rows in materialized["tables"].values() for r in rows for v in r.values())


def test_all_four_rounds_run_offline_with_manual_scores_preserved(simulated):
    path, report = simulated
    assert report["status"] == "通过" and report["execution_state"] == "complete"
    assert not report["real"] and report["fake_sdk_requests"] == 0
    assert [r["n"] for r in report["rounds"]] == [29, 30, 31, 32]
    assert all(r["status"] == "通过" for r in report["rounds"]) and all(report["checks"].values())
    assert report["manual_scores"] == {str(n): None for n in range(29, 33)}
    assert report["network_guard"]["allowed_connections"] == 0 and report["network_guard"]["allowed_endpoints"] == []
    assert not (path / ".violet-eval-state.json").exists()


def test_round29_no_recreated_or_resent_reminder(simulated):
    _, report = simulated
    r = report["rounds"][0]
    assert r["dedup_probe"]["additional_probe"] and not r["dedup_probe"]["sent"]
    assert all(r["checks"].values()) and r["checks"]["no_recreated_reminder"]


def test_round30_actual_target_result_and_exact_chat_event_line(simulated):
    _, report = simulated
    r = report["rounds"][1]
    evidence = r["recall_evidence"]
    [target] = [m for m in evidence["returned"] if m["id"] == evidence["target_id"]]
    assert target["progress"] == "done" and "答辩" in target["content"] and "顺利" in target["content"]
    assert any(target["content"] in line and line.startswith(f"- id={target['id']}; ") for line in evidence["chat_event_lines"])
    assert any(c["agent"] == "IntentAgent" and "query_memory" in c["tool_calls"] for c in r["calls"])


def test_round31_32_use_persisted_correction_and_same_db(simulated):
    import sqlite3
    path, report = simulated
    correction, recall = report["rounds"][2:]
    [new] = correction["correction_evidence"]["active"]
    old = correction["correction_evidence"]["old"]
    assert new["value"] == "物理" and old["status"] == "superseded"
    assert new["supersedes_id"] == old["id"] and old["superseded_by"] == new["id"]
    assert new["id"] in recall["profile_context_evidence"]["fact_ids"]
    assert old["id"] not in recall["profile_context_evidence"]["fact_ids"]
    assert "物理" in "".join(recall["violet"]) and "天文" not in "".join(recall["violet"])
    conn = sqlite3.connect(f"file:{(path / 'violet-test.db').as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        assert conn.execute("SELECT value FROM user_profile_facts WHERE id=? AND status='active'", (new["id"],)).fetchone() == ("物理",)
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0] == 4
    finally: conn.close()


def test_no_added_dates_or_time_jumps(simulated):
    _, report = simulated
    source = json.loads((PROJECT.parent / SENT_REPORT).read_text(encoding="utf-8"))
    sent_at = source["rounds"][1]["reminder"]["reminders"][0]["delivered_at"]
    assert report["clock_policy"]["value"] == sent_at
    assert not report["scenario_clock"]["time_jumps"]
    assert all(r["scenario_time"][:10] == sent_at[:10] for r in report["rounds"])


def test_fake_real_reuses_gate_and_complete_agent_pipeline(fake_real):
    _, report = fake_real
    assert report["status"] == "通过" and report["real"]
    assert report["total_calls"] == report["fake_sdk_requests"] == 13
    assert sum(report["accounting"]["by_stage"].values()) == sum(report["accounting"]["by_round"].values()) == 13
    assert report["network_guard"]["allowed_endpoints"] == ["127.0.0.1:18995"]
    assert any("correct_profile_fact" in c["tool_calls"] for c in report["rounds"][2]["calls"])


@pytest.mark.parametrize("options", [{"allow": "0"}, {"answer": "no"}])
def test_tail_real_authorization_refused_without_any_sdk_request(options):
    _, report = run_case("--real", **options)
    assert report["status"] == "未执行" and report["fake_sdk_requests"] == 0


@pytest.mark.parametrize("fault", ["correction_declined", "old_active", "old_not_superseded", "background_incomplete", "memory_failure"])
def test_failed_correction_or_background_blocks_round32(fault):
    _, report = run_case("--real", fault=fault)
    assert report["status"] == "失败" and report["rounds"][2]["status"] == "失败"
    assert report["rounds"][3]["status"] == "未执行" and not report["rounds"][3]["calls"]
    assert not any(s["id"] == "round32.chat" for s in report["stages"])


@pytest.mark.parametrize("fault", ["old_reply", "missing_context"])
def test_round32_cannot_pass_with_old_answer_or_missing_profile_context(fault):
    _, report = run_case("--real", fault=fault)
    assert report["status"] == "失败" and report["rounds"][2]["status"] == "通过"
    assert report["rounds"][3]["status"] == "失败"


@pytest.mark.parametrize("fault", ["no_query", "wrong_target", "unfinished_reply"])
def test_round30_wrong_recall_or_unfinished_answer_fails(fault):
    _, report = run_case("--real", fault=fault)
    assert report["status"] == "失败" and report["rounds"][1]["status"] == "失败"
    assert report["rounds"][2]["status"] == "未执行"


@pytest.mark.parametrize("fault", ["duplicate_send", "recreated_reminder"])
def test_round29_duplicate_reminder_fails(fault):
    _, report = run_case("--real", fault=fault)
    assert report["status"] == "失败" and report["rounds"][0]["status"] == "失败"


@pytest.mark.parametrize("flags,maximum", [(["--max-calls", "3"], 3), (["--max-tokens", "1"], 0), (["--max-calls-per-round", "1"], 1)])
def test_tail_budget_stops_without_later_success(flags, maximum):
    _, report = run_case("--real", *flags)
    assert report["status"] == "失败" and report["execution_state"] == "stopped_budget"
    assert report["total_calls"] == report["fake_sdk_requests"] == maximum
    assert report["budget"]["refused_calls"] and any(not d["approved"] for d in report["budget_reservations"])


def test_new_purpose_alone_rejected_by_every_report_and_state_reader(tmp_path):
    from tools.replay import ReplaySourceError, _load_report, read_state, load_plan, require_continuous_scope
    from tools.dialogue_eval import summarize, recheck_report
    from tools.harness import seed_workdir, IsolationError
    data = {"purpose": PURPOSE}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / ".violet-eval-state.json").write_text(json.dumps(data), encoding="utf-8")
    for action in (lambda: _load_report(path), lambda: read_state(tmp_path), lambda: summarize(data),
                   lambda: recheck_report(path), lambda: load_plan(path), lambda: require_continuous_scope(data)):
        with pytest.raises(ReplaySourceError): action()
    with pytest.raises(IsolationError): seed_workdir(tmp_path, tmp_path / "target")
    assert not (tmp_path / "target").exists()


def test_new_purpose_alone_refused_by_scores(tmp_path):
    from tools.dialogue_eval import load_scores
    path = tmp_path / "scores.csv"
    path.write_text("purpose,round,score(0/1/2)\n" + PURPOSE + ",29,2\n", encoding="utf-8")
    with pytest.raises(ValueError): load_scores(path)


def test_plan_binding_failure_refuses_before_session(monkeypatch):
    from tools import synthetic_tail as t
    original = t.digest
    monkeypatch.setattr(t, "digest", lambda p: "0" * 64 if str(p).endswith("acceptance_dialogue_30.json") else original(p))
    with pytest.raises(ValueError, match="hash mismatch"): t.build_tail_seed()


@pytest.mark.parametrize("changes", [{"real": False}, {"status": "失败"}, {"verification_environment": "fake_sdk_offline"}])
def test_simulated_or_failed_delivery_evidence_cannot_seed_sent_state(tmp_path, monkeypatch, changes):
    from tools import synthetic_tail as t
    data = json.loads((PROJECT.parent / SENT_REPORT).read_text(encoding="utf-8"))
    data.update(changes)
    path = tmp_path / "rejected.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(t, "SENT_REPORT", str(path))
    with pytest.raises(ValueError, match="real specialized"): t.build_tail_seed()


def test_original_cli_refuses_tail_directory_before_calling_any_model(simulated):
    path, _ = simulated
    for flags in (["--workdir", str(path)], ["--seed-from", str(path), "--workdir", str(path / "never-created")]):
        result = subprocess.run([sys.executable, "-X", "utf8", "-m", "tools.dialogue_eval", "--rounds", "29-32", *flags],
                                cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 2 and "合成" in result.stdout
    assert not (path / "never-created").exists()


def test_cli_failed_correction_preserves_check_state_and_blocks_round32():
    path = PROJECT.parent / "local/b115/test-cases" / uuid.uuid4().hex
    # runpy recreates `python -m`'s separate __main__ module identity. No real model is enabled.
    code = r'''
import runpy, sys
from tools import synthetic_tail as t, harness as h
base=t.tail_mock()
plain=h.MockLLM
class Decline(base):
    def _create(self,**kw):
        user=next((m.get("content") for m in reversed(kw["messages"]) if m["role"]=="user"),"")
        if h._AGENT.get()=="MemoryAgent" and "其实我不是学天文" in user:
            return plain._create(self,**kw)
        return super()._create(**kw)
t.tail_mock=lambda:Decline
sys.argv=["tools.synthetic_rounds","--suite",t.PURPOSE,"--workdir",sys.argv[1]]
runpy.run_module("tools.synthetic_rounds",run_name="__main__")
'''
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(path)], cwd=PROJECT,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"), capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 1
    report = json.loads((path / "specialized_report.json").read_text(encoding="utf-8"))
    assert report["execution_state"] == "failed_check" and report["status"] == "失败"
    assert report["rounds"][3]["status"] == "未执行"
