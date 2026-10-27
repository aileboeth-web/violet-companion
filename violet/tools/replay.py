"""Cross-round replay and continuation for tools.dialogue_eval (phase 7 B.3, multi-report B.5).

    --replay-from <report.json> [--replay-from <next report.json> ...]
                                  rebuild the rounds before the first selected round from REAL test reports
                                  (in order, contiguous, each one verified), offline, then continue live
    --workdir <dir> (kept)        later runs continue from the next round of that directory
    --seed-from <kept dir> --continue-replay <report.json> --continue-replay-sha256 <hex>
                                  (phase 7 B.6.2) copy a kept directory into a new one, rebuild its next round(s)
                                  offline from the real report that continued exactly that state, then go on

What a replay restores, and from where:
  * user lines        -- the acceptance plan (the report's lines must match it exactly)
  * Violet's replies     -- the report's recorded real replies, sent through the normal pipeline
  * profile facts     -- report final_state values, attributed to rounds by call_details;
                         the evidence is the value itself, which must occur verbatim in that round's line
  * event memories    -- report final_state content / kind / progress; the date only if the content
                         itself contains one
  * Violet's life        -- the plan's preload; "shared" is reproduced by the pipeline from the replies
  * situations        -- kind / value from the report's final state (by situation id), expiry recorded by the
                         round's own busy check or final expiry plus later legacy shifts; evidence = whole line
  * scenario clock    -- anchored at the original run's start (preload event time + plan hours_ago),
                         each round advanced by the report's recorded call latencies; never host time.
                         Reports written before phase 7 B.4 (no per-round scenario_time) replay their time
                         passing with the same legacy timestamp shift they were recorded with
Anything the report does not contain is listed under not_restored instead of being invented.

State of a kept directory: .violet-eval-state.json -- replay status (in_progress / complete / failed),
completed rounds with their mode (replay / real / mock) and the scenario time at the end. A directory
with history but no state, a failed or unfinished replay, a gap or an overlap in rounds, or mock rounds
before a real run are refused: nothing is replayed twice and no real test data is overwritten.
"""

from __future__ import annotations

import copy
import hashlib
import json

from tools.legacy import upgrade
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

STATE_FILE = ".violet-eval-state.json"
STATE_VERSION = 1
SYNTHETIC_MARKER = ".violet-synthetic.json"
# directories made before the rename to Violet carry the old marker name -- they are refused just the same
SYNTHETIC_MARKERS = (SYNTHETIC_MARKER, ".eve-synthetic.json")
SPECIALIZED_PURPOSES = {"round27_28_specialized_test", "round29_32_specialized_test"}
DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
FACT_WRITE_ACTIONS = {"created", "superseded_previous", "confirmed"}
NO_STATE_CHANGE = {"query_memory", "memory_done"}

NOT_RESTORED = [
    "profile fact evidence text: the report has no tool arguments; the replay uses the value itself, which "
    "appears verbatim in that round's user line",
    "profile / event / message timestamps: approximated by the scenario clock (original run start + recorded "
    "call latencies); the original wall-clock times of single writes are not in the report",
    "event time of day, valid_until, importance and emotional weight: not in the report (replay defaults); "
    "only a date written in the memory content itself is used",
    "database ids (messages, facts, events, outbound rows) and the intent results of the original run",
    "the chat context of the original rounds (what Intent recalled, which facts were shown)",
    "legacy core memories: the report's final state does not list them",
]


class ReplaySourceError(ValueError):
    """Refused before anything runs (bad report, bad directory state)."""


def require_continuous_scope(data: dict[str, Any]) -> None:
    if (data.get("synthetic") or data.get("purpose") in SPECIALIZED_PURPOSES
            or data.get("continuous_acceptance_eligible") is False or data.get("counts_toward_original_32") is False):
        raise ReplaySourceError("合成专项测试不得进入原连续验收、回放或32轮总分")


class PlanTimeError(RuntimeError):
    """A plan round asks for a scenario time that lies in the scenario's past."""


# ---------------------------------------------------------------------------
# Plan revisions (phase 7 B.4)
# ---------------------------------------------------------------------------
# The acceptance plan itself never changes (its hash is recorded in every kept directory). A revision
# is a separate file <plan stem>.rev<N>.json bound to the base plan's hash; it may only set or remove
# these round keys, and a kept directory refuses a revision that touches rounds it already completed.
REVISION_KEYS = {"time", "advance_hours"}


def load_plan(path: Path, revision: str | Path | None = "auto") -> tuple[dict[str, Any], dict[str, Any]]:
    path = Path(path)
    raw = path.read_bytes()
    plan = upgrade(json.loads(raw.decode("utf-8")))
    require_continuous_scope(plan)
    meta = {"plan": str(path), "base_sha256": hashlib.sha256(raw).hexdigest(), "revision": 1,
            "revision_file": None, "revision_sha256": None, "changed_rounds": []}
    if revision in (None, "none"):
        return plan, meta
    if revision == "auto":
        found = []
        for candidate in sorted(path.parent.glob(f"{path.stem}.rev*.json")):
            data = upgrade(json.loads(candidate.read_text(encoding="utf-8")))
            if data.get("base") == path.name:
                found.append((int(data.get("revision", 0)), candidate))
        if not found:
            return plan, meta
        rev_path = max(found)[1]
    else:
        rev_path = Path(revision)
    rev_raw = rev_path.read_bytes()
    rev = upgrade(json.loads(rev_raw.decode("utf-8")))
    if rev.get("base") != path.name or rev.get("base_sha256") != meta["base_sha256"]:
        raise ReplaySourceError(f"修正版剧本 {rev_path.name} 不是针对当前的 {path.name} 写的（基础剧本哈希不一致），拒绝使用")
    plan = copy.deepcopy(plan)
    by_n = {r["n"]: r for r in plan["rounds"]}
    changed = []
    for key, change in (rev.get("rounds") or {}).items():
        n = int(key)
        if n not in by_n:
            raise ReplaySourceError(f"修正版剧本改动了不存在的第 {n} 轮")
        for field in change.get("remove") or []:
            if field not in REVISION_KEYS:
                raise ReplaySourceError(f"修正版剧本不允许改动第 {n} 轮的 {field}")
            by_n[n].pop(field, None)
        for field, value in (change.get("set") or {}).items():
            if field not in REVISION_KEYS:
                raise ReplaySourceError(f"修正版剧本不允许改动第 {n} 轮的 {field}")
            by_n[n][field] = value
        changed.append(n)
    meta.update(revision=int(rev["revision"]), revision_file=str(rev_path),
                revision_sha256=hashlib.sha256(rev_raw).hexdigest(), changed_rounds=sorted(changed))
    return plan, meta


def scenario_target(spec: dict[str, Any], base_start: datetime, tz) -> datetime:
    """A plan time relative to a round's date: weekday `weekday` (ISO, 1 = Monday) of the week `weeks_ahead`
    weeks after the base round's week, plus `plus_days`, at `at` (HH:MM) in Violet's time zone."""
    base_day: date = base_start.astimezone(tz).date()
    monday = base_day - timedelta(days=base_day.weekday())
    day = monday + timedelta(days=7 * int(spec.get("weeks_ahead", 1)) + int(spec["weekday"]) - 1
                             + int(spec.get("plus_days", 0)))
    hour, minute = (spec.get("at") or "10:00").split(":", 1)
    return datetime(day.year, day.month, day.day, int(hour), int(minute), tzinfo=tz)


