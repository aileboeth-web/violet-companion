"""B.11.2 synthetic specialized tests. Real pipeline, local sender and adversarial model attempts.

All CLI sessions stay below local/b112, outside Windows TEMP, with append-only evidence.
"""
from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tools.synthetic_rounds import (MARKER, PROJECT, PURPOSE, REPORTS, SOURCE_KINDS,
                                    build_seed, digest, ending_checks, validate_workdir)


@pytest.fixture(scope="module")
def specialized():
    workdir = PROJECT.parent / "local/b112/test-cases" / uuid.uuid4().hex
    sources = {name: digest(PROJECT / rel) for name, rel in REPORTS.items()}
    result = subprocess.run([sys.executable, "-X", "utf8", "-m", "tools.synthetic_rounds",
                             "--workdir", str(workdir)], cwd=PROJECT, capture_output=True, text=True,
                            encoding="utf-8", timeout=60)
    report = json.loads((workdir / "specialized_report.json").read_text(encoding="utf-8"))
    assert result.returncode == 0, (result.stdout, result.stderr, report)
    assert sources == {name: digest(PROJECT / rel) for name, rel in REPORTS.items()}
    return workdir, report


def test_seed_values_have_verifiable_field_level_provenance():
    seed = build_seed()
    assert seed["synthetic"] is True and seed["purpose"] == PURPOSE
    assert seed["counts_toward_original_32"] is seed["continuous_acceptance_eligible"] is False
    assert not seed["original_database_restored"]
    seen = set()

    def verify(node):
        if isinstance(node, dict) and "source" in node:
            assert node["source"] in SOURCE_KINDS
            seen.add(node["source"])
            if node["evidence"]:
                e = node["evidence"]
                assert digest(PROJECT / e["path"]) == e["sha256"]
                if node["source"] == "report":
                    from tools.legacy import upgrade
                    value = upgrade(json.loads((PROJECT / e["path"]).read_text(encoding="utf-8")))
                    for part in e["pointer"].strip("/").split("/"):
                        value = value[int(part)] if isinstance(value, list) else value[part]
                    assert value == node["value"]
        elif isinstance(node, dict):
            for child in node.values(): verify(child)
        elif isinstance(node, list):
            for child in node: verify(child)
        else:
            raise AssertionError(f"State field without provenance: {node!r}")
    verify(seed["state"])
    assert seen == SOURCE_KINDS
    assert all(v["value"] is None for v in seed["state"]["unknown"].values())
    assert [r["n"]["value"] for r in seed["state"]["dialogue"]] == [24, 25, 26]
    assert seed["state"]["scenario_time"]["value"] == "2026-10-22T16:13:52+08:00"


def test_round27_mock_ending_and_adversarial_preference_attempt(specialized):
    _, report = specialized
    r = report["rounds"][0]
    assert r["n"] == 27 and r["user"] == "算了不想聊了"
    assert all(ending_checks(r["violet"]).values())
    assert report["checks"]["no_long_term_preference_write"]
    assert r["preference_attempts"][0]["result"] == {
        "action": "rejected", "reason": "session_ending_not_durable_preference"}
    assert "模拟" in r["human_review"] and report["manual_scores"]["27"] is None


