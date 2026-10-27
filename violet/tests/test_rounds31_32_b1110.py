"""B.11.10: --rounds 31-32 gated on registered round 29/30 evidence; offline / fake SDK only."""
import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tools.synthetic_rounds import PROJECT, digest
from tools import synthetic_tail as tail
from tests.test_synthetic_tail_b115 import BOOTSTRAP

ROUND30_REPLY = "记得呀，小满，你天文观测课程的答辩当时挺顺利的。"
needs_evidence = pytest.mark.skipif(not all((PROJECT.parent / p).is_file() for p in tail.REGISTERED_SHA256),
                                    reason="registered round30 evidence is not in this checkout")
pytestmark = needs_evidence


def run_case(*flags, code=BOOTSTRAP, expect=0, **options):
    directory = PROJECT.parent / "local/b1110/test-cases" / uuid.uuid4().hex
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code, json.dumps({
        "argv": ["--suite", tail.PURPOSE, "--rounds", "31-32", "--workdir", str(directory), *flags], **options})],
        cwd=PROJECT, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == expect, (result.stdout, result.stderr)
    secret = b"fake-b115-private-test-credential-0000"
    assert secret not in (result.stdout + result.stderr).encode()
    if expect:
        return directory, result
    assert all(secret not in p.read_bytes() for p in directory.rglob("*") if p.is_file())
    return directory, json.loads((directory / "specialized_report.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def mock_run():
    return run_case()


@pytest.fixture(scope="module")
def fake_real():
    return run_case("--real")


@pytest.mark.parametrize("fixture", ["mock_run", "fake_real"])
def test_correction_then_recall_pass_and_only_31_32_are_counted(request, fixture):
    directory, report = request.getfixturevalue(fixture)
    assert report["status"] == "通过" and report["execution_state"] == "complete"
    assert report["synthetic"] and not report["continuous_acceptance_eligible"] and not report["counts_toward_original_32"]
    assert report["original_32_score"] is None and report["manual_scores"] == {"31": None, "32": None}
    assert [r["n"] for r in report["rounds"]] == report["executed_rounds"] == [31, 32]
    assert all(r["status"] == "通过" and all(r["checks"].values()) for r in report["rounds"])
    assert set(report["accounting"]["by_round"]) == {"31", "32"}
    assert all(c["round"] in (31, 32) for c in report["calls"]) and report["total_calls"] == len(report["calls"]) > 0
    assert not any(s["round"] in (29, 30) for s in report["stages"])
    assert report["selection"]["requested_rounds"] == [31, 32]
    with (directory / "specialized_scores.csv").open(encoding="utf-8-sig", newline="") as f:
        assert [r["round"] for r in csv.DictReader(f)] == ["31", "32"]
    if fixture == "fake_real":
        assert report["verification_environment"] == "fake_sdk_offline"  # real=True only means the gated path.
        assert report["network_guard"]["allowed_endpoints"] == ["127.0.0.1:18995"]
        assert report["budget"]["calls"] == report["total_calls"]


def test_skipped_29_30_cite_registered_history_without_importing_it(mock_run):
    directory, report = mock_run
    r29, r30 = report["skipped_rounds"]
    assert (r29["n"], r30["n"]) == (29, 30)
    assert all(r["status"] == "未执行" and r["history_imported"] is False for r in (r29, r30))
    for r in (r29, r30):
        assert r["historical_evidence"]["sha256"] == digest(PROJECT.parent / r["historical_evidence"]["path"])
    assert r30["historical_evidence"]["sha256"] == tail.REGISTERED_SHA256[tail.ROUND30_REPORT]
    assert r30["historical_evidence"]["historical_status"] == "失败"  # Recorded as-is, never rewritten.
    assert r30["reassessment"]["assertion"] == tail.ANSWER_ASSERTION and r30["reassessment"]["status"] == "通过"
    assert r30["reassessment"]["sha256"] == digest(PROJECT.parent / tail.ROUND30_REASSESSMENT)
    # The historical round-30 reply is cited by hash only; it is never copied into this run.
    for path in directory.rglob("*"):
        if path.is_file() and path.suffix in (".json", ".csv"):
            assert ROUND30_REPLY not in path.read_text(encoding="utf-8-sig"), path
    assert "答辩你还记得" not in json.dumps(report["rounds"], ensure_ascii=False)


def test_fresh_database_seeds_only_old_major_and_32_uses_31_writes(mock_run):
    directory, report = mock_run
    initial = report["initial_state"]
    assert initial["messages"] == []
    majors = [(f["value"], f["status"]) for f in initial["facts"] if f["fact_key"] == "study.major"]
    assert majors == [("天文", "active")]
    assert not any("物理" in json.dumps(v, ensure_ascii=False) for v in initial.values())
    r31, r32 = report["rounds"]
    [new] = r31["correction_evidence"]["active"]
    assert new["value"] == "物理" and r31["correction_evidence"]["old"]["status"] == "superseded"
    assert r32["profile_context_evidence"]["active"] == [{"id": new["id"], "value": "物理"}]
    assert new["id"] in r32["profile_context_evidence"]["fact_ids"]
    with sqlite3.connect(directory / "violet-test.db") as db:
        assert db.execute("SELECT value FROM user_profile_facts WHERE fact_key='study.major' AND status='active'").fetchall() == [("物理",)]
        users = [r[0] for r in db.execute("SELECT content FROM messages WHERE role='user' ORDER BY id")]
    assert users == ["其实我不是学天文的，是学物理的", "我的专业是什么来着"]


@pytest.mark.parametrize("fault", ["correction_declined", "old_active", "old_not_superseded",
                                   "background_incomplete", "memory_failure"])
def test_failed_round31_stops_before_round32(fault):
    _, report = run_case("--real", fault=fault)
    assert report["status"] == "失败" and [r["status"] for r in report["rounds"]] == ["失败", "未执行"]
    assert report["rounds"][1]["calls"] == [] and all(c["round"] == 31 for c in report["calls"])


@pytest.mark.parametrize("fault", ["old_reply", "missing_context"])
def test_round32_old_major_fails(fault):
    _, report = run_case("--real", fault=fault)
    assert report["status"] == "失败" and [r["status"] for r in report["rounds"]] == ["通过", "失败"]


def test_cli_refuses_before_directory_or_model_when_registered_hash_differs():
    code = BOOTSTRAP.replace("args=build_parser()", 't.REGISTERED_SHA256={**t.REGISTERED_SHA256,t.ROUND30_REPORT:"0"*64}\nargs=build_parser()', 1)
    directory, result = run_case("--real", code=code, expect=1)
    assert "Round30 evidence refused" in result.stderr and "sha256 differs" in result.stderr
    assert not directory.exists()


@pytest.mark.parametrize("argv", [["--rounds", "31"], ["--rounds", "32"], ["--rounds", "32-31"],
                                  ["--rounds", "31-32", "--round30-pass"], ["--rounds", "31-32", "round30_pass=true"]])
def test_no_other_selection_or_manual_pass_flag(argv):
    directory = PROJECT.parent / "local/b1110/test-cases" / uuid.uuid4().hex
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-m", "tools.synthetic_rounds", "--suite", tail.PURPOSE,
        "--workdir", str(directory), *argv], cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 2 and not directory.exists()


def test_31_32_rejected_for_other_suite():
    directory = PROJECT.parent / "local/b1110/test-cases" / uuid.uuid4().hex
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-m", "tools.synthetic_rounds", "--suite",
        "round27_28_specialized_test", "--rounds", "31-32", "--workdir", str(directory)], cwd=PROJECT,
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode != 0 and not directory.exists()


