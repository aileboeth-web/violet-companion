"""Synthetic round-27/28 or 29–32 SPECIALIZED suite, never continuous acceptance.

    python -m tools.synthetic_rounds --workdir D:/unity/violet/local/b112/<new-directory>

Offline by default; --real requires the existing API gate, hidden key prompt and confirmation.
Historical reports are read only; each seeded value has provenance.
The persistent directory and its report are deliberately incompatible with dialogue_eval/replay.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json

from tools.legacy import upgrade
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
PURPOSE = "round27_28_specialized_test"
MARKER = ".violet-synthetic.json"
SOURCE_KINDS = {"report", "prior_observation", "inferred", "synthetic"}
REPORTS = {"r25": "tools/output/20261014-001147/report.json",
           "r26": "tools/output/20261014-002003/report.json",
           "r19": "tools/output/20261013-073654/report.json"}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def build_seed(project=PROJECT):
    """Extract observed fields; inferred and operational values are never labelled report facts."""
    project = Path(project)
    reports = {key: upgrade(json.loads((project / rel).read_text(encoding="utf-8"))) for key, rel in REPORTS.items()}
    if not all(d.get("real") is True and not d.get("synthetic") for d in reports.values()):
        raise ValueError("Seed sources must be original real-model reports")
    sources = {key: {"path": rel, "sha256": digest(project / rel)} for key, rel in REPORTS.items()}

    def value(v, source, report=None, pointer=None, note=None):
        assert source in SOURCE_KINDS
        return {"value": v, "source": source,
                "evidence": ({**sources[report], "pointer": pointer} if report else None), "note": note}

    def observed(row, report, pointer, omit=()):
        return {k: value(v, "report", report, f"{pointer}/{k}") for k, v in row.items() if k not in omit}

    final = reports["r26"]["final_state"]
    dialogue = []
    for key, wanted in (("r25", (24, 25)), ("r26", (26,))):
        for n in wanted:
            i, row = next((i, r) for i, r in enumerate(reports[key]["rounds"]) if r["n"] == n)
            dialogue.append(observed({k: row[k] for k in ("n", "user", "violet", "scenario_time")}, key, f"/rounds/{i}"))
    end = reports["r26"]["scenario_clock"]["end"]
    anchor = datetime.fromisoformat(end).replace(microsecond=0).isoformat()
    if anchor != "2026-10-22T16:13:52+08:00":
        raise ValueError("Unexpected report scenario anchor")
    [commitment] = final["commitments"]
    if commitment != {"content": "交作业", "due_at": "2026-10-23T08:00:00+08:00", "status": "pending"}:
        raise ValueError("Unexpected report reminder state")
    i, pref_round = next((i, r) for i, r in enumerate(reports["r19"]["rounds"]) if r["n"] == 19)
    if not any(t.get("tool") == "set_interaction_preference" and t.get("key") == "follow_up_questions"
               and t.get("action") == "set" for c in pref_round["call_details"] for t in c.get("tool_results", [])):
        raise ValueError("Missing historical preference observation")
    return {"synthetic": True, "purpose": PURPOSE, "original_database_restored": False,
            "continuous_acceptance_eligible": False, "counts_toward_original_32": False,
            "sources": sources, "source_kinds": sorted(SOURCE_KINDS),
            "state": {
                "scenario_time": value(anchor, "inferred", "r26", "/scenario_clock/end",
                                       "Original end retains .562000 seconds; requested seed truncates to seconds."),
                "report_clock_end": value(end, "report", "r26", "/scenario_clock/end"),
                "facts": [observed(r, "r26", f"/final_state/facts/{i}") for i, r in enumerate(final["facts"])],
                "events": [observed(r, "r26", f"/final_state/memories/{i}", omit=("id",))
                           for i, r in enumerate(final["memories"])],
                "dialogue": dialogue,
                "commitment": observed(commitment, "r26", "/final_state/commitments/0"),
                "preferences": [{"key": value("follow_up_questions", "inferred", "r19", f"/rounds/{i}/call_details",
                                                 "Confirmed key in tool result; original value was not recorded."),
                                 "value": value("less", "inferred", "r19", f"/rounds/{i}/user",
                                                "Conservative reconstruction from user wording, not a recovered row."),
                                 "evidence": value(pref_round["user"], "report", "r19", f"/rounds/{i}/user")}],
                "preference_observation": value("follow_up_questions was set in round 19", "prior_observation",
                                                "r19", f"/rounds/{i}/call_details",
                                                "A prior write is observed; no full preference table is available."),
                "runtime_defaults": value("New local IDs, synthetic evidence, creation times, weights and audit rows",
                                          "synthetic", note="Operational defaults are not historical data."),
                "unknown": {k: value(None, "synthetic", note="Unknown; omitted, not reconstructed.") for k in (
                    "original_database_ids", "original_write_times", "profile_evidence", "full_round1_23_history",
                    "implicit_preference_rows_and_counts", "original_proactive_chat_preference",
                    "original_fact_confidence", "event_time_and_weights", "original_memory_job_and_outbox_state",
                    "current_user_location", "violet_life_state")}},
            "limitations": ["Not an original database recovery or round-1..26 replay.",
                            "No Violet persona/life records are seeded; expired location is not treated as current.",
                            "Mock replies cannot establish real-model naturalness or real-world delivery timing."]}


def _plain(row):
    return {k: v["value"] for k, v in row.items()}


def apply_seed(session, seed):
    """Write a NEW synthetic database; all generated IDs remain local to this specialized test."""
    from ai.profile_store import Profile
    db = session.db
    profile = Profile(db)
    state = seed["state"]
    for raw in state["facts"]:
        row = _plain(raw)
        got = profile.facts.remember(row["fact_key"], row["value"], source="dashboard", actor="synthetic_seed")
        if got["action"] not in ("created", "confirmed"):
            raise ValueError(f"Synthetic fact refused: {got['action']}")
    for raw in state["events"]:
        row = _plain(raw)
        db.add_event_memory(**row, category="event", source_turn_id="synthetic:historical-event")
    for raw in state["dialogue"]:
        row = _plain(raw)
        db.add_message("user", "incoming", "text", row["user"], created_at=row["scenario_time"])
        for reply in row["violet"]:
            # Reply text is observed; original send timestamps are unknown.
            db.add_message("assistant", "outgoing", "text", reply, created_at=row["scenario_time"])
    for raw in state["preferences"]:
        row = _plain(raw)
        profile.preferences.set_explicit(row["key"], row["value"], evidence=row["evidence"],
                                         source="dashboard", actor="synthetic_seed")
    commitment = _plain(state["commitment"])
    cid = db.add_commitment(commitment["content"], commitment["due_at"], evidence="提醒我明天早上八点交作业",
                            source_turn_id="synthetic:round26")["commitment"]["id"]
    # Attribute every field actually written to the seed tables, including SQLite-generated metadata.
    # New IDs and default timestamps are operational synthetic values, never recovered originals.
    def generated(v):
        return {"value": v, "source": "synthetic", "evidence": None,
                "note": "New local database/default value; original is unknown."}

    def annotate(table, overrides):
        rows = db.fetch_all(f"SELECT * FROM {table} ORDER BY id")
        assert len(rows) == len(overrides)
        result = []
        for row, fields in zip(rows, overrides):
            record = {k: generated(v) for k, v in row.items()}
            for key, source in fields.items():
                assert row[key] == source["value"], (table, key)
                record[key] = source
            result.append(record)
        return result

    messages = []
    for row in state["dialogue"]:
        messages.append({"content": row["user"], "created_at": row["scenario_time"]})
        for i, reply in enumerate(row["violet"]["value"]):
            text_source = {**row["violet"], "value": reply, "evidence": {
                **row["violet"]["evidence"], "pointer": row["violet"]["evidence"]["pointer"] + f"/{i}"}}
            messages.append({"content": text_source, "created_at": {**row["scenario_time"], "source": "inferred",
                "note": "Reply send time unknown; uses the reported round-start time as a synthetic approximation."}})
    materialized = {"synthetic": True, "purpose": PURPOSE, "tables": {
        "user_profile_facts": annotate("user_profile_facts", state["facts"]),
        "event_memories": annotate("event_memories", state["events"]),
        "messages": annotate("messages", messages),
        "commitments": annotate("commitments", [state["commitment"]]),
        "interaction_preferences": annotate("interaction_preferences", [
            {"pref_key": r["key"], "value": r["value"], "evidence": r["evidence"]} for r in state["preferences"]])}}
    write_new(session.workdir / "seed_materialized.json", materialized)
    return cid


def ending_checks(replies):
    text = "".join(replies)
    return {"short_ending": bool(text) and len(text) <= 45,
            "no_followup": not re.search(r"[?？]|为什么|怎么了|什么原因|告诉我|聊聊原因", text),
            "no_lecture": not re.search(r"你应该|你必须|你得|不能逃避|不要逃避|振作", text)}


def validate_workdir(path):
    path = Path(path).resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    local = (PROJECT.parent / "local").resolve()
    if path.exists() or path == temp or temp in path.parents or local not in path.parents:
        raise ValueError("Use a NEW persistent directory below workspace/local, outside Windows TEMP")
    return path


def make_recorder(budget, secrets=()):
    """Instrument the existing budget, keeping round 28's chat and scheduler under one round cap."""
    from tools.harness import CallRecorder

    class SpecializedRecorder(CallRecorder):
        active_stage = None

        def __init__(self):
            super().__init__(budget, secrets=list(secrets))
            self.reservations = []

        def start_round(self, number):
            if self.round != number:
                super().start_round(number)

        def check(self, agent=None):
            reserved, basis = self.reserve_for(agent)
            decision = {"round": self.round, "stage": self.active_stage, "agent": agent,
                        "used_tokens": self.tokens, "reserved_tokens": reserved, "reserve_basis": basis,
                        "approved": False}
            try:
                super().check(agent)
                decision["approved"] = True
            finally:
                self.reservations.append(decision)

    return SpecializedRecorder()