def round_start(db, plan: dict[str, Any], n: int, times: dict[int, str]) -> datetime:
    """When round n started in the scenario: recorded time, else the stored time of its user line."""
    if n in times:
        return datetime.fromisoformat(times[n])
    line = next((r["user"] for r in plan["rounds"] if r["n"] == n), None)
    row = db.fetch_one("SELECT created_at FROM messages WHERE role = 'user' AND content = ? ORDER BY id LIMIT 1",
                       (line,)) if line else None
    if row is None:
        raise PlanTimeError(f"无法确定第 {n} 轮发生的场景时间")
    return datetime.fromisoformat(row["created_at"])


# ---------------------------------------------------------------------------
# Loading and validating a replay source
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


LEGACY_BUSY_ADVANCE_HOURS = 1.6        # what the blocked_busy check advanced (tools.dialogue_eval._proactive)
ROUND_TIME_TOLERANCE_S = 2.0
SEGMENT_END_TOLERANCE_S = 120.0


def _load_report(path: Path) -> dict[str, Any]:
    try:
        report = upgrade(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ReplaySourceError(f"回放报告 {Path(path).name} 无法读取：{exc.__class__.__name__}") from None
    require_continuous_scope(report)
    return report


def uses_legacy_time(report: dict[str, Any]) -> bool:
    """Reports written before phase 7 B.4 let time pass by shifting stored timestamps (tools.harness.advance_time)
    instead of moving the clock; they have no per-round scenario_time."""
    return not any("scenario_time" in r for r in report.get("rounds") or [])


def _after_checks(planned: dict[str, Any], n: int) -> list[str]:
    if planned.get("before"):
        raise ReplaySourceError(f"第 {n} 轮在剧本里有轮前的主动消息检查，回放不支持")
    checks = list((planned.get("after") or {}).values())
    unsupported = [c for c in checks if c != "blocked_busy"]
    if unsupported:
        raise ReplaySourceError(f"第 {n} 轮的主动消息检查 {unsupported} 回放不支持")
    return checks


def _segment(path: Path, report: dict[str, Any], plan_rounds: dict[int, dict[str, Any]], rounds_wanted: list[int]) -> dict[str, Any]:
    """One report = one replay segment: its rounds, the writes the report confirms, its recorded checks."""
    final = report.get("final_state")
    if not isinstance(final, dict):
        raise ReplaySourceError(f"回放报告 {path.name} 缺少 final_state")
    facts = final.get("facts") or []
    memories = final.get("memories") or []
    situations = final.get("situations") or []
    by_n = {r.get("n"): r for r in report.get("rounds") or []}
    legacy = uses_legacy_time(report)
    out_rounds = []
    for n in rounds_wanted:
        planned, recorded = plan_rounds[n], by_n[n]
        if recorded.get("user") != planned["user"] or recorded.get("planned_user", planned["user"]) != planned["user"]:
            raise ReplaySourceError(f"第 {n} 轮的用户原话与验收剧本不一致，拒绝回放")
        after = _after_checks(planned, n)
        violet = recorded.get("violet")
        if not isinstance(violet, list) or not violet or not all(isinstance(x, str) and x for x in violet):
            raise ReplaySourceError(f"第 {n} 轮没有记录 Violet 的回复")
        details = recorded.get("call_details")
        if not isinstance(details, list):
            raise ReplaySourceError(f"第 {n} 轮缺少调用明细（需要 Phase 7 B.2 之后的报告），无法确认写入了什么")
        recorded_checks = recorded.get("proactive") or []
        steps, writes = [], []
        for call in details:
            if call.get("agent") != "MemoryAgent":
                continue
            for result in call.get("tool_results") or []:
                tool, action = result.get("tool"), result.get("action")
                if tool in NO_STATE_CHANGE or result.get("ok") is False:
                    continue
                if tool == "remember_profile_fact" and action == "already_known":
                    continue
                if tool == "remember_profile_fact" and action in FACT_WRITE_ACTIONS:
                    key = result.get("key")
                    active = [f for f in facts if f.get("fact_key") == key and f.get("status") == "active"]
                    if len(active) != 1:
                        raise ReplaySourceError(f"第 {n} 轮写入的档案 {key} 在报告最终状态里不是唯一的有效值")
                    value = active[0]["value"]
                    if not value or value not in planned["user"]:
                        raise ReplaySourceError(f"第 {n} 轮档案 {key} 的值没有逐字出现在用户原话里，无法确认证据")
                    steps.append({"tool": "remember_profile_fact", "args": {"key": key, "value": value, "evidence": value}})
                    writes.append({"type": "profile_fact", "key": key, "value": value,
                                   "source": "report final_state (value) + call_details (round, key, action)",
                                   "evidence": "the value itself, verbatim in the user line (original evidence not recorded)"})
                    continue
                if tool == "save_memory" and action == "created":
                    found = [m for m in memories if m.get("id") == result.get("memory_id")]
                    if len(found) != 1:
                        raise ReplaySourceError(f"第 {n} 轮保存的事件记忆在报告最终状态里找不到")
                    memory = found[0]
                    if memory.get("status") != "active":
                        raise ReplaySourceError(f"第 {n} 轮的事件记忆最终状态是 {memory.get('status')}，回放不支持")
                    args = {"summary": memory["content"], "kind": memory.get("kind") or "event",
                            "progress": memory.get("progress")}
                    date = DATE_RE.search(memory["content"])
                    if date:
                        args["event_date"] = date.group(1)
                    steps.append({"tool": "save_memory", "args": args})
                    writes.append({"type": "event_memory", "content": memory["content"], "kind": args["kind"],
                                   "progress": args["progress"],
                                   "event_date": args.get("event_date"),
                                   "event_date_source": "date written in the memory content" if date else "not recorded",
                                   "source": "report final_state (content, kind, progress) + call_details (round)"})
                    continue
                if tool == "set_situation" and action == "created":
                    sid = result.get("situation_id")
                    # final_state lists situations ORDER BY id; ids start at 1 and situations are never deleted
                    if not isinstance(sid, int) or not 1 <= sid <= len(situations):
                        raise ReplaySourceError(f"第 {n} 轮写入的情境 {sid} 在报告最终状态里找不到")
                    situation = situations[sid - 1]
                    if not situation.get("kind") or not situation.get("value") or not situation.get("expires_at"):
                        raise ReplaySourceError(f"第 {n} 轮写入的情境 {sid} 缺少类型、内容或过期时间，无法精确恢复")
                    seen = [c.get("busy_situation") for c in recorded_checks
                            if (c.get("busy_situation") or {}).get("id") == sid]
                    if seen and (seen[0].get("kind"), seen[0].get("value")) != (situation["kind"], situation["value"]):
                        raise ReplaySourceError(f"第 {n} 轮的情境 {sid} 与同轮记录的忙碌检查不一致")
                    writes.append({"type": "situation", "situation_id": sid, "kind": situation["kind"],
                                   "value": situation["value"], "final_expires_at": situation["expires_at"],
                                   "recorded_expires_at": seen[0].get("expires_at") if seen else None,
                                   "evidence": "the whole user line (original evidence not recorded)"})
                    steps.append({"tool": "set_situation", "args": {"kind": situation["kind"], "value": situation["value"],
                                                                    "evidence": planned["user"], "until": None}})
                    continue
                raise ReplaySourceError(f"第 {n} 轮包含回放不支持的写入：{tool}（{action}）")
        latency = sum(int(c.get("latency_ms") or 0) for c in details)
        out_rounds.append({"n": n, "user": planned["user"], "violet": list(violet), "steps": steps, "writes": writes,
                           "plan_memory": planned.get("memory") or [], "advance_hours": planned.get("advance_hours"),
                           "after": after, "recorded_checks": recorded_checks,
                           "recorded_time": recorded.get("scenario_time"),
                           "duration_s": max(latency, int(recorded.get("reply_latency_ms") or 0)) / 1000})
    produced_facts = {w["key"] for r in out_rounds for w in r["writes"] if w["type"] == "profile_fact"}
    produced_events = {w["content"] for r in out_rounds for w in r["writes"] if w["type"] == "event_memory"}
    produced_situations = {w["situation_id"] for r in out_rounds for w in r["writes"] if w["type"] == "situation"}
    return {"source": str(path), "sha256": _sha256(path), "rounds": out_rounds, "legacy_time": legacy,
            "final": final, "facts": facts, "memories": memories, "situations": situations,
            "scenario_clock_end": (report.get("scenario_clock") or {}).get("end"),
            "replayed_before": report.get("replay"), "produced": {"facts": produced_facts, "events": produced_events,
                                                                  "situations": produced_situations}}


def load_replay(report_path: Path, plan: dict[str, Any], first_live_round: int,
                changed_rounds: list[int] | None = None) -> dict[str, Any]:
    return load_replay_chain([report_path], plan, first_live_round, changed_rounds)


def load_replay_chain(report_paths: list[Path], plan: dict[str, Any], first_live_round: int,
                      changed_rounds: list[int] | None = None) -> dict[str, Any]:
    """Several real reports in order (e.g. rounds 1-5, then rounds 6-10), validated as one history."""
    paths = [Path(p) for p in (report_paths if isinstance(report_paths, (list, tuple)) else [report_paths])]
    reports = [_load_report(p) for p in paths]
    for path, report in zip(paths, reports):
        if report.get("real") is not True:
            raise ReplaySourceError("回放来源必须是真实模型测试的报告（report.real = true），模拟回复不能当作聊天记录")
    plan_rounds = {r["n"]: r for r in plan["rounds"]}
    needed = [n for n in sorted(plan_rounds) if n < first_live_round]
    if not needed:
        raise ReplaySourceError("没有需要回放的轮次：--rounds 必须从第 2 轮或之后开始")
    touched = sorted(set(changed_rounds or []) & set(needed))
    if touched:
        raise ReplaySourceError(f"剧本修正版改动了要回放的第 {touched} 轮，回放的历史与剧本不一致")
    per_report = [sorted(r.get("n") for r in report.get("rounds") or []) for report in reports]
    expected_next, segments = needed[0], []
    for i, (path, report, numbers) in enumerate(zip(paths, reports, per_report)):
        if not numbers:
            raise ReplaySourceError(f"回放报告 {path.name} 没有任何轮次")
        if i == 0 and report.get("replay"):
            raise ReplaySourceError(f"第一份回放报告 {path.name} 本身是在回放之上跑的，请把它的来源报告也按顺序给出")
        if len(set(numbers)) != len(numbers):
            raise ReplaySourceError(f"回放报告 {path.name} 里有重复的轮次")
        if numbers != list(range(numbers[0], numbers[-1] + 1)):
            gap = [n for n in range(numbers[0], numbers[-1] + 1) if n not in numbers]
            raise ReplaySourceError(f"回放报告缺少第 {gap} 轮（{path.name} 的轮次不连续：{numbers}）")
        if numbers[0] > expected_next:
            raise ReplaySourceError(f"回放报告缺少第 {list(range(expected_next, numbers[0]))} 轮")
        if numbers[0] < expected_next:
            raise ReplaySourceError(f"回放报告 {path.name} 与前面的报告重叠或顺序不对（第 {numbers[0]} 轮已经有了）")
        if i > 0:
            parent = report.get("replay") or {}
            earlier = [n for numbers_before in per_report[:i] for n in numbers_before]
            if (report.get("continuation") or {}).get("mode") != "replay" or parent.get("rounds") != earlier:
                raise ReplaySourceError(f"无法确认报告 {path.name} 是接在前面的报告之后跑的（它没有回放第 {earlier} 轮的记录）")
            if i == 1 and parent.get("sha256") != _sha256(paths[0]):
                raise ReplaySourceError(f"报告 {path.name} 回放的来源不是 {paths[0].name}（哈希不一致）")
            if i > 1:
                raise ReplaySourceError("超过两段的回放链无法逐段确认来源，暂不支持")
        expected_next = numbers[-1] + 1
    missing = [n for n in needed if n >= expected_next]
    if missing:
        raise ReplaySourceError(f"回放报告缺少第 {missing} 轮")
    extra = [n for numbers in per_report for n in numbers if n not in needed]
    if extra:
        raise ReplaySourceError(f"回放报告还包含第 {extra} 轮：它的最终状态含有这些轮次的结果，不能只回放到第 {needed[-1]} 轮")
    for path, report, numbers in zip(paths, reports, per_report):
        segments.append(_segment(path, report, plan_rounds, numbers))

    # every item of each segment's final state must come from a round of this or an earlier segment
    for i, seg in enumerate(segments):
        made = {k: set().union(*(s["produced"][k] for s in segments[:i + 1])) for k in ("facts", "events", "situations")}
        for fact in seg["facts"]:
            if fact.get("status") != "active":
                raise ReplaySourceError(f"报告最终状态里有非有效的档案历史（{fact.get('fact_key')}），回放无法重建")
            if fact.get("fact_key") not in made["facts"]:
                raise ReplaySourceError(f"无法确认档案 {fact.get('fact_key')} 是哪一轮写入的")
        for memory in seg["memories"]:
            if memory.get("content") not in made["events"]:
                raise ReplaySourceError("报告最终状态里有无法确认来源轮次的事件记忆")
        if set(range(1, len(seg["situations"]) + 1)) != made["situations"]:
            raise ReplaySourceError(f"报告 {Path(seg['source']).name} 最终状态里的情境无法逐条确认是哪一轮写入的")

    # expiry of each replayed situation: the value recorded right after it was written when the report has one,
    # else the final value plus the legacy time shifts that happened after it was written
    all_rounds = [r for seg in segments for r in seg["rounds"]]
    legacy_of = {r["n"]: seg["legacy_time"] for seg in segments for r in seg["rounds"]}
    for seg in segments:
        for r in seg["rounds"]:
            for step, write in zip([s for s in r["steps"] if s["tool"] == "set_situation"],
                                   [w for w in r["writes"] if w["type"] == "situation"]):
                later = sum((LEGACY_BUSY_ADVANCE_HOURS if "blocked_busy" in q["after"] else 0)
                            + (float(q["advance_hours"] or 0) if q["n"] > r["n"] else 0)
                            for q in all_rounds if q["n"] >= r["n"] and legacy_of[q["n"]])
                derived = (datetime.fromisoformat(write["final_expires_at"]) + timedelta(hours=later)).isoformat()
                if write["recorded_expires_at"] and write["recorded_expires_at"] != derived:
                    raise ReplaySourceError(f"第 {r['n']} 轮情境的记录过期时间与最终状态推算的不一致，无法确认时间")
                step["args"]["until"] = write["recorded_expires_at"] or derived
                write["original_expires_at"] = step["args"]["until"]
                write["expires_source"] = ("recorded by the round's busy check before any later time shift"
                                           if write["recorded_expires_at"] else
                                           f"final_state expires_at + {later} h of later legacy time shifts")

    spec = (plan.get("preload") or {}).get("life_event") or {}
    life = [e for e in segments[0]["final"].get("life_events") or [] if e.get("content") == spec.get("content")]
    if not spec or len(life) != 1 or not life[0].get("occurred_at"):
        raise ReplaySourceError("报告里没有剧本预置的生活事件时间，无法确定剧本时钟")
    anchor = datetime.fromisoformat(life[0]["occurred_at"]) + timedelta(hours=float(spec.get("hours_ago", 20)))
    if anchor.tzinfo is None:
        raise ReplaySourceError("报告里的生活事件时间没有时区，无法确定剧本时钟")
    for i, seg in enumerate(segments):
        seg["expected"] = {
            "facts": sorted((f["fact_key"], f["value"], f["status"]) for f in seg["facts"]),
            "memories": sorted((m["content"], m.get("kind"), m.get("progress"), m.get("status")) for m in seg["memories"]),
            "life_events": sorted((e["content"], int(e.get("share_count") or 0)) for e in seg["final"].get("life_events") or []),
            "situations": sorted((s.get("kind"), s.get("value"), s.get("expires_at")) for s in seg["situations"]),
            "commitments": sorted((c.get("content"), c.get("status")) for c in seg["final"].get("commitments") or []),
        }
        seg["life_times"] = {e["content"]: e.get("occurred_at") for e in seg["final"].get("life_events") or []}
    rounds = all_rounds
    return {
        "source": segments[0]["source"] if len(segments) == 1 else [s["source"] for s in segments],
        "sha256": segments[0]["sha256"] if len(segments) == 1 else [s["sha256"] for s in segments],
        "segments": segments, "anchor": anchor.isoformat(), "rounds": rounds,
        "expected": segments[-1]["expected"],
        "differences_vs_plan": differences_vs_plan(rounds),
        "not_restored": NOT_RESTORED + ([
            "situation evidence text: the whole user line is used (the report keeps no tool arguments)",
            "situation observed_at: the scenario clock at the replayed write (seconds from the original)",
        ] if any(w["type"] == "situation" for r in rounds for w in r["writes"]) else []),
    }


def differences_vs_plan(rounds: list[dict[str, Any]]) -> list[str]:
    """Where the real run's memory differs from the plan's scripted memory steps (labelled, not hidden)."""
    out = []
    for r in rounds:
        planned = []
        for step in r["plan_memory"]:
            args = step.get("args") or {}
            if step.get("tool") in ("remember_profile_fact", "correct_profile_fact"):
                planned.append(("profile_fact", args.get("key"), args.get("value")))
            elif step.get("tool") == "save_memory":
                planned.append(("event_memory", None, args.get("summary")))
            elif step.get("tool") == "set_situation":
                planned.append(("situation", args.get("kind"), args.get("value")))
            else:
                planned.append((step.get("tool"), args.get("kind") or args.get("key"), args.get("value")))
        restored = []
        for w in r["writes"]:
            if w["type"] == "profile_fact":
                restored.append(("profile_fact", w["key"], w["value"]))
            elif w["type"] == "event_memory":
                restored.append(("event_memory", None, w["content"]))
            else:
                restored.append(("situation", w["kind"], w["value"]))
        for item in planned:
            if item not in restored:
                out.append(f"第 {r['n']} 轮：剧本写入 {item[0]} {item[1] or ''} = {item[2]!r}，"
                           f"真实报告没有这样写（未按剧本恢复）")
        for item in restored:
            if item not in planned:
                out.append(f"第 {r['n']} 轮：按真实报告恢复 {item[0]} {item[1] or ''} = {item[2]!r}（剧本里没有这一条）")
    return out


# ---------------------------------------------------------------------------
# Running a replay
# ---------------------------------------------------------------------------

def current_state(db) -> dict[str, list[tuple]]:
    return {
        "facts": sorted((r["fact_key"], r["value"], r["status"]) for r in
                        db.fetch_all("SELECT fact_key, value, status FROM user_profile_facts")),
        "memories": sorted((r["content"], r["kind"], r["progress"], r["status"]) for r in
                           db.fetch_all("SELECT content, kind, progress, status FROM event_memories")),
        "life_events": sorted((r["content"], int(r["share_count"] or 0)) for r in
                              db.fetch_all("SELECT content, share_count FROM life_events")),
        "situations": sorted((r["kind"], r["value"], r["expires_at"]) for r in
                             db.fetch_all("SELECT kind, value, expires_at FROM user_situations")),
        "commitments": sorted((r["content"], r["status"]) for r in db.fetch_all("SELECT content, status FROM commitments")),
    }


def _replay_after_checks(session, r: dict[str, Any], legacy: bool) -> list[dict[str, Any]]:
    """Re-run the round's proactive checks the way the original run did -- offline, and compared with the record."""
    from tools.harness import ReplayError, advance_time
    done = []
    recorded = {c.get("check"): c for c in r["recorded_checks"]}
    for check in r["after"]:                                # only blocked_busy is accepted by the loader
        busy = session.scheduler._busy_situation()
        if legacy:
            advance_time(session.db, LEGACY_BUSY_ADVANCE_HOURS)  # the pre-B.4 mechanism, exactly as recorded
        else:
            session.clock.advance(LEGACY_BUSY_ADVANCE_HOURS)
        result = session.proactive_tick()
        if result["sent"]:
            raise ReplayError(f"第 {r['n']} 轮回放的主动消息检查发出了消息")
        want = recorded.get(check)
        if want is not None and want.get("due_reason") != result["due_reason"]:
            raise ReplayError(f"第 {r['n']} 轮回放的主动消息检查结果与记录不一致："
                              f"{result['due_reason']!r} ≠ {want.get('due_reason')!r}")
        if want is not None and busy is not None and (want.get("busy_situation") or {}).get("expires_at") not in (
                None, busy["expires_at"]):
            raise ReplayError(f"第 {r['n']} 轮回放的忙碌情境时间与记录不一致")
        done.append({"check": check, "due_reason": result["due_reason"], "matches_record": want is not None})
    return done


def _check_end_of_round_situations(session, r: dict[str, Any]) -> dict[str, Any] | None:
    """A recorded busy_cleared check lists the situations as they were at the end of that round."""
    from tools.harness import ReplayError
    want = next((c for c in r["recorded_checks"] if c.get("check") == "busy_cleared"), None)
    if want is None:
        return None
    session.scheduler._busy_situation()                     # applies expiry exactly like the original check did
    have = [{k: row[k] for k in ("kind", "value", "status", "expires_at")} for row in
            session.db.fetch_all("SELECT * FROM user_situations WHERE kind IN ('availability', 'activity') ORDER BY id")]
    expected = [{k: s.get(k) for k in ("kind", "value", "status", "expires_at")}
                for s in sorted(want.get("situations") or [], key=lambda s: s.get("id") or 0)]
    if have != expected:
        raise ReplayError(f"第 {r['n']} 轮结束时的情境与记录不一致：{have} ≠ {expected}")
    return {"round": r["n"], "situations": have}


def _verify_segment(session, seg: dict[str, Any], messages: list[tuple[str, str]]) -> dict[str, Any]:
    from tools.harness import ReplayError
    actual = current_state(session.db)
    mismatches = [f"{k}: 回放 {actual[k]} ≠ 报告 {[tuple(x) for x in v]}" for k, v in seg["expected"].items()
                  if actual[k] != [tuple(x) for x in v]]
    if mismatches:
        raise ReplayError("回放后的状态与真实报告不一致：" + "；".join(mismatches))
    rows = [(m["role"], m["content"]) for m in session.db.fetch_all("SELECT role, content FROM messages ORDER BY id")]
    if rows != messages:
        raise ReplayError(f"回放后的聊天记录与报告不一致（{len(rows)} 条 ≠ {len(messages)} 条，或内容/顺序不同）")
    life = {}
    for row in session.db.fetch_all("SELECT content, occurred_at FROM life_events"):
        want = seg["life_times"].get(row["content"])
        if want:
            drift = abs((datetime.fromisoformat(row["occurred_at"]) - datetime.fromisoformat(want)).total_seconds())
            if drift > ROUND_TIME_TOLERANCE_S:
                raise ReplayError(f"Violet 生活事件的时间与报告相差 {drift:.3f} 秒")
            life[row["content"]] = round(drift, 3)
    return {"source": seg["source"], "rounds": [r["n"] for r in seg["rounds"]], "messages": len(rows),
            "state": sorted(seg["expected"]), "life_event_time_drift_s": life}


def run_replay(session, replay: dict[str, Any], out: Callable[[str], None] = print) -> dict[str, Any]:
    """Replay the rounds through the normal pipeline, offline, segment by segment. Raises ReplayError on any
    difference from the reports (messages, state, recorded checks, times)."""
    from tools.harness import ReplayError, advance_time
    if session.clock is None:
        raise ReplayError("a replay needs the scenario clock")
    # Phase 8.9: every report replayed here was recorded before Violet's ordinary-day items (ai/daily_life.py)
    # existed; they would add life events the report cannot have, so a replay never creates them
    os.environ["VIOLET_DAILY_LIFE"] = "0"
    anchor = datetime.fromisoformat(replay["anchor"])
    starts, offset, verified, end_checks, after_checks, messages = [], 0.0, [], [], [], []
    clock_checks = []
    session.phase = "replay"
    try:
        for i, seg in enumerate(replay["segments"]):
            parent = seg.get("replayed_before") or {}
            if i > 0 and parent.get("scenario_clock", {}).get("end_of_replay"):
                recorded_end = datetime.fromisoformat(parent["scenario_clock"]["end_of_replay"])
                drift = abs((anchor + timedelta(seconds=offset) - recorded_end).total_seconds())
                clock_checks.append({"check": f"start of {Path(seg['source']).name}", "drift_s": round(drift, 3)})
                if drift > ROUND_TIME_TOLERANCE_S:
                    raise ReplayError(f"回放到 {Path(seg['source']).name} 开头时的场景时间与它记录的不一致（{drift:.1f} 秒）")
            for r in seg["rounds"]:
                if r.get("recorded_time"):
                    # phase 7 B.4+ reports record each round's scenario time, plan advances included
                    when = datetime.fromisoformat(r["recorded_time"])
                    offset = (when - anchor).total_seconds()
                else:
                    when = anchor + timedelta(seconds=offset)
                session.clock.set(when)
                starts.append({"n": r["n"], "scenario_time": when.isoformat()})
                if r.get("advance_hours") and seg["legacy_time"]:
                    advance_time(session.db, float(r["advance_hours"]))   # recorded under the pre-B.4 mechanism
                session.replay_reply = "\n".join(r["violet"])
                session.memory_script = r["steps"]
                result = session.say(r["user"], r["n"])
                if result["violet"] != r["violet"]:
                    raise ReplayError(f"第 {r['n']} 轮回放后发出的消息与记录不一致")
                if result["calls"]:
                    raise ReplayError(f"第 {r['n']} 轮回放产生了计费的模型调用")
                messages += [("user", r["user"])] + [("assistant", t) for t in r["violet"]]
                session.memory_script = None
                if r["after"]:
                    after_checks += [{"round": r["n"], **c} for c in _replay_after_checks(session, r, seg["legacy_time"])]
                    if not seg["legacy_time"]:
                        offset += LEGACY_BUSY_ADVANCE_HOURS * 3600 * len(r["after"])   # the clock moved forward
                checked = _check_end_of_round_situations(session, r)
                if checked:
                    end_checks.append(checked)
                out(f"[{r['n']:>2}] （回放）你: {r['user']}")
                offset += r["duration_s"]
            verified.append(_verify_segment(session, seg, messages))
            if seg.get("scenario_clock_end"):
                drift = abs((anchor + timedelta(seconds=offset) - datetime.fromisoformat(seg["scenario_clock_end"])).total_seconds())
                clock_checks.append({"check": f"end of {Path(seg['source']).name}", "drift_s": round(drift, 3)})
                if drift > SEGMENT_END_TOLERANCE_S:
                    raise ReplayError(f"{Path(seg['source']).name} 结束时的场景时间与记录相差 {drift:.0f} 秒")
        end = anchor + timedelta(seconds=offset)
        session.clock.set(end)
    finally:
        session.phase, session.replay_reply, session.memory_script = "live", None, None
    from ai.profile_store import Profile
    effective = [{k: s[k] for k in ("id", "kind", "value", "expires_at")} for s in Profile(session.db).situations.current()]
    provenance = []
    for row in session.db.fetch_all("SELECT id, source_turn_id FROM user_situations ORDER BY id"):
        job = session.db.fetch_one("SELECT user_message FROM memory_jobs WHERE turn_id = ?", (row["source_turn_id"],))
        provenance.append({"situation_id": row["id"], "written_by_memory_job_of": job["user_message"] if job else None})
    return {
        "source": replay["source"], "sha256": replay["sha256"], "rounds": [r["n"] for r in replay["rounds"]],
        "segments": verified,
        "scenario_clock": {"anchor": replay["anchor"], "round_starts": starts, "end_of_replay": end.isoformat(),
                           "checks": clock_checks,
                           "how": "original run start (plan preload event time + hours_ago) plus the recorded call "
                                  "latencies of each round; approximate within seconds. Rounds recorded before phase "
                                  "7 B.4 replay their time passing with the same legacy timestamp shift."},
        "legacy_time_rounds": [r["n"] for seg in replay["segments"] if seg["legacy_time"] for r in seg["rounds"]
                               if r.get("advance_hours") or r["after"]],
        "proactive_checks_replayed": after_checks, "end_of_round_situations": end_checks,
        "effective_situations_at_end": effective, "situation_provenance": provenance,
        "verified_against_report": sorted(replay["expected"]),
        "restored_writes": {r["n"]: r["writes"] for r in replay["rounds"]},
        "differences_vs_plan": replay["differences_vs_plan"],
        "not_restored": replay["not_restored"],
        "billed_calls": 0, "billed_tokens": 0, "offline_answers": session.recorder.replay_calls,
    }


# ---------------------------------------------------------------------------
# Continuation replay (phase 7 B.6.2): the next round(s) of a kept continuation directory, rebuilt
# offline from a real report that was continued from exactly that directory's state
# ---------------------------------------------------------------------------

CN_DATE_RE = re.compile(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})")
CONTINUATION_COUNTED = ("user_profile_facts", "user_situations", "commitments", "life_events", "profile_questions",
                        "event_memories", "messages")
