"""Post-hoc offline reassessment of a recorded real round-30 reply under the current answer assertion.

Reads an existing 29–32 specialized run and writes a NEW report next to nothing it reads. It never calls a
model, never edits the source run and never turns unexecuted rounds 31–32 into a result.
"""
from __future__ import annotations

import argparse
import json

from tools.legacy import upgrade
import re
import subprocess
from pathlib import Path

from tools.synthetic_rounds import PROJECT, digest, write_new
from tools.synthetic_tail import ANSWER_ASSERTION, PURPOSE, round30_checks

KIND = "post_hoc_offline_reassessment"
# Hashed evidence; the run's env placeholder is deliberately never opened. The names are those of the recorded
# 2026-10-19 run directory (written before the rename to Violet).
EVIDENCE = ("specialized_report.json", "specialized_scores.csv", "seed.json", "seed_materialized.json",
            "eve-test.db", "proactive.json", ".eve-synthetic.json")
OLD_ASSERTION = "round30.answer_recalls_outcome/v1"


def answer_v1(text):
    """The rule recorded by fbd8a46, kept only to show the old verdict next to the new one."""
    return "顺利" in text and bool(re.search("结束|完成|通过|过了|搞定", text))


def _head(project):
    """HEAD plus whether the assertion code differs from it, so an uncommitted rule is never attributed to HEAD."""
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--", "tools"], cwd=project, text=True).strip()
        return {"head": head, "tools_uncommitted": bool(dirty)}
    except (OSError, subprocess.CalledProcessError):
        return {"head": None, "tools_uncommitted": None}


def reassess(source, project=PROJECT):
    """Return (result, hashes). Insufficient or inconsistent evidence yields 无法验证, never 通过."""
    source = Path(source)
    hashes = {name: digest(source / name) for name in EVIDENCE if (source / name).is_file()}
    report = upgrade(json.loads((source / "specialized_report.json").read_text(encoding="utf-8")))
    problems = []
    if report.get("real") is not True or report.get("verification_environment") is not None:
        problems.append("source is not a real-model run")
    if report.get("purpose") != PURPOSE:
        problems.append("source is not the 29–32 specialized suite")
    rows = [r for r in report.get("rounds", []) if r.get("n") == 30]
    row = rows[0] if len(rows) == 1 else {}
    if len(rows) != 1:
        problems.append("round 30 row missing or duplicated")
    evidence = row.get("recall_evidence") or {}
    events = [e for e in (report.get("initial_state") or {}).get("events", []) if e.get("id") == evidence.get("target_id")]
    calls = row.get("calls") or []
    recorded = row.get("checks") or {}
    if not row.get("violet"):
        problems.append("no recorded reply")
    if not calls or any(c.get("round") != 30 or c.get("ok") is not True for c in calls):
        problems.append("round 30 calls missing or failed")
    if len(events) != 1 or "returned" not in evidence or "chat_event_lines" not in evidence:
        problems.append("recall evidence or target event missing")
    audits = [a for a in report.get("chat_context_audit", []) if a.get("round") == 30]
    if len(audits) != 1 or audits[0].get("target_event_id") != evidence.get("target_id"):
        problems.append("chat context audit missing for the target")
    text = "".join(row.get("violet") or [])
    result = {"recorded_checks": recorded, "reply": row.get("violet"), "rubric": row.get("rubric")}
    if not problems:
        complete = row.get("background_complete") is True and not row.get("execution_errors") and bool(row.get("violet"))
        new = round30_checks(text, complete=complete, calls=calls, recalled=evidence["returned"],
                             chat_lines=evidence["chat_event_lines"], target=events[0])
        old = {**new, "answer_recalls_outcome": answer_v1(text)}
        if old != recorded:
            problems.append("recorded checks cannot be reproduced from recorded evidence")
        if new["correct_event_in_chat"] != audits[0].get("target_event_in_context"):
            problems.append("chat event line disagrees with chat context audit")
        result.update(recomputed_checks_v1=old, reassessed_checks=new, differences=[
            {"check": k, "recorded": recorded.get(k), "v1": old[k], "v2": new[k]} for k in new if old[k] != new[k]])
    result["evidence_problems"] = problems
    result["status"] = ("无法验证" if problems else
                        "通过" if all(result["reassessed_checks"].values()) else "失败")
    later = [{"n": r["n"], "status": r.get("status"), "reassessed": False,
              "note": "原运行未执行；离线复评不能产生结果。"} for r in report.get("rounds", []) if r.get("n") in (31, 32)]
    return {"kind": KIND, "synthetic": True, "real_model_reexecuted": False, "model_calls": 0,
        "notice": "事后离线复评：仅用新断言重新判定已记录的真实回复与结构证据；未重新执行或调用任何模型，"
                  "原报告、数据库与分数未修改；不计入原32轮，不代表人工评分。",
        "violet_head": _head(project), "assertion": {"field": "round30.answer_recalls_outcome",
            "old": OLD_ASSERTION, "new": ANSWER_ASSERTION},
        "source": {"dir": str(source), "sha256": hashes, "status": report.get("status"),
                   "execution_state": report.get("execution_state"), "total_calls": report.get("total_calls"),
                   "tokens": (report.get("budget") or {}).get("used_tokens")},
        "original_scores_unchanged": {k: report.get(k) for k in (
            "original_32_score", "manual_scores", "continuous_acceptance_eligible", "counts_toward_original_32")},
        "round30": result, "rounds_31_32": later,
        "overall": "第30轮复评" + result["status"] + "；31–32未执行，整组专项未完成。"}, hashes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", required=True, help="new directory; must not exist")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    value, before = reassess(args.source)
    after = {name: digest(Path(args.source) / name) for name in before}
    value["source_unchanged"] = after == before
    write_new(out / "round30_reassessment.json", value)
    print(value["overall"], "source_unchanged=" + str(value["source_unchanged"]))
    return 0 if value["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