def test_existing_selections_unchanged():
    assert tail.select_rounds(None) == (29, 30, 31, 32) and tail.select_rounds("29-32") == (29, 30, 31, 32)
    assert tail.select_rounds("30-32") == (30, 31, 32) and tail.select_rounds("31-32") == (31, 32)
    seed = tail.build_tail_seed()
    assert [s["n"] for s in tail.selection_evidence(seed, (30, 31, 32))["skipped_rounds"]] == [29]
    assert tail.selection_evidence(seed, (29, 30, 31, 32))["skipped_rounds"] == []


# --- Gate on copied evidence: every tamper is refused; registered hashes are re-pointed only to prove the
# content checks hold independently of the hash registry. ---

@pytest.fixture
def evidence(tmp_path, monkeypatch):
    (tmp_path / "violet").mkdir()
    for rel in tail.REGISTERED_SHA256:
        target = tmp_path / rel
        target.parent.mkdir(parents=True)
        shutil.copyfile(PROJECT.parent / rel, target)

    def tamper(rel, change, rehash=True):
        path = tmp_path / rel
        value = json.loads(path.read_text(encoding="utf-8"))
        change(value)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        if rehash:
            monkeypatch.setattr(tail, "REGISTERED_SHA256", {**tail.REGISTERED_SHA256, rel: digest(path)})

    def gate():
        return tail.round30_evidence(tail.build_tail_seed(), tmp_path / "violet")
    return tmp_path, tamper, gate