CONTINUATION_NOT_RESTORED = [
    "save_memory arguments other than the stored content: category, importance, emotional weight, valid_until "
    "and the time of day are replay defaults (the report keeps no tool arguments); the date is the one written "
    "in the memory content",
    "the text of query_memory calls (they change nothing; their recorded result counts are kept in the report)",
    "the intent result and the chat context of the original round (Intent answers a plain chat route offline)",
    "timestamps of the replayed messages, memory job and memory: the scenario time the round started at; "
    "the original finish times of the background job are not in the report",
    "topic bookkeeping written by the pipeline (topic mentions) is re-created by the pipeline, not compared",
]


def _event_date(content: str) -> str | None:
    m = CN_DATE_RE.search(content or "")
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def _later_touches(report: dict[str, Any], after_round: int, memory_id: int) -> bool:
    for r in report.get("rounds") or []:
        if r.get("n", 0) <= after_round:
            continue
        for call in r.get("call_details") or []:
            for res in call.get("tool_results") or []:
                if res.get("memory_id") == memory_id or memory_id in (res.get("memories_ids") or []):
                    return True
    return False


def _state_before_next_jump(report: dict[str, Any], after_round: int, memory_id: int,
                            final: dict[str, Any]) -> tuple[str | None, str | None]:
    """(progress, status) of a memory right after `after_round`: the `before` value of the first later time
    jump that changed it, else its final state (only time can have changed it -- checked by the caller)."""
    for r in sorted(report.get("rounds") or [], key=lambda r: r.get("n", 0)):
        if r.get("n", 0) <= after_round:
            continue
        for change in (r.get("time_advance") or {}).get("events_changed") or []:
            if change.get("id") == memory_id:
                before = change.get("before") or [None, None]
                return before[0], before[1]
    return final.get("progress"), final.get("status")


