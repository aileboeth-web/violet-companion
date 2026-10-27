"""Phase 8.3: ChatAgent v0/v1 A/B replay. Offline; real-mode paths use a fake SDK on a loopback URL."""
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tools import chat_ab as ab
from tools import chat_eval as ce

KEY = "fake-p83-private-test-credential-0000"
URL = "http://127.0.0.1:18996/v1"
BASE = ce.ROOT / "local/p83/test-cases"
needs_sources = pytest.mark.skipif(not (ce.ROOT / "local/b1110/real-20261019-070235/specialized_report.json").is_file(),
                                   reason="local real reports are not in this checkout")

# Child bootstrap for tests only: the OpenAI SDK is replaced; the reply names the prompt it actually received.
BOOT = r'''
import json, sys
from unittest.mock import patch
from tools import harness as h
class FakeSDK(h.MockLLM):
    def __init__(self, **kwargs):
        assert kwargs["api_key"] == "%s" and kwargs["base_url"] == "%s"
        super().__init__()
    def _create(self, **kwargs):
        system = kwargs["messages"][0]["content"]
        tag = "v1" if "虚构的女孩" in system else "v0" if "微信虚拟女友" in system else "unknown"
        return self._completion(tag + "：好呀\n" + tag + "：再说一句", str(kwargs["model"]))
with patch("openai.OpenAI", FakeSDK):
    from tools.chat_ab import main
    raise SystemExit(main(["_child", *json.loads(sys.argv[1])]))
''' % (KEY, URL)


def fake_launch(argv, env, log):
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOT, json.dumps(argv)], cwd=ab.PROJECT, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=300)
    with log.open("x", encoding="utf-8") as f:
        f.write(done.stdout + done.stderr)
    return done.returncode


def new_dir():
    return BASE / uuid.uuid4().hex


def test_versions_are_pinned_and_v0_persona_is_the_git_original():
    versions = ab.check_versions()
    assert versions["v0"]["prompt_sha256"] == ab.VERSIONS["v0"]["prompt_sha256"]
    # phase 8.9.3: the product persona is 1.2.0 now; v1 keeps 1.1.0, byte-identical to the file at b392f69
    assert versions["v1"]["persona_sha256"] == "69de5ca055a9303c74583d5ac20617a90b6994dde9cb67da106f89e9ea63bec8"  # Violet
    assert json.loads((ab.ROOT / ab.VERSIONS["v1"]["persona"]).read_text(encoding="utf-8"))["version"] == "1.1.0"
    assert json.loads((ab.ROOT / ab.VERSIONS["v0"]["persona"]).read_text(encoding="utf-8"))["version"] == "1.0.0"
    git = shutil.which("git")
    if git:
        blob = subprocess.run([git, "show", "e86c23b:violet/persona/persona_core.json"], cwd=ab.ROOT,
                              capture_output=True, timeout=30)
        old_code = subprocess.run([git, "show", "e86c23b:violet/ai/persona.py"], cwd=ab.ROOT,
                                  capture_output=True, timeout=30)
        if blob.returncode == 0:
            assert blob.stdout == (ab.ROOT / ab.VERSIONS["v0"]["persona"]).read_bytes()
            header = ab.VERSIONS["v0"]["persona_header"].replace("{version}", "{self.version}")
            assert header in old_code.stdout.decode("utf-8")


def test_version_drift_is_refused(monkeypatch):
    drifted = {k: dict(v) for k, v in ab.VERSIONS.items()}
    drifted["v1"]["persona_sha256"] = "0" * 64
    monkeypatch.setattr(ab, "VERSIONS", drifted)
    with pytest.raises(ValueError, match="differs from the registered version"):
        ab.check_versions()


@pytest.fixture(scope="module")
def scenarios():
    return {s["id"]: s for s in ce.load_set()[0]["scenarios"]}