def validate_endpoint(url):
    from urllib.parse import urlsplit
    parsed = urlsplit(url or "")
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("Text API URL must be HTTP(S), without credentials, query or fragment")
    return url


class SpecializedStop(RuntimeError):
    """Recorded safe stop, including a failed provider call even when Violet used a fallback."""


def run_specialized(workdir, *, args=None, input_fn=input, key_prompt=None, out=print):
    """A fresh, persistent specialized run. Real text calls reuse RealApiGate and VioletTestSession."""
    import getpass
    import os
    from collections import Counter
    from contextlib import nullcontext
    from tools.harness import (Budget, VioletTestSession, MockLLM, NetworkGuard, RealApiGate,
                               ScenarioClock, _AGENT, call_detail, prepare_environment, summarize_calls, write_report)
    from tools.violet_test_chat import MODEL_KEYS, budget_from

    workdir = validate_workdir(workdir)
    if "ai.chat" in sys.modules:
        raise ValueError("Run in a fresh process so no existing database connection can be reused")
    args = args or build_parser().parse_args(["--workdir", str(workdir)])
    tail = getattr(args, "suite", PURPOSE) == "round29_32_specialized_test"
    if not tail and getattr(args, "rounds", None) is not None:
        raise ValueError("--rounds is only supported by round29_32_specialized_test")
    if tail:
        from tools.synthetic_tail import build_tail_seed, run_tail, tail_mock, select_rounds, selection_evidence
    numbers = select_rounds(getattr(args, "rounds", None)) if tail else (27, 28)
    seed = build_tail_seed() if tail else build_seed()
    selection = selection_evidence(seed, numbers) if tail else None
    if tail:
        seed["selection"] = selection
    workdir.mkdir(parents=True)
    write_new(workdir / MARKER, {k: seed[k] for k in (
        "synthetic", "purpose", "continuous_acceptance_eligible", "counts_toward_original_32")})
    write_new(workdir / "seed.json", seed)
    session = guard = None
    recorder = make_recorder(Budget())
    report = {"synthetic": True, "purpose": seed["purpose"], "specialized_test": True, "real": False,
              "real_requested": args.real, "continuous_acceptance_eligible": False,
              "counts_toward_original_32": False, "original_32_score": None,
              "manual_scores": {str(n): None for n in numbers}, "rounds": [], "stages": [], "checks": {},
              "seed": "seed.json", "notice": "合成种子专项测试，不是原始数据库恢复或连续验收；未验证真实微信或现实准时送达。"}
    call_stages = {}
    audit = None
    if tail:
        report.update(selection=selection, skipped_rounds=selection["skipped_rounds"])

    class SpecializedMock(MockLLM):
        def _create(self, **kwargs):
            if _AGENT.get() == "ChatAgent":
                user = next((m.get("content") for m in reversed(kwargs["messages"]) if m["role"] == "user"), "")
                reminder = any(m["role"] == "system" and "这是用户之前明确要求的提醒" in str(m.get("content"))
                               for m in kwargs["messages"])
                text = ("该交作业了。" if reminder else
                        "早。" if user == "早" else "好，那先不聊了。")
                return self._completion(text, str(kwargs.get("model")))
            return super()._create(**kwargs)

    def perform(number, stage, action):
        recorder.start_round(number)
        recorder.active_stage = stage
        mark, refused, runs = len(recorder.calls), len(recorder.refused), len(recorder.runs)
        entry = {"id": stage, "round": number}
        try:
            return action()
        except BaseException as exc:
            entry["execution_error"] = exc.__class__.__name__
            raise
        finally:
            calls = recorder.calls[mark:]
            for call in calls:
                call_stages[call.call_no] = stage
            entry.update(calls=[{**call_detail(c), "stage": stage} for c in calls],
                         refused_calls=[{**r, "stage": stage} for r in recorder.refused[refused:]],
                         runs=[dict(r) for r in recorder.runs[runs:]])
            report["stages"].append(entry)
            recorder.active_stage = None

    def healthy():
        if recorder.refused or recorder.stopped:
            report["execution_state"] = "stopped_budget"
            raise SpecializedStop("budget")
        if any(not c.ok for c in recorder.calls):
            report["execution_state"] = "failed_model_call"
            raise SpecializedStop("model_call")

    try:
        prepare_environment(workdir, args.config)
        os.environ["VIOLET_DAILY_LIFE"] = "0"       # phase 8.9: rounds 28-32 tail of reports recorded before it existed
        budget = budget_from(args)
        if min(budget.max_calls_per_turn, budget.max_calls_total, budget.max_tokens_total) <= 0:
            raise ValueError("Budget caps must be positive")
        models = {role: os.environ.get(key, "?") for role, key in MODEL_KEYS.items()}
        endpoint = args.api_url or os.environ.get("VIOLET_TEST_API_URL")
        if args.real:
            validate_endpoint(endpoint)
        gate = RealApiGate(input_fn=input_fn, key_prompt=key_prompt or getpass.getpass)
        # Reuse the existing gate, but print neither the key nor its prefix/suffix in this tool.
        def gate_out(line):
            out("  Key : <hidden>" if line.lstrip().startswith("Key :") else line)
        key = gate.open(args.real, endpoint, budget, models, assume_yes=False, out=gate_out)
        recorder = make_recorder(budget, [key] if key else [])
        if tail:
            from tools.memory_query_audit import MemoryQueryAudit
            audit = MemoryQueryAudit(recorder)
            report.update(memory_query_audit=audit.queries, chat_context_audit=audit.chat_requests)
        report.update(real=key is not None, models=models, execution_state="running")
        report["notice"] += " 真实文字模型路径。" if key else " 模拟。"
        guard = NetworkGuard([endpoint] if key else []).install()
        from ai.chat import violetdb
        from ai.profile_store import Profile
        from tools.dialogue_eval import _proactive
        with (nullcontext() if key else patch("tools.harness.MockLLM", tail_mock() if tail else SpecializedMock)):
            session = VioletTestSession(workdir, recorder, real=key is not None, owns_db=True, memory_audit=audit,
                                     clock=ScenarioClock(datetime.fromisoformat(seed["state"]["scenario_time"]["value"])))
            if tail:
                run_tail(session, seed, report, perform, healthy, stop=SpecializedStop, numbers=numbers, audit=audit)
            else:
                cid = apply_seed(session, seed)
                before = violetdb.fetch_all("SELECT * FROM interaction_preferences ORDER BY id")
                if not key:
                    # The offline adversarial regression remains; real mode runs MemoryAgent itself.
                    session.memory_script = [{"tool": "set_interaction_preference", "args": {
                        "key": "proactive_chat", "value": "no", "evidence": "算了不想聊了"}}]
                r27 = perform(27, "round27.chat", lambda: session.say("算了不想聊了", 27))
                after = violetdb.fetch_all("SELECT * FROM interaction_preferences ORDER BY id")
                counts = Counter(c["agent"] for c in r27["calls"])
                extra = sorted(set(counts) - {"IntentAgent", "ChatAgent", "MemoryAgent"})
                report["checks"].update(ending_checks(r27["violet"]))
                report["checks"].update(no_long_term_preference_write=before == after, no_unnecessary_agents=not extra,
                    round27_execution_complete=r27["background_complete"] and not r27["execution_errors"])
                report["rounds"].append({"n": 27, **r27, "preference_attempts": session.memory_script_results,
                    "agent_call_review": {"by_agent": dict(counts), "unexpected_agents": extra,
                                          "note": "Repeated Chat/Memory calls remain available for human review."},
                    "human_review": "未执行：请人工评估自然、简短、不追问、不说教。" if key else
                                    "未执行：模拟回复；请人工评估自然、简短、不追问、不说教。"})
                healthy()
                Profile(violetdb).preferences.set_explicit("proactive_chat", "no", source="dashboard", actor="synthetic_probe")
                report["preference_probe"] = {"source": "synthetic", "key": "proactive_chat", "value": "no",
                    "note": "Separate control condition, not a historical preference or round-27 effect."}
                jump = session.advance(20)
                r28 = {"n": 28, "time_advance": jump, "on_time_real_delivery_verified": False}
                report["rounds"].append(r28)
                entry = perform(28, "round28.reminder_due", lambda: _proactive(
                    session, "allowed_or_skipped_without_bypass", time_advance=jump))
                r28["reminder"] = entry
                healthy()
                [reminder] = entry["reminders"]
                row = violetdb.get_commitment(cid)
                event = violetdb.get_proactive_event(reminder["proactive_event_id"]) if reminder["proactive_event_id"] else None
                fallback = bool(event and event.get("reason") == "fallback template used")
                r28["reminder_used_fallback"] = fallback
                report["checks"].update(
                    advance_20_hours=jump["hours"] == 20,
                    reminder_sent=entry["ok"] is True and reminder["delivery"] == "sent",
                    reminder_model_content_valid=not fallback,
                    no_ordinary_chat_mixed=not entry["chat_proactive"]["sent"],
                    delivery_ids_match=row["proactive_event_id"] == reminder["proactive_event_id"]
                        and reminder["commitment_id"] == cid and row["status"] == reminder["event_status"] == "sent",
                    outbox_sent=bool(reminder["outbox"]) and all(o["status"] == "sent" for o in reminder["outbox"]),
                    time_jump_delay=reminder["late_minutes"] >= 253 and "时间跳跃" in (reminder["late_cause"] or ""))
                sent_mark = len(session.wechat.sent)
                repeat = perform(28, "round28.repeat_check", lambda: _proactive(session, "allowed_or_skipped_without_bypass"))
                r28["repeat"] = repeat
                healthy()
                report["checks"]["no_duplicate_reminder"] = len(session.wechat.sent) == sent_mark and not repeat["reminders"]
                if not key:
                    session.memory_script = []
                returned = perform(28, "round28.user_return", lambda: session.say("早", 28))
                r28["user_return"] = returned
                r28.update({k: v for k, v in returned.items() if k != "calls"})
                report["checks"]["user_return_replied"] = bool(returned["violet"]) and not returned["execution_errors"]
                healthy()
                cfg_path = workdir / "proactive.json"
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg["quiet_hours"] = {"start": "00:00", "end": "23:59"}
                cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
                quiet = perform(28, "round28.quiet_chat_probe", lambda: _proactive(session, "allowed_or_skipped_without_bypass"))
                r28["quiet_probe"] = quiet
                healthy()
                report["checks"]["ordinary_chat_respects_quiet"] = quiet["ok"] and quiet["due_reason"] == "quiet hours" and not quiet["sent"]
                quiet_cid = violetdb.add_commitment("静默探针", violetdb.now(), source_turn_id="synthetic:quiet-probe")["commitment"]["id"]
                quiet_reminder = perform(28, "round28.quiet_reminder_probe", lambda: _proactive(session, "allowed_or_skipped_without_bypass"))
                r28["quiet_reminder_probe"] = quiet_reminder
                healthy()
                report["checks"]["reminder_respects_quiet"] = (not quiet_reminder["sent"] and
                    violetdb.get_commitment(quiet_cid)["status"] == "pending" and
                    quiet_reminder["reminders"][0]["delivery"] == "postponed_quiet_hours")
                report["runtime_provenance"] = {"source": "synthetic", "commitment_id": cid,
                    "note": "All local IDs, assigned evidence, write/audit times and defaults are synthetic. "
                            "Message text and round-start times come from seed fields; reply send times are unknown."}
                report["execution_state"] = "complete"
    except PermissionError:
        report["execution_state"] = "authorization_refused"
    except SpecializedStop:
        pass
    except BaseException as exc:
        report["execution_error"] = exc.__class__.__name__
        report["execution_state"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed_execution"
    finally:
        if session is not None:
            report["close_errors"] = session.close()
        if guard is not None:
            guard.uninstall()
        report["network_guard"] = guard.summary() if guard is not None else None
        calls = [{**call_detail(c), "stage": call_stages.get(c.call_no, "unassigned")} for c in recorder.calls]
        report.update(calls=calls, total_calls=len(calls), call_summary=summarize_calls(recorder.calls, recorder.runs),
                      budget=recorder.budget_state(), budget_reservations=recorder.reservations)
        for row in report["rounds"]:
            row["calls"] = [c for c in calls if c["round"] == row["n"]]
        report["accounting"] = {"by_stage": {s["id"]: len(s["calls"]) for s in report["stages"]},
                                "by_round": {str(n): sum(c["round"] == n for c in calls) for n in numbers
                                             if any(c["round"] == n for c in calls)}}
        report["executed_rounds"] = sorted({c["round"] for c in calls})
        report["checks"]["calls_fully_attributed"] = (all(c["stage"] != "unassigned" and c["round"] in numbers for c in calls)
            and sum(len(s["calls"]) for s in report["stages"]) == len(calls)
            and sum(report["accounting"]["by_round"].values()) == len(calls))
        report["checks"]["model_calls_ok"] = all(c["ok"] for c in calls) and not recorder.refused
        report["status"] = ("未执行" if report.get("execution_state") == "authorization_refused" else
            "通过" if report.get("execution_state") == "complete" and all(report["checks"].values()) and not report.get("close_errors") else "失败")
        target = workdir / "specialized_report.json"
        if target.exists():
            raise FileExistsError("Refusing to overwrite specialized evidence")
        write_report(target.name, report, recorder, workdir)
        with (workdir / "specialized_scores.csv").open("x", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["purpose", "synthetic", "round", "score(0/1/2)", "note"])
            for n in numbers:
                writer.writerow([seed["purpose"], True, n, "", "专项人工评分；不计入原32轮；真实文字模型不代表真实微信送达"])
    return report


def build_parser():
    parser = argparse.ArgumentParser(description="合成专项套件；默认离线，真实文字模型必须门禁与确认")
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--suite", choices=(PURPOSE, "round29_32_specialized_test"), default=PURPOSE)
    parser.add_argument("--rounds", choices=("29-32", "30-32", "31-32"), help="Only for the tail suite; default 29-32")
    parser.add_argument("--api-url")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--max-calls-per-round", type=int, default=10)
    parser.add_argument("--max-calls", type=int, default=16)
    parser.add_argument("--max-tokens", type=int, default=150_000)
    parser.add_argument("--price-per-1k", type=float)
    parser.add_argument("--max-cost", type=float)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    report = run_specialized(args.workdir, args=args)
    print(f"{report['notice']}\n结果：{report['status']}；目录：{args.workdir}")
    return 0 if report["status"] == "通过" else 2 if report["status"] == "未执行" else 1


if __name__ == "__main__":
    raise SystemExit(main())