def load_continuation_replay(report_path: Path, expected_sha256: str | None, plan: dict[str, Any],
                             plan_meta: dict[str, Any], state: dict[str, Any], first_live_round: int) -> dict[str, Any]:
    """Validate everything that can be checked before a session exists. Raises ReplaySourceError."""
    path = Path(report_path)
    if not expected_sha256:
        raise ReplaySourceError("续跑回放必须给出来源报告的 SHA-256（--continue-replay-sha256），被改动过的报告会被拒绝")
    if not path.is_file():
        raise ReplaySourceError(f"回放报告 {path} 不存在")
    sha = _sha256(path)
    if sha != expected_sha256.strip().lower():
        raise ReplaySourceError(f"回放报告 {path.name} 的哈希与给定的不一致（报告可能被改动过），拒绝回放")
    report = _load_report(path)
    if report.get("real") is not True:
        raise ReplaySourceError("回放来源必须是真实模型测试的报告（report.real = true）")
    done = [r["n"] for r in state.get("rounds") or []]
    if not done:
        raise ReplaySourceError("测试目录里没有已完成的轮次，不能做续跑回放")
    needed = list(range(max(done) + 1, first_live_round))
    if not needed:
        raise ReplaySourceError(f"测试目录已完成到第 {max(done)} 轮，第 {first_live_round} 轮之前没有需要回放的轮次"
                                f"（不会重复回放）")
    cont = report.get("continuation") or {}
    if cont.get("mode") != "continue" or cont.get("completed_rounds_before") != done:
        raise ReplaySourceError(f"报告 {path.name} 不是接在这个目录已完成的第 {done[0]}-{done[-1]} 轮之后跑的，拒绝回放")
    rp = report.get("plan") or {}
    if rp.get("base_sha256") != state.get("plan_sha256") or rp.get("base_sha256") != plan_meta["base_sha256"]:
        raise ReplaySourceError("报告、测试目录和当前验收剧本的剧本哈希不一致，拒绝回放")
    if rp.get("revision") != plan_meta["revision"] or rp.get("revision_sha256") != plan_meta["revision_sha256"]:
        raise ReplaySourceError("报告用的剧本修正版与当前的不同，拒绝回放")
    by_n = {r.get("n"): r for r in report.get("rounds") or []}
    missing = [n for n in needed if n not in by_n]
    if missing:
        raise ReplaySourceError(f"报告 {path.name} 里没有第 {missing} 轮")
    started = by_n[needed[0]].get("scenario_time")
    if not state.get("clock_end") or not started or abs(
            (datetime.fromisoformat(started) - datetime.fromisoformat(state["clock_end"])).total_seconds()) > 0.001:
        raise ReplaySourceError(f"报告第 {needed[0]} 轮开始的场景时间与测试目录结束时的场景时间不一致，"
                                f"它们不是同一条时间线，拒绝回放")
    plan_rounds = {r["n"]: r for r in plan["rounds"]}
    final = report.get("final_state") or {}
    memories = {m.get("id"): m for m in final.get("memories") or []}
    rounds, existing = [], {}
    for n in needed:
        planned, rec = plan_rounds[n], by_n[n]
        if rec.get("user") != planned["user"] or rec.get("planned_user", planned["user"]) != planned["user"]:
            raise ReplaySourceError(f"第 {n} 轮的用户原话与验收剧本不一致，拒绝回放")
        unsupported = [k for k in ("before", "after", "time", "advance_hours") if planned.get(k)]
        if unsupported or rec.get("time_advance") or rec.get("proactive"):
            raise ReplaySourceError(f"第 {n} 轮带有时间推进或主动消息检查（{unsupported}），续跑回放不支持")
        violet = rec.get("violet")
        if not isinstance(violet, list) or not violet or not all(isinstance(x, str) and x for x in violet):
            raise ReplaySourceError(f"第 {n} 轮没有记录 Violet 的回复")
        details = rec.get("call_details")
        if not isinstance(details, list) or len(details) != rec.get("calls"):
            raise ReplaySourceError(f"第 {n} 轮的调用明细不完整，无法确认写入了什么")
        results = []
        for call in details:
            for res in call.get("tool_results") or []:
                tool = res.get("tool")
                if call.get("agent") != "MemoryAgent":
                    if tool != "query_memory":
                        raise ReplaySourceError(f"第 {n} 轮的 {call.get('agent')} 调用了 {tool}，续跑回放不支持")
                    continue
                if tool not in NO_STATE_CHANGE:
                    results.append(res)
        steps, expects, writes = [], [], []
        i = 0
        while i < len(results):
            res = results[i]
            if res.get("tool") != "save_memory" or res.get("action") not in ("needs_decision", "created"):
                raise ReplaySourceError(f"第 {n} 轮包含续跑回放不支持的写入：{res.get('tool')}（{res.get('action')}）")
            asked = res if res.get("action") == "needs_decision" else None
            created = results[i + 1] if asked else res
            if asked and (i + 1 >= len(results) or created.get("tool") != "save_memory"
                          or created.get("action") != "created"):
                raise ReplaySourceError(f"第 {n} 轮的 needs_decision 之后没有新建记忆，续跑回放不支持")
            mid = created.get("memory_id")
            memory = memories.get(mid)
            if not isinstance(mid, int) or memory is None:
                raise ReplaySourceError(f"第 {n} 轮新建的记忆 {mid} 在报告最终状态里找不到")
            if _later_touches(report, n, mid):
                raise ReplaySourceError(f"第 {n} 轮新建的记忆 {mid} 在之后的轮次里被改动过，无法确认它当时的样子")
            progress, status = _state_before_next_jump(report, n, mid, memory)
            if status != "active" or progress not in ("planned", "ongoing", "done", "cancelled", "unknown"):
                raise ReplaySourceError(f"第 {n} 轮新建的记忆 {mid} 当时的状态无法确认（{progress}/{status}）")
            args = {"summary": memory["content"], "kind": memory.get("kind") or "event", "progress": progress,
                    "category": "general"}
            date_text = _event_date(memory["content"])
            if date_text:
                args["event_date"] = date_text
            if asked:
                for cid in asked.get("candidates_ids") or []:
                    other = memories.get(cid)
                    if other is None or _later_touches(report, n, cid):
                        raise ReplaySourceError(f"第 {n} 轮的相似候选 {cid} 无法从报告确认")
                    existing[cid] = {"content": other["content"],
                                     "progress": _state_before_next_jump(report, n, cid, other)[0],
                                     "status": _state_before_next_jump(report, n, cid, other)[1]}
                steps.append({"tool": "save_memory", "args": dict(args)})
                expects.append({"action": "needs_decision", "candidates_ids": sorted(asked.get("candidates_ids") or [])})
            steps.append({"tool": "save_memory", "args": {**args, "new_event": bool(asked)}})
            expects.append({"action": "created", "memory_id": mid})
            writes.append({"type": "event_memory", "memory_id": mid, "content": memory["content"],
                           "kind": args["kind"], "progress": progress, "event_date": date_text,
                           "source": "report final_state (content, kind) + later time jump `before` (progress) + "
                                     "call_details (round, needs_decision candidates, created id)",
                           "event_date_source": "date written in the memory content" if date_text else "not recorded"})
            i += 2 if asked else 1
        rounds.append({"n": n, "user": planned["user"], "violet": list(violet), "steps": steps, "expects": expects,
                       "writes": writes, "scenario_time": rec.get("scenario_time")})
    nxt = by_n.get(needed[-1] + 1)
    if nxt and (nxt.get("time_advance") or {}).get("from"):
        end, end_source = nxt["time_advance"]["from"], f"recorded: the clock when round {needed[-1] + 1} started, before its time jump"
    elif nxt and nxt.get("scenario_time"):
        end, end_source = nxt["scenario_time"], f"recorded: the start of round {needed[-1] + 1}"
    else:
        last = by_n[needed[-1]]
        spent = sum(int(c.get("latency_ms") or 0) for c in last.get("call_details") or []) / 1000
        end = (datetime.fromisoformat(last["scenario_time"]) + timedelta(seconds=spent)).isoformat()
        end_source = "approximate: round start + recorded call latencies"
    return {"source": str(path), "sha256": sha, "rounds": rounds, "existing": existing,
            "end_clock": end, "end_clock_source": end_source, "not_restored": list(CONTINUATION_NOT_RESTORED)}