def test_round28_delivery_links_delay_and_no_duplicates(specialized):
    path, report = specialized
    r = report["rounds"][1]
    [reminder] = r["reminder"]["reminders"]
    assert r["time_advance"]["hours"] == 20
    # The scenario clock starts at the seed instant and runs on with real elapsed time (ScenarioClock), so the
    # advance starts a little after 16:13:52 -- never before it, and within the CLI's 60 s timeout.
    start = datetime.fromisoformat("2026-10-22T16:13:52+08:00")
    begin, end = (datetime.fromisoformat(r["time_advance"][k]) for k in ("from", "to"))
    assert timedelta(0) <= begin - start < timedelta(seconds=60)
    assert end - begin == timedelta(hours=20)
    assert reminder["late_minutes"] == 253 and "测试时间跳跃导致的延迟" in reminder["late_cause"]
    assert reminder["delivery"] == reminder["status_after"] == reminder["event_status"] == "sent"
    assert r["reminder"]["chat_proactive"]["sent"] == []
    assert not r["repeat"]["sent"] and not r["repeat"]["reminders"]
    assert r["on_time_real_delivery_verified"] is False
    # Read only. Check IDs against the actual persisted DB, not only the runner's booleans.
    conn = sqlite3.connect(f"file:{(path / 'violet-test.db').as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        cid, event = reminder["commitment_id"], reminder["proactive_event_id"]
        assert conn.execute("SELECT status, proactive_event_id FROM commitments WHERE id=?", (cid,)).fetchone() == ("sent", event)
        [row] = conn.execute("SELECT id, decision, status FROM proactive_events WHERE event_type='commitment_reminder' AND status='sent'").fetchall()
        assert row[0] == event and json.loads(row[1])["commitment_id"] == cid and row[2] == "sent"
        [outbox] = conn.execute("SELECT status, content FROM outbound_messages WHERE proactive_event_id=?", (event,)).fetchall()
        assert outbox[0] == "sent" and "交作业" in outbox[1]
        assert conn.execute("SELECT value FROM interaction_preferences WHERE pref_key='proactive_chat' AND status='active'").fetchone() == ("no",)
        assert conn.execute("SELECT content FROM messages ORDER BY id LIMIT 1").fetchone()[0] == "我周末去爬了泰山，累坏了"
        assert conn.execute("SELECT COUNT(*) FROM life_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_quiet_rules_for_ordinary_chat_and_reminders(specialized):
    _, report = specialized
    r = report["rounds"][1]
    assert r["quiet_probe"]["due_reason"] == "quiet hours" and not r["quiet_probe"]["sent"]
    [reminder] = r["quiet_reminder_probe"]["reminders"]
    assert reminder["status_after"] == "pending" and reminder["delivery"] == "postponed_quiet_hours"
    assert not r["quiet_reminder_probe"]["sent"]


def test_specialized_report_has_no_original_score_or_continuation_state(specialized):
    path, report = specialized
    assert report["status"] == "通过" and all(report["checks"].values())
    assert report["synthetic"] and report["purpose"] == PURPOSE
    assert report["original_32_score"] is None and not report["counts_toward_original_32"]
    assert not (path / ".violet-eval-state.json").exists()
    assert (path / MARKER).exists()
    assert report["network_guard"]["allowed_connections"] == 0
    assert not report["real"] and not report["close_errors"]
    materialized = json.loads((path / "seed_materialized.json").read_text(encoding="utf-8"))
    assert materialized["synthetic"] and materialized["purpose"] == PURPOSE
    for rows in materialized["tables"].values():
        for row in rows:
            assert row["id"]["source"] == "synthetic"
            assert all(v["source"] in SOURCE_KINDS for v in row.values())
    with (path / "specialized_scores.csv").open(encoding="utf-8-sig") as f:
        assert all(r["purpose"] == PURPOSE and r["score(0/1/2)"] == "" for r in csv.DictReader(f))


@pytest.mark.parametrize("text", ["算了不想聊了", "今天不想聊了。", "我先不聊了", "暂时不想聊天了！"])
def test_one_session_ending_cannot_supersede_a_long_term_preference(violetdb, text):
    from ai.profile_store import Profile
    store = Profile(violetdb).preferences
    store.set_explicit("proactive_chat", "welcome", source="dashboard")
    before = violetdb.fetch_all("SELECT * FROM interaction_preferences")
    got = store.set_explicit("proactive_chat", "no", evidence=text, user_message=text)
    assert got == {"action": "rejected", "reason": "session_ending_not_durable_preference"}
    assert violetdb.fetch_all("SELECT * FROM interaction_preferences") == before


@pytest.mark.parametrize("text", ["以后别主动找我聊天", "算了不想聊了，以后别主动找我", "别主动找我"])
def test_explicit_future_optout_is_still_respected(violetdb, text):
    from ai.profile_store import Profile
    got = Profile(violetdb).preferences.set_explicit("proactive_chat", "no", evidence=text, user_message=text)
    assert got["action"] == "set"
    assert Profile(violetdb).preferences.effective()["proactive_chat"]["value"] == "no"


@pytest.mark.parametrize("replies", [["为什么不聊了？"], ["你应该振作起来"], ["怎么了，告诉我"], ["好" * 50], []])
def test_ending_checks_do_not_accept_bad_mock_answers(replies):
    assert not all(ending_checks(replies).values())


def test_specialized_directories_cannot_continue_or_seed_original_acceptance(specialized, tmp_path):
    from tools.harness import IsolationError, seed_workdir
    from tools.replay import ReplaySourceError, check_workdir
    path, _ = specialized
    for real in (False, True):
        with pytest.raises(ReplaySourceError, match="合成"):
            check_workdir(path, False, {27, 28}, real)
    with pytest.raises(IsolationError, match="合成"):
        seed_workdir(path, tmp_path / "target")
    assert not list(tmp_path.iterdir())


def test_synthetic_state_is_refused_even_without_marker(tmp_path):
    from tools.replay import ReplaySourceError, read_state
    (tmp_path / ".violet-eval-state.json").write_text(json.dumps({"synthetic": True}), encoding="utf-8")
    with pytest.raises(ReplaySourceError, match="合成"):
        read_state(tmp_path)


@pytest.mark.parametrize("flag", [{"synthetic": True}, {"purpose": PURPOSE}, {"counts_toward_original_32": False}])
def test_synthetic_reports_cannot_be_replayed_summarized_or_rechecked(tmp_path, flag):
    from tools.dialogue_eval import recheck_report, summarize
    from tools.replay import ReplaySourceError, _load_report
    path = tmp_path / "special.json"
    path.write_text(json.dumps(flag), encoding="utf-8")
    for action in (lambda: _load_report(path), lambda: summarize(flag, {27: (2, "special")}), lambda: recheck_report(path)):
        with pytest.raises(ReplaySourceError, match="合成"):
            action()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["special.json"]


def test_specialized_scores_refused_by_original_score_loader(specialized):
    from tools.dialogue_eval import load_scores
    path, _ = specialized
    with pytest.raises(ValueError, match="合成"):
        load_scores(path / "specialized_scores.csv")


def test_persistent_directory_required_and_no_overwrite(specialized, tmp_path):
    path, _ = specialized
    for invalid in (path, tmp_path / "temp-not-persistent", PROJECT / "other"):
        with pytest.raises(ValueError, match="persistent"):
            validate_workdir(invalid)
    before = digest(path / "seed.json")
    result = subprocess.run([sys.executable, "-X", "utf8", "-m", "tools.synthetic_rounds", "--workdir", str(path)],
                            cwd=PROJECT, capture_output=True, timeout=30)
    assert result.returncode != 0 and digest(path / "seed.json") == before
