"""Phase 8.7: multi-turn chat evaluation tool. Offline: mock model, or a fake SDK on a loopback URL."""
import copy
import csv
import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tools import chat_dialogue as cd

KEY = "fake-p87-private-test-credential-0000"
URL = "http://127.0.0.1:18994/v1"
BASE = cd.ROOT / "local/p87/test-cases"

# Child bootstrap for tests only: the OpenAI SDK is replaced by the tool's own offline mock.
BOOT = r'''
import json, sys
from unittest.mock import patch
from tools import chat_dialogue as cd
Mock = cd._mock_class()
class FakeSDK(Mock):
    def __init__(self, **kwargs):
        assert kwargs["api_key"] == "%s" and kwargs["base_url"] == "%s"
        super().__init__()
with patch("openai.OpenAI", FakeSDK):
    raise SystemExit(cd.main(["_child", *json.loads(sys.argv[1])]))
''' % (KEY, URL)


def fake_launch(argv, env, log):
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOT, json.dumps(argv)], cwd=cd.PROJECT, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=900)
    with log.open("x", encoding="utf-8") as f:
        f.write(done.stdout + done.stderr)
    return done.returncode


def new_dir():
    return BASE / uuid.uuid4().hex


def load(run_dir, name):
    return json.loads((run_dir / name).read_text(encoding="utf-8"))


def chat_system(step):
    req = next(r["request"] for r in step["requests"] if r["agent"] == "ChatAgent")
    return "\n".join(m["content"] for m in req if m["role"] == "system")


@pytest.fixture(scope="module")
def episodes():
    return cd.load_episodes()[0]


@pytest.fixture(scope="module")
def full_run():
    out = new_dir()
    return out, cd.run(out, label="mock-all")


# ---------------------------------------------------------------------------------------------- episode set

def test_episode_set_is_synthetic_versioned_and_covers_eight_situations(episodes):
    assert episodes["version"] == "p87.v1" and episodes["synthetic"] is True
    assert [e["id"] for e in episodes["episodes"]] == [f"E{i}" for i in range(1, 9)]
    for ep in episodes["episodes"]:
        users = [s for s in ep["steps"] if "user" in s]
        assert 8 <= len(users) <= 12 and ep["rater_notes"]["red_flags"], ep["id"]
    assert cd.validate_episodes(episodes) == []