def run_continuation_replay(session, replay: dict[str, Any], out: Callable[[str], None] = print) -> dict[str, Any]:
    """Replay the round(s) offline on the continuation directory and verify them. Raises ReplayError."""
    from tools.harness import ReplayError
    db = session.db
    for mid, want in replay["existing"].items():
        row = db.fetch_one("SELECT content, progress, status FROM event_memories WHERE id = ?", (mid,))
        have = dict(row) if row else None
        if have != want:
            raise ReplayError(f"测试目录里的记忆 {mid} 与报告记录的起点不一致：{have} ≠ {want}")
    created = [w["memory_id"] for r in replay["rounds"] for w in r["writes"]]
    seq = db.fetch_one("SELECT seq FROM sqlite_sequence WHERE name = 'event_memories'")
    top = db.fetch_one("SELECT MAX(id) AS m FROM event_memories")
    next_id = max(int(seq["seq"]) if seq else 0, int(top["m"] or 0)) + 1
    if created and created[0] != next_id:
        raise ReplayError(f"下一条事件记忆的 id 是 {next_id}，报告里是 {created[0]}：目录状态与报告的起点不一致")
    unfinished = db.fetch_all("SELECT id, status FROM memory_jobs WHERE status != 'done'")
    if unfinished:
        raise ReplayError(f"测试目录里还有没完成的记忆任务 {unfinished}，拒绝回放")
    before = {t: db.fetch_one(f"SELECT COUNT(*) AS c FROM {t}")["c"] for t in CONTINUATION_COUNTED}
    start = session.clock.now().isoformat()
    session.phase = "replay"
    checked = []
    try:
        for r in replay["rounds"]:
            session.clock.set(datetime.fromisoformat(r["scenario_time"]))
            session.replay_reply = "\n".join(r["violet"])
            session.memory_script, session.memory_script_results = r["steps"], []
            result = session.say(r["user"], r["n"])
            if result["violet"] != r["violet"]:
                raise ReplayError(f"第 {r['n']} 轮回放后发出的消息与记录不一致")
            if result["calls"]:
                raise ReplayError(f"第 {r['n']} 轮回放产生了计费的模型调用")
            got = []
            for item in session.memory_script_results:
                res = item["result"] if isinstance(item["result"], dict) else {}
                entry = {"action": res.get("action")}
                if res.get("action") == "needs_decision":
                    entry["candidates_ids"] = sorted(c.get("id") for c in res.get("candidates") or [])
                if res.get("action") == "created":
                    entry["memory_id"] = res.get("memory_id")
                got.append(entry)
            if got != r["expects"]:
                raise ReplayError(f"第 {r['n']} 轮回放的记忆写入与记录不一致：{got} ≠ {r['expects']}")
            for w in r["writes"]:
                row = db.fetch_one("SELECT content, kind, progress, status, event_time FROM event_memories WHERE id = ?",
                                   (w["memory_id"],))
                if row is None or (row["content"], row["kind"], row["progress"], row["status"]) != (
                        w["content"], w["kind"], w["progress"], "active"):
                    raise ReplayError(f"第 {r['n']} 轮回放的记忆 {w['memory_id']} 与报告不一致")
                if w["event_date"] and not (row["event_time"] or "").startswith(w["event_date"]):
                    raise ReplayError(f"第 {r['n']} 轮回放的记忆 {w['memory_id']} 的日期不是 {w['event_date']}")
            checked.append({"round": r["n"], "messages": 1 + len(r["violet"]), "memory_results": got})
            session.memory_script = None
            out(f"[{r['n']:>2}] （续跑回放）你: {r['user']}")
    finally:
        session.phase, session.replay_reply, session.memory_script = "live", None, None
    after = {t: db.fetch_one(f"SELECT COUNT(*) AS c FROM {t}")["c"] for t in CONTINUATION_COUNTED}
    expected = dict(before)
    expected["messages"] += sum(1 + len(r["violet"]) for r in replay["rounds"])
    expected["event_memories"] += len(created)
    if after != expected:
        raise ReplayError(f"回放后的记录数与预期不一致（没有凭空新增或丢失）：{after} ≠ {expected}")
    pending = {
        "memory_jobs": db.fetch_all("SELECT id, status FROM memory_jobs WHERE status != 'done'"),
        "inbound_messages": db.fetch_all("SELECT id, status FROM inbound_messages WHERE status != 'done'"),
        "outbound_messages": db.fetch_all("SELECT id, status FROM outbound_messages WHERE status != 'sent'"),
    }
    if any(pending.values()):
        raise ReplayError(f"回放后还有未完成的后台任务：{pending}")
    end = datetime.fromisoformat(replay["end_clock"])
    session.clock.set(end)
    return {
        "mode": "continuation_replay", "source": replay["source"], "sha256": replay["sha256"],
        "rounds": [r["n"] for r in replay["rounds"]], "checked": checked,
        "verified": ["user lines vs plan", "Violet replies verbatim", "memory results (needs_decision candidates, created id)",
                     "created memory content / kind / progress / date", "existing candidate memories unchanged",
                     "record counts (only the expected messages and memories added)", "no unfinished background work",
                     "start time = directory clock_end"],
        "restored_writes": {r["n"]: r["writes"] for r in replay["rounds"]},
        "scenario_clock": {"start": start, "round_starts": [{"n": r["n"], "scenario_time": r["scenario_time"]}
                                                            for r in replay["rounds"]],
                           "end": end.isoformat(), "end_source": replay["end_clock_source"]},
        "record_counts": {"before": before, "after": after},
        "not_restored": replay["not_restored"],
        "billed_calls": 0, "billed_tokens": 0, "offline_answers": session.recorder.replay_calls,
    }