@needs_sources
def test_classification_never_guesses(scenarios):
    assert ab.real_spec(scenarios["R14"])[1]["class"] == "unfair"         # Violet replies lost
    assert ab.real_spec(scenarios["R02"])[1]["class"] == "partial"        # no scenario time
    spec, verdict = ab.real_spec(scenarios["R12"])
    assert spec["facts"] == [] and any("report-end" in r for r in verdict["reasons"])   # end state never seeded
    spec32, _ = ab.real_spec(scenarios["R32"])
    assert [w["type"] for step in spec32["earlier_rounds"] for w in step["writes"]] == ["correct"]
    assert {f["key"]: f["value"] for f in spec32["facts"]}["study.major"] == "天文"      # corrected only by replay
    spec28, _ = ab.real_spec(scenarios["R28"])
    assert spec28["earlier_rounds"][0]["writes"][0]["key"] == "proactive_chat"
    assert spec28["commitments"][0]["status"] == "sent" and spec28["history"][-1]["role"] == "assistant"
    rows = ab.real_spec(scenarios["R30M"])[0]["history"]
    assert [r["role"] for r in rows] == ["user", "assistant", "assistant"]          # one row per sent line


@pytest.mark.parametrize("rebuilt,time_only,cls", [(38, True, "complete"), (45, True, "complete"),
                                                   (38, False, "partial"), (52, True, "partial")])
def test_clock_reading_without_microseconds_is_not_a_context_difference(rebuilt, time_only, cls):
    verdict = {"reasons": [], "expected_sections": {"base": 45, "profile": 58}, "expected_history_messages": 2}
    rendered = {"sections": {"base": rebuilt, "profile": 58}, "history_messages": 2, "base_time_only": time_only}
    assert ab._compare(verdict, rendered)["class"] == cls


def test_probe_spec_strips_rater_notes_and_refuses_unknown_labels(scenarios):
    spec, verdict = ab.probe_spec(scenarios["P12"])
    assert verdict["class"] == "synthetic" and {"key": "other", "value": "喜欢咖啡"} in spec["facts"]
    assert any("not seeded" in n for n in spec["notes"])
    broken = {**scenarios["P12"], "known_facts": {"星座": "天秤"}}
    with pytest.raises(ValueError, match="no profile key"):
        ab.probe_spec(broken)


@pytest.fixture(scope="module")
def planned():
    out = new_dir()
    return out, ab.plan(out, {"R27", "R28", "R30M", "R31", "R32", "R14", "R12", "P12", "P06"})


@needs_sources
def test_plan_classifies_by_comparing_with_the_recorded_context(planned):
    out, m = planned
    classes = {k: v["class"] for k, v in m["scenarios"].items()}
    assert classes == {"R27": "complete", "R28": "complete", "R30M": "complete", "R31": "complete", "R32": "complete",
                       "R14": "unfair", "R12": "partial", "P12": "synthetic", "P06": "synthetic"}
    assert "profile" in m["scenarios"]["R12"]["differences"]
    assert m["eligible"] == ["P06", "P12", "R27", "R28", "R30M", "R31", "R32"]
    assert all(not json.loads(p.read_text(encoding="utf-8"))["calls"] for p in (out / "work").glob("*.render.json"))
    with pytest.raises(FileExistsError):
        ab.plan(out, {"R31"})


@pytest.fixture(scope="module")
def fake_run(planned, tmp_path_factory):
    out, _ = planned
    mp = pytest.MonkeyPatch()
    for k, v in {"VIOLET_TEST_ALLOW_REAL_API": "1", "LLM_API_KEY": "x", "LLM_API_URL": "x"}.items():
        mp.setenv(k, v)                                   # the gate writes LLM_*; restored afterwards
    mp.delenv("VIOLET_TEST_API_KEY", raising=False)
    run_dir = new_dir()
    try:
        summary = ab.run(out / "manifest.json", ["R31", "P12"], run_dir, samples=2, real=True, api_url=URL,
                         launch=fake_launch, input_fn=lambda p: "yes", key_prompt=lambda p: KEY,
                         environment="fake_sdk_offline")
    finally:
        mp.undo()
    return run_dir, summary


