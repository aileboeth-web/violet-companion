"""Phase 8.1: chat-experience evaluation set, blind sheets and scoring. Offline; real reports are only read."""
import copy
import csv
import json
import os
import subprocess
import sys

import pytest

from tools import chat_eval as ce
from tools.legacy import upgrade

SET_FILE = ce.ROOT / ce.SET_PATH
needs_sources = pytest.mark.skipif(not all((ce.ROOT / item[1]).is_file() for item in ce.REAL_SELECTION),
                                   reason="local real reports are not in this checkout")


@pytest.fixture(scope="module")
def frozen():
    return ce.load_set()


@needs_sources
def test_build_reproduces_committed_set_byte_for_byte():
    # the committed set was written before the rename to Violet: it is rebuilt exactly, in today's names (the name in
    # free text is compared as one name on both sides; everything else must be identical)
    def one_name(value):
        import re
        text = json.dumps(value, ensure_ascii=False).replace("Project Eve/", "violet/").replace("Eve", "Violet")
        return json.loads(re.sub(r"(?<![a-z])eve_", "violet_", text))
    rebuilt = one_name(json.loads(ce.dump_set(ce.build_set())))
    assert rebuilt == one_name(upgrade(json.loads(SET_FILE.read_text(encoding="utf-8"))))
    assert b"\r\n" not in SET_FILE.read_bytes()       # its sha256 must survive Git line-ending normalisation


@needs_sources
def test_every_cited_source_still_matches(frozen):
    scenario_set, _ = frozen
    assert ce.verify(scenario_set) == []


def test_verify_reports_changed_or_missing_sources(frozen):
    scenario_set = copy.deepcopy(frozen[0])
    first = scenario_set["scenarios"][0]
    rel = next(iter(first["source_sha256"]))
    first["source_sha256"][rel] = "0" * 64
    first["source_sha256"]["local/missing/report.json"] = "1" * 64
    problems = ce.verify(scenario_set)
    assert f"{first['id']}: sha256 differs {rel}" in problems
    assert f"{first['id']}: missing local/missing/report.json" in problems


def test_composition_real_vs_synthetic(frozen):
    scenarios = frozen[0]["scenarios"]
    real = [s for s in scenarios if s["kind"] == "real"]
    synthetic = [s for s in scenarios if s["kind"] == "synthetic"]
    assert len(real) == len(ce.REAL_SELECTION) >= 30 and len(synthetic) >= 15
    assert all(s["reference_reply"] is None and s["source"]["path"] == ce.PROBES_PATH for s in synthetic)
    assert all(s["source"]["path"] != ce.PROBES_PATH and ce.PROBES_PATH not in s["source_sha256"] for s in real)
    covered = {t for s in scenarios for t in s["tags"]}
    for tag in ("short_reply", "silence", "joke", "casual", "topic_switch", "continuation", "long_gap", "intimacy",
                "boundary", "memory_recall", "memory_miss", "correction", "no_life_event", "venting", "help", "good_news"):
        assert tag in covered, tag


@needs_sources
def test_real_scenarios_quote_the_report_exactly(frozen):
    for s in frozen[0]["scenarios"]:
        if s["kind"] != "real":
            continue
        report = upgrade(json.loads((ce.ROOT / s["source"]["path"]).read_text(encoding="utf-8")))
        row = report["rounds"][int(s["source"]["pointer"].split("/")[-1])]
        assert row["n"] == s["source"]["round"] and row["user"] == s["user"] and row.get("violet") == s["reference_reply"]


@needs_sources
def test_history_never_invents_eve_replies(frozen):
    known = set()
    for item in ce.REAL_SELECTION:
        for rel in [item[1], *item[4]]:
            for r in upgrade(json.loads((ce.ROOT / rel).read_text(encoding="utf-8"))).get("rounds", []):
                known.add("\n".join(r.get("violet") or []))
                known.update(t for t in ((r.get("reminder") or {}).get("sent") or []))
    for s in frozen[0]["scenarios"]:
        if s["kind"] != "real":
            continue
        for t in s["history"]:
            if t["role"] == "violet" and t["source"] in ("same_report", "same_report:proactive_reminder") or t["source"].startswith("replay_source:"):
                assert t["role"] == "user" or t["text"] in known, (s["id"], t)
            if t["source"] == "unknown_lost_test_directory":
                assert t["text"] is None
    by_id = {s["id"]: s for s in frozen[0]["scenarios"]}
    assert any(t["source"] == "plan_user_line" for t in by_id["R14"]["history"])
    assert any("已丢失的测试目录" in m for m in by_id["R14"]["missing"])
    assert all(t["source"].startswith("replay_source:") for t in by_id["R06"]["history"])
    assert by_id["R28"]["history"][-1]["source"] == "same_report:proactive_reminder"
    assert by_id["R21"]["reference_reply"] == [] and by_id["R21"]["reference_media"] == [{"kind": "image", "ai_generated": True}]