def test_gate_accepts_untouched_copy(evidence):
    _, _, gate = evidence
    entry = gate()
    assert entry["status"] == "未执行" and entry["reassessment"]["differences"] == tail.ROUND30_EXPECTED_DIFF


def _row(value):
    return next(r for r in value["rounds"] if r["n"] == 30)


REPORT, REASSESS = tail.ROUND30_REPORT, tail.ROUND30_REASSESSMENT


@pytest.mark.parametrize("rel,change,message", [
    (REPORT, lambda v: v.update(real=False), "not a real run"),
    (REPORT, lambda v: v.update(purpose="round27_28_specialized_test"), "not a real run"),
    (REPORT, lambda v: _row(v).update(user="别的问题"), "user input"),
    (REPORT, lambda v: _row(v)["calls"][0].update(ok=False), "model calls"),
    (REPORT, lambda v: _row(v)["checks"].update(correct_event_in_chat=False), "structural"),
    (REPORT, lambda v: _row(v)["checks"].update(answer_recalls_outcome=True), "exactly the v1"),
    (REPORT, lambda v: v.update(rounds=[]), "row missing"),
    (REASSESS, lambda v: v["assertion"].update(new="round30.answer_recalls_outcome/v1"), "did not use"),
    (REASSESS, lambda v: v.update(kind="manual_pass"), "post-hoc"),
    (REASSESS, lambda v: v.update(model_calls=3), "post-hoc"),
    (REASSESS, lambda v: v["eve_head"].update(tools_uncommitted=True), "not committed"),   # the stored (old) name
    (REASSESS, lambda v: v["source"].update(dir="D:/elsewhere/real-x"), "source is not"),
    (REASSESS, lambda v: v["source"]["sha256"].update({"specialized_report.json": "0" * 64}), "source is not"),
    (REASSESS, lambda v: v["round30"].update(differences=[]), "expected one"),
    (REASSESS, lambda v: v["round30"].update(status="无法验证"), "expected one"),
    (REASSESS, lambda v: v["rounds_31_32"][0].update(reassessed=True), "claims rounds"),
    (REASSESS, lambda v: v["round30"]["reassessed_checks"].update(intent_queried=False), "does not reproduce")])
def test_gate_rejects_tampered_content_even_with_matching_registry(evidence, rel, change, message):
    _, tamper, gate = evidence
    tamper(rel, change)
    with pytest.raises(ValueError, match="Round30 evidence refused.*" + message):
        gate()


@pytest.mark.parametrize("rel", [REPORT, REASSESS])
def test_gate_rejects_changed_bytes_and_missing_files(evidence, rel):
    root, tamper, gate = evidence
    tamper(rel, lambda v: v.update(note="edited"), rehash=False)
    with pytest.raises(ValueError, match="sha256 differs from registered value: " + rel):
        gate()
    (root / rel).unlink()
    with pytest.raises(ValueError, match="missing " + rel):
        gate()
