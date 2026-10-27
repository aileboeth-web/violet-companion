"""Phase 7 B.7.1: continuing a directory whose rounds were run with a plan revision.

The old rule refused whenever the current revision changes a completed round -- even when that round was
run with this very revision (round 15 of violet-testchat-b62-r15-a ran with revision 2). Now every completed
round is compared with the plan text it would get today. Offline, fictional data."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
BASE_PLAN = PROJECT / "tests" / "samples" / "acceptance_dialogue_30.json"


def _meta(revision="auto"):
    from tools.replay import load_plan
    return load_plan(BASE_PLAN, revision)[1]


def _state(last=15, used=2, revisions=None, untagged=()):
    meta = _meta()
    rounds = []
    for n in range(1, last + 1):
        entry = {"n": n, "mode": "replay" if n <= 10 else "real", "scenario_time": f"2026-10-13T09:{30 + n:02d}:00+08:00"}
        if n > 10 and n not in untagged:
            entry["plan_revision"] = used
        rounds.append(entry)
    return {
        "version": 1, "plan": str(BASE_PLAN), "plan_sha256": meta["base_sha256"],
        "plan_revisions": revisions if revisions is not None else {
            "2": {"file": meta["revision_file"], "sha256": meta["revision_sha256"], "changed_rounds": [15]}},
        "replay": {"source": ["a"], "sha256": ["b"], "rounds": list(range(1, 11)), "status": "complete"},
        "rounds": rounds, "clock_end": "2026-10-22T10:01:35.343000+08:00",
    }


def _dir(tmp_path, state, name="violet-testchat-kept"):
    d = tmp_path / name
    d.mkdir()
    (d / ".violet-eval-state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return d


def _check(d, rounds, revision="auto"):
    from tools.replay import check_workdir
    return check_workdir(d, False, set(rounds), True, _meta(revision))


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

def test_a_round_run_with_the_same_revision_file_can_be_continued(tmp_path):
    d = _dir(tmp_path, _state())
    assert _check(d, {16})["mode"] == "continue"
    assert _check(d, range(16, 21))["mode"] == "continue"


def test_the_failed_case_of_r15a_round15_with_revision2_then_round16(tmp_path):
    """The exact shape of violet-testchat-b62-r15-a: rounds 1-10 replayed (no revision recorded), 11-13 real,
    14 replayed from a continued report, 15 real -- 11 to 15 all with revision 2."""
    state = _state()
    state["rounds"][13]["mode"] = "replay"
    state["continuation_replays"] = [{"rounds": [14], "status": "complete"}]
    assert _check(_dir(tmp_path, state), {16})["mode"] == "continue"


@pytest.mark.parametrize("state,revision,message", [
    (lambda: _state(used=1, revisions={"1": {"file": None, "sha256": None, "changed_rounds": []}}), "auto",
     "是用原剧本跑的，现在用修正版 2"),                                 # base -> rev2 changes a finished round
    (lambda: _state(revisions={"2": {"file": "x", "sha256": "0" * 64, "changed_rounds": [15]}}), "auto",
     "修正版文件之后被改动了"),                                          # rev2 file changed since
    (lambda: _state(revisions={"2": {"file": "x", "changed_rounds": [15]}}), "auto", "没有它的文件哈希"),
    (lambda: _state(untagged=(15,)), "auto", "没有这一轮用哪个剧本版本跑的记录"),
    (lambda: _state(), "none", "是用修正版 2跑的，现在用原剧本"),          # rev2 -> back to the base plan
    (lambda: _state(used=3, revisions={}), "auto", "没有这个修正版的记录"),
])
def test_a_finished_round_that_would_get_another_plan_text_is_refused(tmp_path, state, revision, message):
    from tools.replay import ReplaySourceError
    d = _dir(tmp_path, state())
    before = hashlib.sha256((d / ".violet-eval-state.json").read_bytes()).hexdigest()
    with pytest.raises(ReplaySourceError, match=message):
        _check(d, {16}, revision)
    assert hashlib.sha256((d / ".violet-eval-state.json").read_bytes()).hexdigest() == before


def test_a_revision_of_rounds_not_run_yet_is_allowed(tmp_path):
    """Rounds 1-14 finished; revision 2 changes only round 15: allowed, whatever was recorded before."""
    for state in (_state(last=14), _state(last=14, used=1, revisions={}), _state(last=14, untagged=range(11, 15)),
                  _state(last=14, revisions={"2": {"file": "x", "sha256": "0" * 64, "changed_rounds": [15]}})):
        d = _dir(tmp_path, state, name=f"violet-testchat-{hashlib.md5(json.dumps(state).encode()).hexdigest()[:8]}")
        assert _check(d, {15})["mode"] == "continue"


def test_older_rules_still_apply(tmp_path):
    from tools.replay import ReplaySourceError
    cases = [
        (lambda s: s["rounds"].pop(12), "连续不重复"),                                   # a gap: 1-12, 14, 15
        (lambda s: s["rounds"].insert(5, dict(s["rounds"][4])), "连续不重复"),           # round 5 twice
        (lambda s: s["rounds"][14].update(mode="mock"), "模拟回复的轮次"),
        (lambda s: s["replay"].update(status="in_progress"), "回放没有成功完成"),
        (lambda s: s.update(continuation_replays=[{"rounds": [14], "status": "failed"}]), "续跑回放没有成功完成"),
        (lambda s: s.update(plan_sha256="0" * 64), "剧本哈希不一致"),
    ]
    for i, (edit, message) in enumerate(cases):
        state = _state()
        edit(state)
        d = _dir(tmp_path, state, name=f"violet-testchat-old-{i}")
        with pytest.raises(ReplaySourceError, match=message):
            _check(d, {16})
    with pytest.raises(ReplaySourceError, match="拒绝重复运行"):
        _check(_dir(tmp_path, _state(), name="violet-testchat-overlap"), {15, 16})


# ---------------------------------------------------------------------------
# Through the CLI: --seed-from and a direct --workdir decide the same way
# ---------------------------------------------------------------------------

@pytest.fixture()
def clean_env():
    saved = dict(os.environ)
    yield os.environ
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture()
def run_eval(clean_env, violetdb, tmp_path, monkeypatch):
    from tools import dialogue_eval, violet_test_chat
    real_open = violet_test_chat.open_session
    monkeypatch.setattr(violet_test_chat, "open_session",
                        lambda args, input_fn=input, **kw: real_open(args, input_fn=input_fn, out=lambda s: None,
                                                                     db=violetdb, **kw))
    counter = {"n": 0}

    def run(*argv):
        counter["n"] += 1
        out = tmp_path / f"out{counter['n']}"
        rc = dialogue_eval.main([*argv, "--output-dir", str(out)])
        report = json.loads((out / "report.json").read_text(encoding="utf-8")) if (out / "report.json").exists() else None
        return rc, report
    return run


def _kept(violetdb, tmp_path, name, state):
    d = _dir(tmp_path, state, name)
    src, dst = sqlite3.connect(violetdb.db_path), sqlite3.connect(d / "violet-test.db")
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return d


def _listing(d):
    return sorted((p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in d.iterdir() if p.is_file())


@pytest.mark.parametrize("seeded", [True, False])
def test_seed_and_direct_continuation_accept_the_same_directory(run_eval, violetdb, tmp_path, seeded):
    kept = _kept(violetdb, tmp_path, "violet-testchat-r15", _state())
    before = _listing(kept)
    target = tmp_path / "violet-testchat-r16"
    argv = (["--seed-from", str(kept), "--workdir", str(target)] if seeded else ["--workdir", str(kept)])
    rc, report = run_eval(*argv, "--rounds", "16")
    assert rc == 0 and [r["n"] for r in report["rounds"]] == [16]
    assert report["rounds"][0]["proactive"][0]["check"] == "ended_event_not_allowed"
    if seeded:
        assert _listing(kept) == before and not target.exists()                 # source untouched, copy removed


@pytest.mark.parametrize("seeded", [True, False])
def test_seed_and_direct_continuation_refuse_the_same_directory(run_eval, violetdb, tmp_path, capsys, seeded):
    kept = _kept(violetdb, tmp_path, "violet-testchat-r15-changed",
                 _state(revisions={"2": {"file": "x", "sha256": "0" * 64, "changed_rounds": [15]}}))
    before = _listing(kept)
    target = tmp_path / "violet-testchat-r16-refused"
    argv = (["--seed-from", str(kept), "--workdir", str(target)] if seeded else ["--workdir", str(kept)])
    rc, report = run_eval(*argv, "--rounds", "16")
    assert rc == 2 and report is None and "修正版文件之后被改动了" in capsys.readouterr().out
    assert _listing(kept) == before and not target.exists()
    assert not violetdb.fetch_all("SELECT id FROM messages WHERE content = '嗯嗯'")


def test_an_existing_target_is_never_overwritten(run_eval, violetdb, tmp_path, capsys):
    kept = _kept(violetdb, tmp_path, "violet-testchat-r15-src", _state())
    target = tmp_path / "violet-testchat-existing"
    target.mkdir()
    (target / "keep.txt").write_text("mine", encoding="utf-8")
    assert run_eval("--seed-from", str(kept), "--workdir", str(target), "--rounds", "16")[0] == 2
    assert "还不存在的 --workdir" in capsys.readouterr().out
    assert [p.name for p in target.iterdir()] == ["keep.txt"]