# ---------------------------------------------------------------------------
# Directory state (continuation, no double replay)
# ---------------------------------------------------------------------------

def read_state(workdir: Path) -> dict[str, Any] | None:
    if any((Path(workdir) / marker).exists() for marker in SYNTHETIC_MARKERS):
        raise ReplaySourceError("合成专项测试目录不得用于原连续验收")
    path = Path(workdir) / STATE_FILE
    if not path.exists():
        return None
    try:
        state = upgrade(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {"version": STATE_VERSION, "corrupt": True}
    require_continuous_scope(state)
    return state


def write_state(workdir: Path, state: dict[str, Any]) -> None:
    path = Path(workdir) / STATE_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def count_messages(db_path: Path) -> int:
    if not Path(db_path).exists():
        return 0
    # Even a read-only connection to a WAL database creates -wal / -shm files next to it. Without pending
    # log data, open it as immutable: nothing is created in a kept directory that is only being inspected.
    wal = Path(f"{db_path}-wal")
    immutable = "" if wal.exists() and wal.stat().st_size > 0 else "&immutable=1"
    try:
        conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro{immutable}", uri=True)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise ReplaySourceError(f"无法读取测试目录里的数据库：{exc}") from None


def unfinished_memory_jobs(db_path: Path) -> list[dict[str, Any]]:
    """Memory jobs of a kept test directory that did not finish (read-only, like count_messages)."""
    if not Path(db_path).exists():
        return []
    wal = Path(f"{db_path}-wal")
    immutable = "" if wal.exists() and wal.stat().st_size > 0 else "&immutable=1"
    try:
        conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro{immutable}", uri=True)
        try:
            rows = conn.execute("SELECT id, turn_id, status, attempts, last_error FROM memory_jobs "
                                "WHERE status != 'done' ORDER BY id").fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise ReplaySourceError(f"无法读取测试目录里的数据库：{exc}") from None
    return [{"job_id": r[0], "turn_id": r[1], "status": r[2], "attempts": r[3],
             "error_type": (r[4] or "").split(":", 1)[0] or None} for r in rows]


def _revision_label(revision: int) -> str:
    return "原剧本" if revision == 1 else f"修正版 {revision}"


def revision_conflict(state: dict[str, Any], plan_meta: dict[str, Any]) -> str | None:
    """Would a completed round get a different plan text now than the one it was run with? (phase 7 B.7.1)

    Each round records the revision it ran with (plan_revision) and the state records each revision's
    file hash (plan_revisions). A round the current revision changes must have been run with this very
    revision file; a round run with another revision must be changed by neither of the two; a round
    without a record (directories from before phase 7 B.4) must not be changed by the current revision."""
    current = int(plan_meta["revision"])
    changed_now = set(plan_meta["changed_rounds"])
    recorded = state.get("plan_revisions") or {}
    for r in state.get("rounds") or []:
        n, used = r["n"], r.get("plan_revision")
        if used is None:
            if n in changed_now:
                return (f"{_revision_label(current)}改动了已经完成的第 [{n}] 轮，而目录状态里没有这一轮用哪个剧本版本"
                        f"跑的记录，拒绝继续")
            continue
        used = int(used)
        if used != current:
            if used != 1 and str(used) not in recorded:
                return f"第 {n} 轮用{_revision_label(used)}跑过，但目录状态里没有这个修正版的记录，拒绝继续"
            changed_then = set((recorded.get(str(used)) or {}).get("changed_rounds") or [])
            if n in changed_now or n in changed_then:
                return (f"已经完成的第 [{n}] 轮是用{_revision_label(used)}跑的，现在用{_revision_label(current)}，"
                        f"两者对这一轮不同，拒绝继续")
            continue
        if n in changed_now:
            entry = recorded.get(str(current)) or {}
            if not entry.get("sha256"):
                return f"已经完成的第 [{n}] 轮用{_revision_label(current)}跑过，但目录状态里没有它的文件哈希，拒绝继续"
            if entry["sha256"] != plan_meta["revision_sha256"]:
                return (f"已经完成的第 [{n}] 轮用{_revision_label(current)}跑过，但修正版文件之后被改动了"
                        f"（哈希不一致），拒绝继续")
    return None


def check_workdir(workdir: Path | None, replay_requested: bool, rounds: set[int], real: bool,
                  plan_meta: dict[str, Any] | None = None, accept_incomplete_memory: bool = False) -> dict[str, Any]:
    """Decide fresh / replay / continue for the target directory -- or refuse. Reads only.

    A round that ran is not a round whose background work finished: unfinished memory jobs refuse a
    continuation unless the tester accepts them explicitly (then they are listed in the result)."""
    if workdir is None or not Path(workdir).exists():
        return {"mode": "replay" if replay_requested else "fresh", "state": None}
    workdir = Path(workdir)
    state = read_state(workdir)
    history = count_messages(workdir / "violet-test.db")
    if replay_requested and (state is not None or history):
        raise ReplaySourceError(f"{workdir} 里已经有回放或聊天记录，拒绝再次回放（不会覆盖已有测试数据）")
    if state is None:
        if history:
            raise ReplaySourceError(f"{workdir} 里有聊天记录但没有评测状态文件，无法确认它的来历，拒绝使用")
        return {"mode": "replay" if replay_requested else "fresh", "state": None}
    if state.get("corrupt") or state.get("version") != STATE_VERSION:
        raise ReplaySourceError(f"{workdir} 的评测状态文件无法识别，拒绝使用")
    if plan_meta is not None and state.get("plan_sha256") != plan_meta["base_sha256"]:
        raise ReplaySourceError(f"{workdir} 是用另一个版本的验收剧本跑的（剧本哈希不一致），拒绝继续")
    replay = state.get("replay")
    if replay is not None and replay.get("status") != "complete":
        raise ReplaySourceError(f"{workdir} 的回放没有成功完成（{replay.get('status')}），不能在上面继续测试")
    unfinished = [c.get("status") for c in state.get("continuation_replays") or [] if c.get("status") != "complete"]
    if unfinished:
        raise ReplaySourceError(f"{workdir} 的续跑回放没有成功完成（{unfinished[0]}），不能在上面继续测试")
    done = [r["n"] for r in state.get("rounds") or []]
    if not done:
        raise ReplaySourceError(f"{workdir} 的评测状态里没有已完成的轮次，拒绝使用")
    if done != list(range(1, len(done) + 1)):
        raise ReplaySourceError(f"{workdir} 的已完成轮次不是从第 1 轮起连续不重复的（{done}），拒绝继续")
    overlap = sorted(set(done) & set(rounds))
    if overlap:
        raise ReplaySourceError(f"第 {overlap} 轮在 {workdir} 里已经完成，拒绝重复运行")
    if min(rounds) != max(done) + 1:
        raise ReplaySourceError(f"{workdir} 已完成到第 {max(done)} 轮，下一次必须从第 {max(done) + 1} 轮开始")
    if plan_meta is not None:
        problem = revision_conflict(state, plan_meta)
        if problem:
            raise ReplaySourceError(f"{workdir}：{problem}")
    if real and any(r.get("mode") == "mock" for r in state.get("rounds") or []):
        raise ReplaySourceError(f"{workdir} 里有模拟回复的轮次，不能在上面继续真实测试")
    unfinished = unfinished_memory_jobs(workdir / "violet-test.db")
    flagged = [r["n"] for r in state.get("rounds") or [] if r.get("background_complete") is False]
    if (unfinished or flagged) and not accept_incomplete_memory:
        jobs = "、".join(f"#{j['job_id']}（{j['status']}，{j['error_type'] or '无错误类型'}）" for j in unfinished)
        raise ReplaySourceError(
            f"{workdir} 里有没完成的记忆任务 {jobs or flagged}：这些轮次执行过，但后台记忆整理没有完成。"
            f"确认接受这个状态再继续请加 --accept-incomplete-memory（会写进报告）")
    return {"mode": "continue", "state": state, "unfinished_memory_jobs": unfinished,
            "rounds_with_unfinished_background_work": flagged}


def new_state(plan_path: Path, replay: dict[str, Any] | None) -> dict[str, Any]:
    return {"version": STATE_VERSION, "plan": str(plan_path), "plan_sha256": _sha256(plan_path), "plan_revisions": {},
            "replay": ({"source": replay["source"], "sha256": replay["sha256"],
                        "sources": [{"source": seg["source"], "sha256": seg["sha256"],
                                     "rounds": [r["n"] for r in seg["rounds"]], "legacy_time": seg["legacy_time"]}
                                    for seg in replay["segments"]],
                        "rounds": [r["n"] for r in replay["rounds"]], "status": "in_progress"} if replay else None),
            "rounds": [], "clock_end": None}