def test_probe_with_reference_reply_is_refused(monkeypatch):
    original = ce._load

    def fake(rel, root):
        value = original(rel, root)
        if rel == ce.PROBES_PATH:
            value = copy.deepcopy(value)
            value["probes"][0]["reference_reply"] = ["编的"]
        return value
    monkeypatch.setattr(ce, "_load", fake)
    monkeypatch.setattr(ce, "REAL_SELECTION", [])
    with pytest.raises(ValueError, match="must not carry a reference reply"):
        ce.build_set()


def test_baseline_variant_holds_only_real_replies(frozen):
    baseline = ce.baseline_variant(frozen[0])
    kinds = {s["id"]: s["kind"] for s in frozen[0]["scenarios"]}
    assert baseline["replies"] and all(kinds[sid] == "real" for sid in baseline["replies"])
    assert baseline["replies"]["R21"] == [[ce.MEDIA_LINE]]


def _variant(tmp_path, set_sha, **override):
    value = {"variant": "trial", "set_sha256": set_sha, "environment": "mock", "model": "none",
             "replies": {"R08": [["好吧不跟你争了", "反正我还会接着看的"]], "R17": [["哈哈哈食堂就是开盲盒"]], "P01": [["哈哈"]]}}
    value.update(override)
    path = tmp_path / f"{value['variant']}.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("override,message", [
    ({"variant": ce.BASELINE}, "reserved"), ({"set_sha256": "0" * 64}, "different scenario set"),
    ({"environment": "guess"}, "environment"), ({"replies": {"NOPE": [["x"]]}}, "unknown scenario"),
    ({"replies": {"R08": ["not a list of lines"]}}, "list of samples")])
def test_variant_files_are_validated(tmp_path, frozen, override, message):
    with pytest.raises(ValueError, match=message):
        ce.load_variant(_variant(tmp_path, frozen[1], **override), *frozen)


def _sheet(tmp_path, frozen, seed=3):
    trial = ce.load_variant(_variant(tmp_path, frozen[1]), *frozen)
    return ce.blind(frozen[0], frozen[1], [ce.baseline_variant(frozen[0]), trial], seed, {"R08", "R17", "P01"})


def test_blind_sheet_hides_variants_and_is_reproducible(tmp_path, frozen):
    rows, md, key = _sheet(tmp_path, frozen)
    assert key["skipped"] == {"P01": [ce.BASELINE]}            # probes have no historical reply
    assert {i["scenario"] for i in key["items"].values()} == {"R08", "R17"}
    for secret in ("trial", ce.BASELINE, "mock", "variant"):
        assert secret not in md and all(secret not in json.dumps(r, ensure_ascii=False) for r in rows)
    assert all({c["variant"] for c in i["candidates"].values()} == {"trial", ce.BASELINE} for i in key["items"].values())
    again = _sheet(tmp_path / "again", frozen) if (tmp_path / "again").mkdir() is None else None
    assert again[0] == rows and again[2] == key
    orders = {json.dumps(_sheet(tmp_path / f"s{s}", frozen, s)[2]["items"], sort_keys=True)
              for s in range(8) if (tmp_path / f"s{s}").mkdir() is None}
    assert len(orders) > 1
    out = tmp_path / "out"
    ce.write_blind(out, rows, md, key)
    with pytest.raises(FileExistsError):
        ce.write_blind(out, rows, md, key)


def _fill(rows, key, override=None):
    filled = []
    for row in rows:
        cand = key["items"][row["item"]]["candidates"][row["candidate"]]
        good = cand["variant"] == "trial"
        r = {**row, "natural": "2" if good else "1", "context": "2", "grounded": "2", "rhythm": "2" if good else "0",
             "self": "1", "intimacy": "na", "respect": "na", "rank": "1" if good else "2",
             "reason": "更像随口接话" if good else "句子偏长、像在总结", "rater": "r1"}
        filled.append(r)
    for (item, letter), changes in (override or {}).items():
        for r in filled:
            if r["item"] == item and r["candidate"] == letter:
                r.update(changes)
    return filled


def test_score_unblinds_and_keeps_reasons(tmp_path, frozen):
    rows, _, key = _sheet(tmp_path, frozen)
    result = ce.score(_fill(rows, key), key, *frozen)
    assert result["by_variant"]["trial"]["rhythm"] == {"mean": 2, "n": 2}
    assert result["by_variant"][ce.BASELINE]["rhythm"] == {"mean": 0, "n": 2}
    assert "intimacy" not in result["by_variant"]["trial"]           # na is excluded, not counted as 0
    assert result["pairwise_preference"] == {f"{ce.BASELINE} vs trial": {"wins": 0, "losses": 2}}
    assert all(r["reason"] for r in result["rated_rows"]) and result["variants"]["trial"]["environment"] == "mock"


