"""Phase 8.5: legacy vs natural ChatAgent context A/B. Offline; real-mode paths use a fake SDK on a loopback URL."""
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tools import chat_ab as ab
from tools import chat_eval as ce

KEY = "fake-p85-private-test-credential-0000"
URL = "http://127.0.0.1:18995/v1"
BASE = ce.ROOT / "local/p85/test-cases"
FOUR = ["P03", "P08", "P14", "P17"]

# Child bootstrap for tests only: the OpenAI SDK is replaced; the reply says which context it was given.
BOOT = r'''
import json, sys
from unittest.mock import patch
from tools import harness as h
class FakeSDK(h.MockLLM):
    def __init__(self, **kwargs):
        assert kwargs["api_key"] == "%s" and kwargs["base_url"] == "%s"
        super().__init__()
    def _create(self, **kwargs):
        system = "\n".join(m["content"] for m in kwargs["messages"] if m["role"] == "system")
        tag = "带时间" if "【以上是之前的聊天" in system else "无时间"
        return self._completion(tag + "：好呀", str(kwargs["model"]))
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


def requests_of(run_dir, name):
    return json.loads((run_dir / f"generations/{name}.json").read_text(encoding="utf-8"))["request_messages"]


def system_text(request):
    return "\n".join(m["content"] for m in request if m["role"] == "system")


@pytest.fixture
def real_env(monkeypatch):
    for k, v in {"VIOLET_TEST_ALLOW_REAL_API": "1", "LLM_API_KEY": "x", "LLM_API_URL": "x"}.items():
        monkeypatch.setenv(k, v)                          # the gate writes LLM_*; restored afterwards
    monkeypatch.delenv("VIOLET_TEST_API_KEY", raising=False)


def test_arms_keep_the_p83_names_and_vary_one_factor():
    assert [a["variant"] for a in ab.resolve_arms()] == ["chat_v0", "chat_v1"]
    assert [a["arm"] for a in ab.resolve_arms(context_style="natural")] == ["v0", "v1"]
    arms = ab.resolve_arms(arms=["v1:legacy", "v1:natural"])
    assert [(a["arm"], a["variant"], a["version"], a["context_style"]) for a in arms] == [
        ("v1-legacy", "chat_v1_legacy", "v1", "legacy"), ("v1-natural", "chat_v1_natural", "v1", "natural")]
    with pytest.raises(ValueError, match="one factor at a time"):
        ab.resolve_arms(arms=["v0:legacy", "v1:natural"])
    with pytest.raises(ValueError, match="distinct"):
        ab.resolve_arms(arms=["v1:natural", "v1:natural"])
    for bad in (["v2:legacy"], ["v1"], ["v1:fancy"]):
        with pytest.raises(ValueError):
            ab.resolve_arms(arms=bad)


@pytest.fixture(scope="module")
def planned():
    out = new_dir()
    manifest = ab.plan(out, set(FOUR) | {"P07"})
    assert set(FOUR) <= set(manifest["eligible"])
    return out / "manifest.json"


@pytest.fixture(scope="module")
def style_run(planned):
    out = new_dir()
    return out, ab.run(planned, FOUR, out, samples=2, arms=["v1:legacy", "v1:natural"], familiar_relationship="omit")


def test_style_run_differs_only_in_the_context_rendering(style_run):
    out, s = style_run
    assert s["state"] == "complete" and s["compared"] == "context_style" and s["context_style"] == "compared"
    assert s["planned_generations"] == 16 and len(s["jobs"]) == 16 and s["not_run"] == [] and s["failed_jobs"] == []
    assert s["variants"] == {"v1-legacy": "variant-v1-legacy.json", "v1-natural": "variant-v1-natural.json"}
    for sid in FOUR:
        f = s["fairness"][sid]
        assert f["context_identical"] is True and f["versions"] == ["v1"] and f["generations"] == 4, sid
        assert f["within_arm_identical"] == {"v1-legacy": True, "v1-natural": True}
        assert all(f["shared_inputs_identical"].values()) and f["limitations"], sid       # all four are 'familiar'
    legacy, natural = requests_of(out, "P03-v1-legacy-s0"), requests_of(out, "P03-v1-natural-s1")
    assert legacy[0] == natural[0]                                      # the same Chat v1 system prompt
    assert legacy[-1] == natural[-1] == {"role": "user", "content": "在吗"}
    assert "【以上是之前的聊天" in system_text(natural) and "【以上是之前的聊天" not in system_text(legacy)
    for sid in FOUR:                                                    # omitted, and nothing put in its place
        assert "【你们的相处" not in system_text(requests_of(out, f"{sid}-v1-natural-s0"))
        assert "这是你们第一次聊天" not in system_text(requests_of(out, f"{sid}-v1-natural-s0"))
    assert {j["relationship_line"] for j in s["jobs"] if j["context_style"] == "natural"} == {"omitted"}
    assert {j["relationship_line"] for j in s["jobs"] if j["context_style"] == "legacy"} == {"product"}
    assert (out / "rater_focus.md").read_text(encoding="utf-8").count("\n- ") == 6
    for name in ("v1-legacy", "v1-natural"):
        v = json.loads((out / f"variant-{name}.json").read_text(encoding="utf-8"))
        assert v["prompt_sha256"] == ab.VERSIONS["v1"]["prompt_sha256"] and v["context_style"] == name[3:]
        assert sorted(v["replies"]) == FOUR and all(len(x) == 2 for x in v["replies"].values())


def test_style_variants_feed_blind_and_score_without_revealing_the_arm(style_run):
    out, _ = style_run
    scenario_set, set_sha = ce.load_set()
    variants = [ce.load_variant(out / f"variant-{a}.json", scenario_set, set_sha) for a in ("v1-legacy", "v1-natural")]
    rows, md, key = ce.blind(scenario_set, set_sha, variants, 85, set(FOUR), all_samples=True)
    assert len(key["items"]) == 8 and key["skipped"] == {} and key["all_samples"] is True
    pairs = sorted((i["scenario"], {c["sample"] for c in i["candidates"].values()}.pop()) for i in key["items"].values())
    assert pairs == [(sid, k) for sid in FOUR for k in (0, 1)]
    assert key["variants"]["chat_v1_natural"]["context_style"] == "natural"
    for word in ("legacy", "v1-natural", "chat_v1", "context_style", "v1.1.0"):     # "natural" is also a dimension
        assert word not in md and all(word not in json.dumps(r, ensure_ascii=False) for r in rows)
    for r in rows:
        r.update(natural="1", context="1", grounded="2", rhythm="1", self="na", intimacy="na", respect="na",
                 rank="1" if r["candidate"] == "A" else "2", reason="测试", rater="t")
    result = ce.score(rows, key, scenario_set, set_sha)
    assert set(result["by_variant"]) == {"chat_v1_legacy", "chat_v1_natural"}
    assert sum(p["wins"] + p["losses"] for p in result["pairwise_preference"].values()) == 8
    default_rows, _, default_key = ce.blind(scenario_set, set_sha, variants, 85, set(FOUR))
    assert len(default_key["items"]) == 4 and "all_samples" not in default_key
    assert {c["sample"] for i in default_key["items"].values() for c in i["candidates"].values()} == {0}


def test_product_relationship_line_is_kept_unless_omitted(planned):
    out = new_dir()
    s = ab.run(planned, ["P14", "P07"], out, arms=["v1:legacy", "v1:natural"])
    assert s["familiar_relationship"] == "product"
    assert "这是你们第一次聊天" in system_text(requests_of(out, "P14-v1-natural-s0"))       # product behaviour, as warned
    assert "not represented" in s["fairness"]["P14"]["limitations"][0] and s["fairness"]["P07"]["limitations"] == []
    with pytest.raises(ValueError, match="familiar_relationship"):
        ab.run(planned, ["P14"], new_dir(), arms=["v1:natural"], familiar_relationship="invent")
    out = new_dir()                                                     # "omit" only touches probes declared familiar
    s = ab.run(planned, ["P07"], out, arms=["v1:natural"], familiar_relationship="omit")
    assert s["jobs"][0]["relationship_line"] == "product"
    assert "【你们的相处" in system_text(requests_of(out, "P07-v1-s0"))


def test_blind_all_samples_pairs_the_same_sample_index():
    scenario_set, set_sha = ce.load_set()
    a = {"variant": "a", "replies": {"P03": [["a0"], ["a1"]], "P17": [["a0"], ["a1"]]}}
    b = {"variant": "b", "replies": {"P03": [["b0"], ["b1"]], "P17": [["b0"]]}}
    rows, md, key = ce.blind(scenario_set, set_sha, [a, b], 3, {"P03", "P17"}, all_samples=True)
    assert key["skipped"] == {"P17#s1": ["b"]} and len(key["items"]) == 3
    for item in key["items"].values():
        k = {c["sample"] for c in item["candidates"].values()}.pop()
        assert {c["sha256"] for c in item["candidates"].values()} == {ce.sha256_text(f"a{k}"), ce.sha256_text(f"b{k}")}


def test_real_scenarios_are_refused_for_any_natural_arm(planned, tmp_path):
    manifest = json.loads(planned.read_text(encoding="utf-8"))
    manifest["scenarios"]["R31"] = {"kind": "real", "class": "complete"}     # as if its context had been rebuilt
    manifest["eligible"].append("R31")
    (tmp_path / "m.json").write_text(json.dumps(manifest), encoding="utf-8")
    target = new_dir()
    for arms in (["v1:legacy", "v1:natural"], ["v1:natural"]):
        with pytest.raises(ValueError, match="natural context needs recorded message times.*R31"):
            ab.run(tmp_path / "m.json", ["P03", "R31"], target, arms=arms)
    assert not target.exists()


def test_real_flag_is_not_dropped_for_a_natural_run(planned, monkeypatch):
    # Phase 8.4 reused the name `real` for the refused-scenario list: an all-synthetic natural run went to the mock.
    monkeypatch.delenv("VIOLET_TEST_ALLOW_REAL_API", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.setenv("LLM_API_URL", "x")
    for kwargs in ({"context_style": "natural"}, {"arms": ["v1:legacy", "v1:natural"]}):
        target = new_dir()
        with pytest.raises(PermissionError, match="VIOLET_TEST_ALLOW_REAL_API"):
            ab.run(planned, ["P17"], target, real=True, api_url=URL, launch=fake_launch,
                   input_fn=lambda p: "yes", key_prompt=lambda p: KEY, **kwargs)
        assert not target.exists()


def test_fake_sdk_style_run_records_everything_and_leaks_no_credential(planned, real_env):
    out = new_dir()
    s = ab.run(planned, ["P03", "P17"], out, samples=2, real=True, api_url=URL, launch=fake_launch,
               input_fn=lambda p: "yes", key_prompt=lambda p: KEY, environment="fake_sdk_offline",
               arms=["v1:legacy", "v1:natural"], familiar_relationship="omit", max_calls=20, max_tokens=200_000)
    assert s["state"] == "complete" and s["used_calls"] == 8 and s["used_tokens"] > 0
    assert sum(j["calls"] for j in s["jobs"]) == 8 and sum(j["tokens"] for j in s["jobs"]) == s["used_tokens"]
    natural = json.loads((out / "variant-v1-natural.json").read_text(encoding="utf-8"))
    legacy = json.loads((out / "variant-v1-legacy.json").read_text(encoding="utf-8"))
    assert natural["replies"]["P03"] == [["带时间：好呀"]] * 2 and legacy["replies"]["P03"] == [["无时间：好呀"]] * 2
    assert natural["environment"] == legacy["environment"] == "fake_sdk_offline"
    for g in (out / "generations").glob("*.json"):
        r = json.loads(g.read_text(encoding="utf-8"))
        assert r["network_guard"]["allowed_endpoints"] == ["127.0.0.1:18995"] and r["calls"] and r["request_messages"]
        assert r["model"] and r["errors"] == []
    assert all(KEY.encode() not in p.read_bytes() for p in out.rglob("*") if p.is_file())


def test_budget_stop_lists_every_generation_that_did_not_run(planned, real_env):
    s = ab.run(planned, ["P03", "P08"], new_dir(), samples=2, real=True, api_url=URL, launch=fake_launch,
               input_fn=lambda p: "yes", key_prompt=lambda p: KEY, arms=["v1:legacy", "v1:natural"], max_calls=3)
    assert s["state"] == "stopped_budget" and s["used_calls"] <= 3 and s["planned_generations"] == 8
    assert len(s["jobs"]) == 3 and len(s["not_run"]) == 5
    assert {(n["scenario"], n["sample"], n["arm"]) for n in s["not_run"]} >= {("P08", 1, "v1-natural")}


def test_a_failed_child_and_a_skewed_arm_are_never_reported_as_fair(planned):
    def dead_natural(argv, env, log):
        if argv[argv.index("--context-style") + 1] == "natural":
            log.write_text("simulated crash", encoding="utf-8")
            return 1
        return ab._launch(argv, env, log)
    s = ab.run(planned, ["P17"], new_dir(), arms=["v1:legacy", "v1:natural"], launch=dead_natural)
    assert s["state"] == "has_failed_jobs" and s["failed_jobs"][0]["errors"] == ["child wrote no result"]

    def skewed(argv, env, log):
        if argv[argv.index("--context-style") + 1] == "natural":
            spec_path = Path(argv[argv.index("--spec") + 1])
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec["preferences"] = []                                    # the natural arm loses the preference
            changed = spec_path.with_name("skewed-" + spec_path.name)
            changed.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            argv = [*argv[:argv.index("--spec") + 1], str(changed), *argv[argv.index("--spec") + 2:]]
        return ab._launch(argv, env, log)
    s = ab.run(planned, ["P17"], new_dir(), arms=["v1:legacy", "v1:natural"], launch=skewed)
    f = s["fairness"]["P17"]
    assert s["state"] == "context_mismatch" and f["context_identical"] is False
    assert f["shared_inputs_identical"]["spec_sha256"] is False and f["within_arm_identical"]["v1-natural"] is True


def test_cli_arms_and_context_style_are_exclusive(planned, capsys):
    with pytest.raises(SystemExit):
        ab.main(["run", "--manifest", str(planned), "--scenarios", "P17", "--out", str(new_dir()),
                 "--arms", "v1:legacy,v1:natural", "--context-style", "natural"])
    assert "drop --context-style" in capsys.readouterr().err