@pytest.mark.parametrize("mutate,problem", [
    (lambda d: d.pop("synthetic"), "marked synthetic"),
    (lambda d: d["episodes"][0]["seed"]["history"].append(
        {"at": "2026-11-02T20:30:00+08:00", "role": "user", "text": "晚"}), "before the episode starts"),
    (lambda d: d["episodes"][0]["steps"][1].update(after="soon"), "bad 'after'"),
    (lambda d: d["episodes"][0]["steps"][1].update(tick="scheduler"), "exactly one of user / tick"),
    (lambda d: d["episodes"][0]["steps"][1].update(reply="想要的回复"), "unknown keys"),
    (lambda d: d["episodes"][0].update(steps=d["episodes"][0]["steps"][:5]), "user turns"),
    (lambda d: d["episodes"][1].update(id="E1"), "duplicated"),
])
def test_broken_episode_sets_are_refused(episodes, mutate, problem, tmp_path):
    data = copy.deepcopy(episodes)
    mutate(data)
    assert any(problem in p for p in cd.validate_episodes(data))
    (tmp_path / "e.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="episode set refused"):
        cd.load_episodes(tmp_path / "e.json")


# ---------------------------------------------------------------------------------------------- full mock run

def test_all_eight_episodes_run_through_the_product_pipeline(full_run, episodes):
    out, s = full_run
    assert s["state"] == "complete" and s["environment"] == "mock" and s["memory"] == "scripted"
    assert s["steps_executed"] == s["steps_planned"] and s["not_run"] == {}
    debug = load(out, "debug.json")["episodes"]
    for ep in episodes["episodes"]:
        res = debug[ep["id"]]
        assert res["state"] == "complete" and res["synthetic"] is True and res["not_run"] == []
        for step in res["steps"]:
            assert step["status"] == "ok", (ep["id"], step["n"], step["problems"])
            if step["kind"] == "user":
                assert step["background_complete"] and all(j["status"] == "done" for j in step["memory_jobs"])
                agents = [c["agent"] for c in step["calls"]]
                assert agents[0] == "IntentAgent", (ep["id"], step["n"])
                if step["reply"] == "sent":
                    chat = [r for r in step["requests"] if r["agent"] == "ChatAgent"]
                    assert "ChatAgent" in agents and chat[0]["request"][-1]["content"] == step["user"]
        # the scenario clock follows the script, never the host clock
        assert res["steps"][0]["scheduled_at"] == cd.datetime.fromisoformat(ep["start"]).isoformat()
        assert all(abs(st["late_seconds"]) < 60 for st in res["steps"])


def test_each_episode_has_its_own_new_database(full_run, episodes):
    out, _ = full_run
    debug = load(out, "debug.json")["episodes"]
    paths = set()
    for ep in episodes["episodes"]:
        res = debug[ep["id"]]
        db = Path(res["database"]["path"])
        assert db.parent == (out / "work" / ep["id"]).resolve() and db.is_file()
        paths.add(db)
        with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as conn:
            texts = [r[0] for r in conn.execute("SELECT content FROM messages ORDER BY id")]
        seeded = [h["text"] for h in ep["seed"]["history"]]
        assert texts[:len(seeded)] == seeded                         # only this episode's synthetic history
        sent = sum(len(st.get("violet") or []) + len(st.get("reminder_sent") or []) + len(st.get("proactive_sent") or [])
                   for st in res["steps"])
        users = sum(1 for st in res["steps"] if st["kind"] == "user")
        assert len(texts) == res["messages_in_database"] == len(seeded) + users + sent
    assert len(paths) == 8
    with pytest.raises(FileExistsError):
        cd.run(out, label="again", episode_ids=["E1"])


def test_silence_correction_reminder_and_preference_are_recorded(full_run):
    out, _ = full_run
    debug = load(out, "debug.json")["episodes"]
    e1 = debug["E1"]["steps"]
    assert e1[8]["user"] == "嗯嗯" and e1[8]["violet"] == [] and e1[8]["reply"] == "silent_by_intent"
    assert e1[8]["intent"][0]["route"] == "silent" and e1[9]["violet"]
    e3 = debug["E3"]
    correction = e3["steps"][2]
    assert correction["memory_script"] == "applied" and correction["memory_script_results"][0]["result"]["ok"]
    assert {(f["fact_key"], f["value"], f["status"]) for f in e3["final_state"]["facts"]} >= {
        ("study.major", "物理", "active"), ("study.major", "天文", "superseded")}
    assert "专业：天文" not in chat_system(e3["steps"][9])
    reminder_step = e3["steps"][3]
    assert reminder_step["state_changes"]["commitments"]["now"][0]["status"] == "pending"
    tick = e3["steps"][5]
    assert tick["kind"] == "scheduler" and tick["reminders"][0]["delivery"] == "sent"
    assert tick["reminders"][0]["late_cause"] and "时间跳跃" in tick["reminders"][0]["late_cause"]
    assert tick["reminder_sent"] == ["（模拟提醒）到点啦，交作业"]
    assert "已经提醒过了" in chat_system(e3["steps"][6])               # Chat knows it was sent
    assert e3["final_state"]["commitments"][0]["status"] == "sent"
    assert "不希望被追问" in chat_system(debug["E8"]["steps"][0])
    e6 = debug["E6"]["steps"]
    assert e6[0]["state_changes"]["situations"]["now"][0]["kind"] == "availability"
    assert [s["kind"] for s in e6].count("scheduler") == 2 and all(s.get("due_reason") for s in e6 if s["kind"] == "scheduler")


def test_transcript_is_readable_and_complete(full_run, episodes):
    out, _ = full_run
    text = (out / "transcript.md").read_text(encoding="utf-8")
    for ep in episodes["episodes"]:
        assert f"## {ep['id']} {ep['title']}" in text
        for step in ep["steps"]:
            if "user" in step:
                assert f"**小满**：{step['user']}" in text
    assert "**11月2日（周一）**" in text and "⋯⋯ 过了 9 小时" in text and "背景（合成，非真实数据）" in text
    assert "_Eve 没有回复（判断为不需要回复）_" in text and "**Violet（提醒）**：（模拟提醒）到点啦，交作业" in text
    assert "（这段时间 Violet 没有主动发消息）" in text and "⚠" not in text
    assert "mock" in text.split("## E1")[0]                          # the run header says it is simulated


def test_provenance_and_diagnostics(full_run):
    out, s = full_run
    p = s["provenance"]
    assert len(p["product_tree_sha256"]) == 64 and p["tool"] == cd.TOOL_VERSION
    assert p["episodes_file_sha256"] == cd.sha256_bytes(cd.EPISODES_PATH.read_bytes())
    episodes = cd.load_episodes()[0]["episodes"]
    assert s["episode_sha256"] == {e["id"]: cd.episode_sha(e) for e in episodes}
    child = s["child_provenance"]["E1"]
    assert child["chat"]["prompt_sha256"] and child["chat"]["model"] and child["context_style"] == "natural"
    assert child["persona_core_sha256"] == cd.sha256_bytes((cd.PROJECT / "persona/persona_core.json").read_bytes())
    diag = load(out, "diagnostics.json")
    assert diag["diagnostic_only"] is True and diag["episodes"]["E1"]["silent"] == 1
    assert set(diag["episodes"]) == {f"E{i}" for i in range(1, 9)}


# ---------------------------------------------------------------------------------------------- variants, blind, score

@pytest.fixture(scope="module")
def variant_run():
    out = new_dir()
    prompt = out.parent / f"prompt-{uuid.uuid4().hex}.txt"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text((cd.PROJECT / "persona/chat_prompt_versions/chat.v1.txt").read_text(encoding="utf-8")
                      + "\n（测试变体）", encoding="utf-8")
    return out, cd.run(out, label="legacy-variant", episode_ids=["E8"], context_style="legacy",
                       chat_prompt_file=str(prompt)), prompt


def test_variant_settings_are_applied_and_recorded(variant_run):
    out, s, prompt = variant_run
    sha = cd.sha256_bytes(prompt.read_text(encoding="utf-8").encode("utf-8"))
    assert s["state"] == "complete" and s["context_style"] == "legacy"
    assert s["provenance"]["chat_prompt_file_sha256"] == cd.sha256_bytes(prompt.read_bytes())
    assert s["provenance"]["chat_prompt_text_sha256"] == sha
    child = s["child_provenance"]["E8"]
    assert child["context_style"] == "legacy" and child["chat"]["prompt_sha256"] == sha
    step = load(out, "debug.json")["episodes"]["E8"]["steps"][0]
    request = next(r["request"] for r in step["requests"] if r["agent"] == "ChatAgent")
    assert request[0]["content"].endswith("（测试变体）") and "【交流偏好与边界】" in chat_system(step)


def test_blind_hides_the_runs_and_score_merges(full_run, variant_run, tmp_path):
    (full_dir, _), (variant_dir, _, _) = full_run, variant_run
    out = tmp_path / "blind"
    key = cd.blind([full_dir, variant_dir], out, seed=87)
    assert list(key["items"]) == ["E8"] and set(key["skipped"]) == {f"E{i}" for i in range(1, 8)}
    md = (out / "sheet.md").read_text(encoding="utf-8")
    for leak in ("mock-all", "legacy-variant", "legacy", "scripted", "执行状态", key["runs"]["mock-all"]["git_head"] or "x" * 40):
        assert leak not in md
    assert "可以接受" in md and "第 5 步：直接提问：应该回答" in md and md.count("## 候选") == 2
    assert "## E8" not in md and md.count("# 题目 E8") == 1 and md.count("背景（合成，非真实数据）") == 2
    with (out / "sheet.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r.update({d: "4" for d in cd.DIMENSIONS}, intimacy="na", rank="1" if r["candidate"] == "A" else "2",
                 turn_flags="第1步：模拟回复", reason="测试", rater="t")
    result = cd.score(rows, key)
    assert set(result["by_label"]) == {"mock-all", "legacy-variant"} and result["by_label"]["mock-all"]["natural"]["mean"] == 4
    assert sum(p["wins"] + p["losses"] for p in result["pairwise_preference"].values()) == 1
    rows[0]["reason"] = ""
    with pytest.raises(ValueError, match="reason is required"):
        cd.score(rows, key)
    rows[0].update(reason="x", candidate_sha12="0" * 12)
    with pytest.raises(ValueError, match="changed after blinding"):
        cd.score(rows, key)


# ---------------------------------------------------------------------------------------------- failures and safety

def test_budget_stop_marks_unrun_steps_and_episodes(tmp_path):
    out = new_dir()
    s = cd.run(out, label="tight", episode_ids=["E1", "E2"], max_calls=5)
    assert s["state"] == "stopped_budget" and s["episode_states"] == {"E1": "stopped_budget", "E2": "not_run"}
    assert s["used_calls"] <= 5 and s["not_run"]["E2"] == list(range(1, 11)) and s["not_run"]["E1"]
    e1 = load(out, "debug.json")["episodes"]["E1"]
    assert e1["steps"][-1]["status"] == "failed" and any("budget refused" in p for p in e1["steps"][-1]["problems"])
    text = (out / "transcript.md").read_text(encoding="utf-8")
    assert "没有正常完成" in text and "步没有执行" in text and "这一段没有执行" in text
    assert cd.main(["run", "--out", str(new_dir()), "--label", "tight-cli", "--episodes", "E1", "--max-calls", "3"]) == 1


def test_a_crashed_child_or_reused_directory_is_a_failed_episode():
    def dead(argv, env, log):
        log.write_text("simulated crash", encoding="utf-8")
        return 1
    s = cd.run(new_dir(), label="dead", episode_ids=["E1"], launch=dead)
    assert s["state"] == "has_failed_episodes" and s["episode_states"] == {"E1": "child_failed"}

    def reused(argv, env, log):
        Path(argv[argv.index("--workdir") + 1]).mkdir(parents=True)        # as if an older database were there
        return cd._launch(argv, env, log)
    out = new_dir()
    s = cd.run(out, label="reused", episode_ids=["E1"], launch=reused)
    assert s["episode_states"] == {"E1": "child_failed"}
    assert "every episode gets a new database" in (out / "results/E1.log").read_text(encoding="utf-8")


def test_real_mode_needs_the_gate_before_any_directory(monkeypatch):
    monkeypatch.delenv("VIOLET_TEST_ALLOW_REAL_API", raising=False)
    target = new_dir()
    with pytest.raises(PermissionError, match="VIOLET_TEST_ALLOW_REAL_API"):
        cd.run(target, label="r", episode_ids=["E1"], real=True, api_url=URL, launch=fake_launch,
               input_fn=lambda p: "yes", key_prompt=lambda p: KEY)
    assert not target.exists()
    with pytest.raises(ValueError, match="memory=scripted"):
        cd.run(new_dir(), label="m", episode_ids=["E1"], memory="real")


def test_fake_sdk_real_path_records_calls_and_leaks_no_key(monkeypatch):
    for k, v in {"VIOLET_TEST_ALLOW_REAL_API": "1", "LLM_API_KEY": "x", "LLM_API_URL": "x"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("VIOLET_TEST_API_KEY", raising=False)
    out = new_dir()
    s = cd.run(out, label="fake-real", episode_ids=["E1"], real=True, api_url=URL, launch=fake_launch,
               input_fn=lambda p: "yes", key_prompt=lambda p: KEY, environment="fake_sdk_offline",
               max_calls=60, max_tokens=100_000)
    assert s["state"] == "complete" and s["environment"] == "fake_sdk_offline" and s["memory"] == "real"
    res = load(out, "debug.json")["episodes"]["E1"]
    agents = {c["agent"] for c in res["calls"]}
    assert {"IntentAgent", "ChatAgent", "MemoryAgent"} <= agents and all(c["usage_present"] for c in res["calls"])
    assert s["used_calls"] == len(res["calls"]) and s["used_tokens"] == res["budget"]["used_tokens"]
    assert res["network_guard"]["allowed_endpoints"] == ["127.0.0.1:18994"]
    assert res["steps"][2]["memory_script"] is None                    # MemoryAgent decides in real mode
    assert res["steps"][8]["reply"] == "sent"                           # mock_intent is never applied to a model
    assert all(KEY.encode() not in p.read_bytes() for p in out.rglob("*") if p.is_file())


def test_child_refuses_real_calls_without_parent_confirmation(episodes):
    target = new_dir()
    target.mkdir(parents=True)
    (target / "E1.json").write_text(json.dumps(episodes["episodes"][0], ensure_ascii=False), encoding="utf-8")
    env = {**cd._child_env(), "VIOLET_TEST_ALLOW_REAL_API": "1", "VIOLET_TEST_API_KEY": KEY}
    code = fake_launch(["--episode", str(target / "E1.json"), "--workdir", str(target / "w"), "--result",
                        str(target / "r.json"), "--label", "x", "--memory", "real", "--max-calls", "5", "--max-tokens",
                        "10000", "--max-calls-per-turn", "5", "--real", "--api-url", URL], env, target / "child.log")
    log = (target / "child.log").read_text(encoding="utf-8")
    assert code != 0 and not (target / "r.json").exists() and "no parent confirmation" in log and KEY not in log


def test_a_changed_episode_file_is_not_reported_as_that_episode():
    def tampered(argv, env, log):
        path = Path(argv[argv.index("--episode") + 1])
        ep = json.loads(path.read_text(encoding="utf-8"))
        ep["steps"][0]["user"] = "换掉的第一句"
        path.write_text(json.dumps(ep, ensure_ascii=False), encoding="utf-8")
        return cd._launch(argv, env, log)
    s = cd.run(new_dir(), label="tampered", episode_ids=["E1"], launch=tampered)
    assert s["episode_states"] == {"E1": "episode_mismatch"} and s["state"] == "has_failed_episodes"


def test_blind_skips_conversations_that_did_not_finish(full_run, tmp_path):
    full_dir, _ = full_run
    tight = new_dir()
    cd.run(tight, label="tight-blind", episode_ids=["E1"], max_calls=3)
    key = cd.blind([full_dir, tight], tmp_path / "b", seed=1)
    assert key["items"] == {} and key["skipped"]["E1"] == "not completed by every run"