def test_grounded_issue_requires_and_keeps_evidence(tmp_path, frozen):
    rows, _, key = _sheet(tmp_path, frozen)
    item, letter = rows[0]["item"], rows[0]["candidate"]
    with pytest.raises(ValueError, match="quoted evidence"):
        ce.score(_fill(rows, key, {(item, letter): {"grounded": "0"}}), key, *frozen)
    result = ce.score(_fill(rows, key, {(item, letter): {"grounded": "0", "grounded_evidence": "“刚才那个资料”"}}), key, *frozen)
    assert result["grounded_issues"][0]["evidence"] == "“刚才那个资料”"


@pytest.mark.parametrize("change,message", [
    ({"reason": " "}, "reason is required"), ({"natural": "3"}, "expected 0/1/2"), ({"natural": "na"}, "expected 0/1/2"),
    ({"candidate_sha12": "000000000000"}, "changed after blinding"), ({"scenario": "R01"}, "not in key"),
    ({"rank": "1"}, "rank every candidate")])
def test_score_rejects_bad_rows(tmp_path, frozen, change, message):
    rows, _, key = _sheet(tmp_path, frozen)
    target = next(r for r in rows if key["items"][r["item"]]["candidates"][r["candidate"]]["variant"] == ce.BASELINE)
    with pytest.raises(ValueError, match=message):
        ce.score(_fill(rows, key, {(target["item"], target["candidate"]): change}), key, *frozen)


def test_score_rejects_duplicates_and_foreign_keys(tmp_path, frozen):
    rows, _, key = _sheet(tmp_path, frozen)
    filled = _fill(rows, key)
    with pytest.raises(ValueError, match="duplicate"):
        ce.score(filled + [filled[0]], key, *frozen)
    with pytest.raises(ValueError, match="different scenario set"):
        ce.score(filled, {**key, "set_sha256": "0" * 64}, *frozen)


def test_diagnostics_are_marked_diagnostic_only(frozen):
    result = ce.diagnose(frozen[0], [ce.baseline_variant(frozen[0])])
    assert result["diagnostic_only"] is True and "不是优化目标" in result["notice"]
    base = result["variants"][ce.BASELINE]
    assert base["replies"] == len(ce.baseline_variant(frozen[0])["replies"]) and 0 <= base["name_rate"] <= 1


def test_tool_imports_no_model_network_or_runtime_module():
    code = "import sys, tools.chat_eval; print([m for m in sys.modules if m.split('.')[0] in ('openai','httpx','requests','ai')])"
    out = subprocess.run([sys.executable, "-B", "-c", code], cwd=ce.PROJECT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "[]", out.stderr


@needs_sources
def test_cli_round_trip_and_refusals(tmp_path):
    run = lambda *a: subprocess.run([sys.executable, "-B", "-X", "utf8", "-m", "tools.chat_eval", *a], cwd=ce.PROJECT,
                                    env=dict(os.environ, PYTHONIOENCODING="utf-8"), capture_output=True, text=True,
                                    encoding="utf-8", timeout=120)
    assert run("verify").returncode == 0
    out = tmp_path / "sheet"
    assert run("blind", "--variant", ce.BASELINE, "--seed", "1", "--out", str(out)).returncode == 0
    assert run("blind", "--variant", ce.BASELINE, "--seed", "1", "--out", str(out)).returncode != 0   # never overwrite
    with (out / "sheet.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r.update(natural="1", context="1", grounded="2", rhythm="1", self="na", intimacy="na", respect="na",
                 reason="基线", rater="r")
    filled = tmp_path / "filled.csv"
    with filled.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ce.SHEET_FIELDS)
        w.writeheader()
        w.writerows(rows)
    result = tmp_path / "result.json"
    assert run("score", "--sheet", str(filled), "--key", str(out / "key.json"), "--out", str(result)).returncode == 0
    assert json.loads(result.read_text(encoding="utf-8"))["by_variant"][ce.BASELINE]["natural"]["n"] == len(rows)
    tampered = tmp_path / "tampered.json"
    value = json.loads(SET_FILE.read_text(encoding="utf-8"))
    value["plan"]["sha256"] = "0" * 64
    tampered.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    assert run("verify", "--set", str(tampered)).returncode == 1
    refused = run("blind", "--set", str(tampered), "--variant", ce.BASELINE, "--seed", "1", "--out", str(tmp_path / "x"))
    assert refused.returncode != 0 and not (tmp_path / "x").exists()