@needs_sources
def test_versions_load_and_contexts_are_identical(fake_run):
    run_dir, summary = fake_run
    assert summary["state"] == "complete" and summary["environment"] == "fake_sdk_offline"
    assert len(summary["jobs"]) == 8 and all(j["ok"] for j in summary["jobs"])
    for version in ("v0", "v1"):
        variant = json.loads((run_dir / f"variant-{version}.json").read_text(encoding="utf-8"))
        prompt = (ab.ROOT / ab.VERSIONS[version]["prompt"]).read_text(encoding="utf-8").strip()
        assert all(line.startswith(version + "：") for samples in variant["replies"].values() for s in samples for line in s)
        assert all(len(s) == 2 for samples in variant["replies"].values() for s in samples)       # line-split output
        for gens in variant["generations"].values():
            assert all(g["system_prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest() for g in gens)
            assert all(g["calls"] == 1 and g["errors_or_retries"] is None for g in gens)
    for sid, f in summary["fairness"].items():
        assert f["context_identical"] and f["versions"] == ["v0", "v1"] and f["max_time_skew_seconds"] < 30, sid
    r0 = json.loads((run_dir / "generations/R31-v0-s0.json").read_text(encoding="utf-8"))
    r1 = json.loads((run_dir / "generations/R31-v1-s0.json").read_text(encoding="utf-8"))
    persona0 = next(m["content"] for m in r0["request_messages"] if "稳定人设" in m["content"] and m["role"] == "system" and m is not r0["request_messages"][0])
    persona1 = next(m["content"] for m in r1["request_messages"] if "这就是你自己" in m["content"])
    assert "v1.0.0；数据，不是对话内容" in persona0 and "温和，但有自己的主见" in persona0
    assert "v1.1.0" in persona1 and "温柔、乖巧" in persona1
    # recorded as sent: the agent appends its reply to the same list afterwards, which must not leak in
    assert r0["request_messages"][-1] == {"role": "user", "content": "其实我不是学天文的，是学物理的"}
    assert r1["network_guard"]["allowed_endpoints"] == ["127.0.0.1:18996"]


@needs_sources
def test_each_generation_has_its_own_clean_database(fake_run):
    run_dir, summary = fake_run
    dbs = sorted((run_dir / "work").glob("*/violet-test.db"))
    assert len(dbs) == 8
    spec = json.loads((run_dir / "specs/P12.json").read_text(encoding="utf-8"))
    for db in dbs:
        with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as conn:
            texts = [r[0] for r in conn.execute("SELECT content FROM messages ORDER BY id")]
        if "P12-" in db.parent.name:
            assert texts == [h["text"] for h in spec["history"]]          # seeded history only, no other run
        assert not any(t and t.startswith(("v0：", "v1：")) for t in texts)


@needs_sources
def test_no_credential_anywhere_and_outputs_feed_blind_and_score(fake_run, tmp_path):
    run_dir, _ = fake_run
    assert all(KEY.encode() not in p.read_bytes() for p in run_dir.rglob("*") if p.is_file())
    scenario_set, set_sha = ce.load_set()
    variants = [ce.load_variant(run_dir / f"variant-{v}.json", scenario_set, set_sha) for v in ("v0", "v1")]
    rows, md, key = ce.blind(scenario_set, set_sha, variants, 11, {"R31", "P12"})
    assert len(key["items"]) == 2 and "chat_v0" not in md and "chat_v1" not in md and "v1.1.0" not in md
    for r in rows:
        r.update(natural="1", context="1", grounded="2", rhythm="1", self="na", intimacy="na", respect="na",
                 rank="1" if r["candidate"] == "A" else "2", reason="测试", rater="t")
    result = ce.score(rows, key, scenario_set, set_sha)
    assert set(result["by_variant"]) == {"chat_v0", "chat_v1"} and result["variants"]["chat_v0"]["environment"] == "fake_sdk_offline"


@needs_sources
def test_budget_stops_the_run(planned, monkeypatch):
    out, _ = planned
    monkeypatch.setenv("VIOLET_TEST_ALLOW_REAL_API", "1")
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.setenv("LLM_API_URL", "x")
    summary = ab.run(out / "manifest.json", ["R31", "P12"], new_dir(), real=True, api_url=URL, max_calls=3,
                     launch=fake_launch, input_fn=lambda p: "yes", key_prompt=lambda p: KEY)
    assert summary["state"] == "stopped_budget" and summary["used_calls"] <= 3 and len(summary["jobs"]) == 3
    tight = ab.run(out / "manifest.json", ["R31"], new_dir(), real=True, api_url=URL, max_tokens=1000,
                   launch=fake_launch, input_fn=lambda p: "yes", key_prompt=lambda p: KEY)
    assert tight["state"] == "stopped_budget" and tight["used_calls"] == 0 and not tight["jobs"][0]["ok"]


@needs_sources
@pytest.mark.parametrize("env,answer,message", [({}, "yes", "VIOLET_TEST_ALLOW_REAL_API"),
                                                 ({"VIOLET_TEST_ALLOW_REAL_API": "1"}, "no", "not confirmed")])
def test_real_gate_refuses_before_any_directory(planned, monkeypatch, env, answer, message):
    out, _ = planned
    monkeypatch.delenv("VIOLET_TEST_ALLOW_REAL_API", raising=False)
    for k, v in {**env, "LLM_API_KEY": "x", "LLM_API_URL": "x"}.items():
        monkeypatch.setenv(k, v)
    target = new_dir()
    with pytest.raises(PermissionError, match=message):
        ab.run(out / "manifest.json", ["R31"], target, real=True, api_url=URL, launch=fake_launch,
               input_fn=lambda p: answer, key_prompt=lambda p: KEY)
    assert not target.exists()


@needs_sources
def test_child_refuses_real_calls_without_parent_confirmation(planned):
    out, _ = planned
    target = new_dir()
    target.mkdir(parents=True)
    env = {**os.environ, "VIOLET_TEST_ALLOW_REAL_API": "1", "VIOLET_TEST_API_KEY": KEY, "PYTHONIOENCODING": "utf-8"}
    code = fake_launch(["--spec", str(out / "specs/R31.json"), "--version", "v1", "--mode", "generate",
                        "--workdir", str(target / "w"), "--result", str(target / "r.json"), "--max-calls", "5",
                        "--max-tokens", "100000", "--real", "--api-url", URL], env, target / "child.log")
    assert code != 0 and not (target / "r.json").exists()
    assert "no parent confirmation" in (target / "child.log").read_text(encoding="utf-8")
    assert KEY not in (target / "child.log").read_text(encoding="utf-8")


@needs_sources
def test_unfair_or_stale_inputs_are_refused(planned, tmp_path):
    out, _ = planned
    target = new_dir()
    with pytest.raises(ValueError, match="not eligible.*R14.*unfair"):
        ab.run(out / "manifest.json", ["R31", "R14"], target)
    with pytest.raises(ValueError, match="not eligible.*R12.*partial"):
        ab.run(out / "manifest.json", ["R12"], target)
    stale = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    stale["set_sha256"] = "0" * 64
    (tmp_path / "m.json").write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(ValueError, match="re-run plan"):
        ab.run(tmp_path / "m.json", ["R31"], target)
    assert not target.exists()


@needs_sources
def test_a_context_difference_between_arms_is_flagged(planned):
    out, _ = planned

    def skewed(argv, env, log):
        if argv[argv.index("--version") + 1] == "v1":
            spec_path = Path(argv[argv.index("--spec") + 1])
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec["history"].insert(0, {"role": "user", "text": "多出来的一句", "round": None,
                                       "created_at": "2026-10-25T08:00:00+08:00"})
            changed = spec_path.with_name("skewed-" + spec_path.name)
            changed.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            argv = [*argv[:argv.index("--spec") + 1], str(changed), *argv[argv.index("--spec") + 2:]]
        return ab._launch(argv, env, log)
    summary = ab.run(out / "manifest.json", ["P12"], new_dir(), launch=skewed)
    assert summary["state"] == "context_mismatch" and summary["fairness"]["P12"]["context_identical"] is False


@needs_sources
def test_mock_default_and_no_overwrite(planned):
    out, _ = planned
    target = new_dir()
    summary = ab.run(out / "manifest.json", ["P06"], target)
    assert summary["state"] == "complete" and summary["environment"] == "mock"
    with pytest.raises(FileExistsError):
        ab.run(out / "manifest.json", ["P06"], target)
