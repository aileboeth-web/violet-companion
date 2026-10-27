"""Phase 8.1 chat-experience evaluation, fully offline.

build     freeze ~30 real scenarios (with provenance) + synthetic probes into one versioned set
verify    re-hash every source the set cites
blind     anonymous sheet (csv + readable md) for any variants' replies; mapping kept in a separate key
score     validate a filled sheet, unblind it and compare variants (scores keep reasons and evidence)
diagnose  length / name / question / repetition numbers -- diagnostic only, never an optimisation target

No model, network or Violet runtime module is imported. Sources are only read; outputs go to NEW paths.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json

from tools.legacy import upgrade
import random
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
VERSION = "p81.v1"
SET_PATH = "violet/tests/samples/chat_eval/scenarios.p81.v1.json"
PROBES_PATH = "violet/tests/samples/chat_eval/probes.p81.v1.json"
PLAN = "violet/tests/samples/acceptance_dialogue_30.json"
BASELINE = "historical_real"
OUT = "violet/tools/output/"
B113 = "local/b113/real-20261019-014631/"
B115 = "local/b115/real-20261019-041807/specialized_report.json"
B117 = "local/b117/real-20261019-053449/specialized_report.json"
B118 = "local/b118/real-20261019-060349/specialized_report.json"
B1110 = "local/b1110/real-20261019-070235/specialized_report.json"

# (id, primary report, round, tags, related runs of the same round whose context may differ)
REAL_SELECTION = [
    ("R01", OUT + "20261012-183438/report.json", 1, ["opening", "address"], [OUT + "20261012-095649/report.json"]),
    ("R02", OUT + "20261012-183438/report.json", 2, ["casual", "self_disclosure"], [OUT + "20261012-095649/report.json"]),
    ("R03", OUT + "20261012-183438/report.json", 3, ["casual"], [OUT + "20261012-095649/report.json"]),
    ("R04", OUT + "20261012-183438/report.json", 4, ["venting"], [OUT + "20261012-095649/report.json"]),
    ("R05", OUT + "20261012-183438/report.json", 5, ["violet_self", "violet_life"], [OUT + "20261012-095649/report.json"]),
    ("R06", OUT + "20261012-213429/report.json", 6, ["continuation", "violet_self"], []),
    ("R07", OUT + "20261012-213429/report.json", 7, ["disagreement", "violet_self"], []),
    ("R08", OUT + "20261012-213429/report.json", 8, ["short_reply"], []),
    ("R09", OUT + "20261012-213429/report.json", 9, ["ending"], []),
    ("R10", OUT + "20261012-213429/report.json", 10, ["venting", "short_reply"], []),
    ("R11", OUT + "20261013-003833/report.json", 11, ["joke"], [OUT + "20261012-224249/report.json"]),
    ("R12", OUT + "20261013-003833/report.json", 12, ["casual"], [OUT + "20261012-224249/report.json"]),
    ("R13", OUT + "20261013-003833/report.json", 13, ["joke", "context_use"], [OUT + "20261012-224249/report.json"]),
    ("R14", OUT + "20261013-051915/report.json", 14, ["help", "venting"], [OUT + "20261012-224249/report.json"]),
    ("R15", OUT + "20261013-065322/report.json", 15, ["good_news", "long_gap"],
     [OUT + "20261012-224249/report.json", OUT + "20261013-051915/report.json", OUT + "20261013-065732/report.json"]),
    ("R16", OUT + "20261013-073348/report.json", 16, ["short_reply", "ended_topic"], []),
    ("R17", OUT + "20261013-073654/report.json", 17, ["casual", "venting"], []),
    ("R18", OUT + "20261013-073654/report.json", 18, ["violet_self"], []),
    ("R19", OUT + "20261013-073654/report.json", 19, ["preference", "boundary"], []),
    ("R20", OUT + "20261013-073654/report.json", 20, ["preference", "casual"], []),
    ("R21", OUT + "20261013-095903/report.json", 21, ["image", "honesty"], []),
    ("R22", OUT + "20261013-095903/report.json", 22, ["image", "honesty"], []),
    ("R23", OUT + "20261014-001147/report.json", 23, ["help", "honesty"], [OUT + "20261013-221347/report.json"]),
    ("R24", OUT + "20261014-001147/report.json", 24, ["casual", "user_story"], [OUT + "20261013-221347/report.json"]),
    ("R25", OUT + "20261014-001147/report.json", 25, ["honesty", "violet_self"], [OUT + "20261013-221347/report.json"]),
    ("R26", OUT + "20261014-002003/report.json", 26, ["reminder"], [OUT + "20261013-221347/report.json"]),
    ("R27", B113 + "specialized_report.json", 27, ["ending", "boundary"], []),
    ("R28", B113 + "specialized_report.json", 28, ["long_gap", "short_reply"], []),
    ("R29", B115, 29, ["casual", "preference"], []),
    ("R30", B118, 30, ["memory_recall"], []),
    ("R30M", B115, 30, ["memory_miss", "honesty"], [B117]),
    ("R31", B1110, 31, ["correction", "honesty"], []),
    ("R32", B1110, 32, ["memory_recall", "short_reply"], []),
]

DIMENSIONS = {
    "natural": "日常微信聊天的自然程度",
    "context": "是否真正理解上下文（接住当前话题、时间与关系）",
    "grounded": "没有无依据的经历、理由或用户引述（2=无；1=轻微臆测；0=编造）",
    "rhythm": "互动节奏与长短是否合适",
    "self": "是否体现 Violet 自己的性格和想法",
    "intimacy": "撒娇、亲近是否符合当时关系（不适用填 na）",
    "respect": "是否尊重用户偏好和边界（不适用填 na）",
}
NA_ALLOWED = {"self", "intimacy", "respect"}
SHEET_FIELDS = ["item", "scenario", "candidate", "candidate_sha12", *DIMENSIONS, "rank", "grounded_evidence", "reason", "rater"]
ENVIRONMENTS = {"real_model", "fake_sdk_offline", "mock", "manual"}


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load(rel: str, root: Path) -> dict:
    return upgrade(json.loads((root / rel).read_text(encoding="utf-8")))


def _rel(path_text: str) -> str:
    """Report-internal paths are relative to Violet and may use backslashes."""
    text = path_text.replace("\\", "/")
    return text if text.startswith(("violet/", "local/")) else "violet/" + text


def _row(report: dict, n: int) -> dict:
    rows = [r for r in report.get("rounds", []) if r.get("n") == n]
    if len(rows) != 1:
        raise ValueError(f"round {n} missing or duplicated")
    return rows[0]


def _turns(n: int, user, violet, source: str, time=None) -> list[dict]:
    return [{"round": n, "role": "user", "text": user, "source": source, "time": time},
            {"round": n, "role": "violet", "text": "\n".join(violet) if isinstance(violet, list) else violet, "source": source, "time": time}]


def _plain(field):
    return field.get("value") if isinstance(field, dict) and "value" in field else field


def _history(report: dict, rel: str, n: int, root: Path, plan: dict, sources: dict) -> tuple[list[dict], list[str]]:
    """Earlier turns with their provenance; unknown Violet replies stay None, never filled in."""
    turns, missing, covered = [], [], set()
    replay = report.get("replay") or {}
    segments = replay.get("segments")
    if segments is None and isinstance(replay.get("source"), str):
        segments = [{"source": replay["source"], "rounds": replay.get("rounds") or []}]   # older single-source format
    for seg in segments or []:
        src = _rel(seg["source"])
        sources[src] = sha256_file(root / src)
        source_report = _load(src, root)
        for k in seg["rounds"]:
            if k < n:
                r = _row(source_report, k)
                turns += _turns(k, r["user"], r.get("violet") or [], f"replay_source:{src}")
                covered.add(k)
    if rel.startswith(B113):
        seed_rel = B113 + "seed.json"
        sources[seed_rel] = sha256_file(root / seed_rel)
        for d in _load(seed_rel, root)["state"]["dialogue"]:
            k = _plain(d["n"])
            turns += _turns(k, _plain(d["user"]), _plain(d["violet"]), f"specialized_seed:{seed_rel}", _plain(d.get("scenario_time")))
            covered.add(k)
    before = (report.get("continuation") or {}).get("completed_rounds_before") or []
    plan_rows = {r["n"]: r for r in plan["rounds"]}
    unknown = [k for k in before if k < n and k not in covered]
    for k in unknown:
        turns += [{"round": k, "role": "user", "text": plan_rows[k]["user"], "source": "plan_user_line", "time": None},
                  {"round": k, "role": "violet", "text": None, "source": "unknown_lost_test_directory", "time": None}]
    if unknown:
        missing.append(f"第{unknown[0]}–{unknown[-1]}轮在已丢失的测试目录中执行：只知道剧本里的用户原话，Violet 当时的回复未知")
    for r in report.get("rounds", []):
        k = r.get("n")
        if k is not None and k < n and r.get("violet") is not None:
            turns += _turns(k, r["user"], r.get("violet") or [], "same_report", r.get("scenario_time"))
            covered.add(k)
    turns.sort(key=lambda t: (t["round"], t["role"] != "user"))
    row = _row(report, n)
    for text in ((row.get("reminder") or {}).get("sent") or []):
        # A proactive reminder delivered in this round before the user wrote.
        turns.append({"round": n, "role": "violet", "text": text, "source": "same_report:proactive_reminder", "time": None})
    return turns, missing


def _scenario_time(report: dict, row: dict, n: int, rel: str, root: Path):
    if row.get("scenario_time"):
        return row["scenario_time"], "report_row"
    if (row.get("time_advance") or {}).get("to"):
        return row["time_advance"]["to"], "report_time_advance"
    for item in ((report.get("replay") or {}).get("scenario_clock") or {}).get("round_starts") or []:
        if item.get("n") == n:
            return item["scenario_time"], "replay_clock"
    if rel.startswith(B113) and n == 27:
        return _plain(_load(B113 + "seed.json", root)["state"]["scenario_time"]), "specialized_seed"
    return None, "unknown"


def _seed_state(rel: str, root: Path):
    return _load(B113 + "seed.json", root)["state"] if rel.startswith(B113) else None


def _preferences(report: dict, n: int, seed=None) -> tuple[list[dict], str]:
    if seed is not None:
        return [{"key": _plain(p["key"]), "value": _plain(p["value"]), "evidence": _plain(p.get("evidence"))}
                for p in seed.get("preferences", [])], "specialized_seed"
    initial = (report.get("initial_state") or {}).get("preferences")
    if initial is not None:
        return [{"key": p["pref_key"], "value": p["value"], "evidence": p.get("evidence")} for p in initial], "specialized_initial_state"
    found = []
    for r in report.get("rounds", []):
        if (r.get("n") or 0) >= n:
            continue
        for c in r.get("call_details") or []:
            for t in c.get("tool_results") or []:
                if t.get("tool") == "set_interaction_preference" and t.get("action") == "set":
                    found.append({"key": t.get("key"), "value": None, "evidence": f"round {r['n']} MemoryAgent set (value not recorded)"})
    return found, "same_report_tool_results" if found else "unknown"


def _real_scenario(sid, rel, n, tags, related, root: Path, plan: dict) -> dict:
    report = _load(rel, root)
    if report.get("real") is not True:
        raise ValueError(f"{rel} is not a real-model report")
    sources = {rel: sha256_file(root / rel)}
    row = _row(report, n)
    history, missing = _history(report, rel, n, root, plan, sources)
    time, time_source = _scenario_time(report, row, n, rel, root)
    seed = _seed_state(rel, root)
    prefs, pref_source = _preferences(report, n, seed)
    ctx = next((run.get("context") for run in row.get("runs") or [] if run.get("agent") == "ChatAgent" and run.get("context")), None)
    initial = report.get("initial_state") or {}
    if seed is not None:
        facts = ({_plain(f["fact_key"]): _plain(f["value"]) for f in seed["facts"]}, "specialized_seed")
    elif initial.get("facts"):
        facts = ({f["fact_key"]: f["value"] for f in initial["facts"] if f.get("status") == "active"}, "specialized_initial_state")
    else:
        facts = ({f["fact_key"]: f["value"] for f in (report.get("final_state") or {}).get("facts", [])
                  if f.get("status") == "active"}, "report_end_state_after_this_round")
    for c in initial.get("commitments") or []:
        if c.get("status") == "sent":
            missing.append(f"种子里有一条已送达的提醒（{c.get('content')}），它不在 Chat 的对话历史里")
    plan_row = next((r for r in plan["rounds"] if r["n"] == n), {})
    media = [{"kind": "image", "ai_generated": a.get("ai_generated")} for a in ((row.get("images") or {}).get("assets") or [])
             if a.get("direction") == "outbound"]
    if time is None:
        missing.append("本轮场景时间未记录")
    if pref_source == "unknown" and ((ctx or {}).get("sections") or {}).get("preferences"):
        pref_source = "chat_context_had_preferences_section"
        missing.append(f"Chat 上下文含偏好节（{ctx['sections']['preferences']} 字），但报告未记录具体内容")
    elif pref_source == "unknown":
        missing.append("用户交流偏好未知（报告未记录）")
    if facts[1] == "report_end_state_after_this_round":
        missing.append("用户档案只有报告结束时的状态（可能含本轮之后才写入的信息）")
    rel_runs = []
    for other in related:
        sources[other] = sha256_file(root / other)
        o = _row(_load(other, root), n)
        rel_runs.append({"path": other, "reply": o.get("violet"), "note": "同一轮的其他真实运行；上下文可能不同，不作为本场景的候选回复"})
    return {
        "id": sid, "kind": "real", "source": {"path": rel, "round": n, "pointer": f"/rounds/{report['rounds'].index(row)}"},
        "tags": tags, "user": row["user"], "rubric": row.get("rubric") or plan_row.get("rubric"),
        "scenario_time": time, "scenario_time_source": time_source, "time_advance": row.get("time_advance"),
        "relationship_stage": "familiar" if n > 5 else "new", "relationship_stage_source": "inferred_from_round_number",
        "history": history, "known_facts": facts[0], "known_facts_source": facts[1],
        "preferences": prefs, "preferences_source": pref_source,
        "chat_context_sections": (ctx or {}).get("sections"), "violet_life": None,
        "reference_reply": row.get("violet"), "reference_media": media, "related_runs": rel_runs,
        "missing": missing + ["Violet 的生活记录与情绪状态未单独记录（可参考 chat_context_sections 中 violet_life 的长度）"],
        "source_sha256": sources,
    }


def build_set(root: Path = ROOT) -> dict:
    plan = _load(PLAN, root)
    probes = _load(PROBES_PATH, root)
    scenarios = [_real_scenario(*item, root=root, plan=plan) for item in REAL_SELECTION]
    for p in probes["probes"]:
        if "reference_reply" in p:
            raise ValueError(f"synthetic probe {p['id']} must not carry a reference reply")
        scenarios.append({**p, "kind": "synthetic", "source": {"path": PROBES_PATH, "id": p["id"]},
                          "reference_reply": None, "source_sha256": {PROBES_PATH: sha256_file(root / PROBES_PATH)}})
    ids = [s["id"] for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate scenario id")
    return {"version": VERSION, "notice": "真实场景只摘录报告原文并保留来源哈希；合成探针为人工编写，不是历史对话。未知信息保持为空并列入 missing。",
            "plan": {"path": PLAN, "sha256": sha256_file(root / PLAN)}, "dimensions": DIMENSIONS, "scenarios": scenarios}


def dump_set(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=False) + "\n"


def load_set(path=None, root: Path = ROOT) -> tuple[dict, str]:
    path = Path(path) if path else root / SET_PATH
    raw = path.read_bytes()
    return upgrade(json.loads(raw.decode("utf-8"))), hashlib.sha256(raw).hexdigest()


def verify(scenario_set: dict, root: Path = ROOT) -> list[str]:
    problems = []
    if sha256_file(root / scenario_set["plan"]["path"]) != scenario_set["plan"]["sha256"]:
        problems.append("plan sha256 differs")
    for s in scenario_set["scenarios"]:
        for rel, sha in s["source_sha256"].items():
            p = root / rel
            if not p.is_file():
                problems.append(f"{s['id']}: missing {rel}")
            elif sha256_file(p) != sha:
                problems.append(f"{s['id']}: sha256 differs {rel}")
    return problems


# ---------------------------------------------------------------------------------------------- variants

MEDIA_LINE = "[发送了一张 AI 生成的图片]"


def baseline_variant(scenario_set: dict) -> dict:
    replies = {}
    for s in scenario_set["scenarios"]:
        if s["kind"] == "real" and (s["reference_reply"] or s["reference_media"]):
            replies[s["id"]] = [(s["reference_reply"] or []) + [MEDIA_LINE for _ in s["reference_media"]]]
    return {"variant": BASELINE, "environment": "real_model", "notes": "历史真实回复，摘自各场景的源报告", "replies": replies}


def load_variant(path, scenario_set: dict, set_sha: str) -> dict:
    v = upgrade(json.loads(Path(path).read_text(encoding="utf-8")))
    if v.get("variant") in (None, "", BASELINE):
        raise ValueError("variant name missing or reserved")
    if v.get("set_sha256") != set_sha:
        raise ValueError("variant was produced for a different scenario set")
    if v.get("environment") not in ENVIRONMENTS:
        raise ValueError(f"environment must be one of {sorted(ENVIRONMENTS)}")
    ids = {s["id"] for s in scenario_set["scenarios"]}
    for sid, samples in (v.get("replies") or {}).items():
        if sid not in ids:
            raise ValueError(f"unknown scenario {sid}")
        if not isinstance(samples, list) or not samples or not all(isinstance(x, list) and all(isinstance(t, str) for t in x) for x in samples):
            raise ValueError(f"{sid}: replies must be a non-empty list of samples, each a list of message lines")
    return v


def render_reply(lines: list[str]) -> str:
    return "\n".join(lines) or "[没有回复]"


# ---------------------------------------------------------------------------------------------- blind sheets

def _context_md(s: dict, max_turns: int = 10) -> str:
    lines = [f"- 来源：{'真实测试' if s['kind'] == 'real' else '合成探针（人工编写，非真实对话）'}",
             f"- 场景时间：{s.get('scenario_time') or '未知'}"]
    if s.get("last_chat_gap_hours") is not None:
        lines.append(f"- 距上次聊天：约 {s['last_chat_gap_hours']} 小时")
    if s.get("time_advance"):
        lines.append(f"- 本轮前时间推进：{s['time_advance'].get('hours')} 小时")
    lines.append(f"- 关系阶段：{'刚认识' if s.get('relationship_stage') == 'new' else '已经熟悉'}")
    if s.get("known_facts"):
        lines.append("- 已知用户信息：" + "；".join(f"{k}={v}" for k, v in s["known_facts"].items()))
    if s.get("preferences"):
        lines.append("- 用户偏好：" + "；".join(f"{p['key']}={p.get('value') or '值未记录'}" for p in s["preferences"]))
    if s.get("violet_life") is not None:
        lines.append("- Violet 的生活记录：" + ("；".join(e["content"] for e in s["violet_life"]) or "无"))
    if s.get("memories"):
        lines.append("- 相关记忆：" + "；".join(m["content"] for m in s["memories"]))
    if s.get("rater_notes"):
        lines.append("- 评分提示：可接受 " + "、".join(s["rater_notes"]["acceptable"]) + "；需警惕 " + "、".join(s["rater_notes"]["red_flags"]))
    elif s.get("rubric"):
        lines.append(f"- 原剧本要求：{s['rubric']}")
    for m in s.get("missing") or []:
        lines.append(f"- 缺失：{m}")
    lines.append("\n对话（较早 → 最近）：")
    history = s.get("history") or []
    if len(history) > max_turns:
        lines.append(f"…（省略更早的 {len(history) - max_turns} 条）")
    for t in history[-max_turns:]:
        who = "用户" if t["role"] == "user" else "Violet"
        text = t["text"] if t["text"] is not None else "（回复未知）"
        lines.append(f"> {who}：{text}".replace("\n", " / "))
    lines.append(f"\n**用户本轮：{s['user']}**")
    return "\n".join(lines)


def blind(scenario_set: dict, set_sha: str, variants: list[dict], seed: int, scenario_ids=None,
          all_samples: bool = False) -> tuple[list[dict], str, dict]:
    """One item per scenario (sample 0), or with all_samples one item per scenario and sample index, each item
    comparing the variants' same-index samples. Without all_samples the output is unchanged from p81."""
    if len({v["variant"] for v in variants}) != len(variants) or len(variants) < 1:
        raise ValueError("need distinct variants")
    rng = random.Random(seed)
    by_id = {s["id"]: s for s in scenario_set["scenarios"]}
    chosen = [sid for sid in by_id if scenario_ids is None or sid in scenario_ids]
    if not all_samples:
        rng.shuffle(chosen)
        units = [(sid, 0, sid) for sid in chosen]
    else:
        units = []
        for sid in chosen:
            n = max((len(v["replies"].get(sid) or []) for v in variants), default=0)
            units += [(sid, k, f"{sid}#s{k}") for k in range(max(n, 1))]
        rng.shuffle(units)
    header = ["# Violet 聊天盲评表\n", "按维度打 0/1/2（部分维度可填 na），每个候选都写评分理由；grounded<2 时在 grounded_evidence 引用原句。"
              "rank 为同一题内的偏好排序（1 最好）。候选顺序随机，看不到来源。关键词、称呼、问句和长度不是评分标准，请按整体聊天感受判断。\n",
              *[f"- {d}：{text}" for d, text in DIMENSIONS.items()], ""]
    rows, md, items, skipped = [], header, {}, {}
    for sid, k, unit in units:
        have = [v for v in variants if len(v["replies"].get(sid) or []) > k]
        if len(have) != len(variants):
            skipped[unit] = sorted({v["variant"] for v in variants} - {v["variant"] for v in have})
            continue
        item = f"I{len(items) + 1:03d}"
        cands = [(v["variant"], render_reply(v["replies"][sid][k])) for v in have]
        rng.shuffle(cands)
        items[item] = {"scenario": sid, "candidates": {}}
        md.append(f"\n## {item}\n\n{_context_md(by_id[sid])}\n")
        for letter, (variant, text) in zip("ABCDEFG", cands):
            sha = sha256_text(text)
            items[item]["candidates"][letter] = {"variant": variant, "sample": k, "sha256": sha}
            md.append(f"**候选 {letter}**\n\n" + "\n".join("> " + line for line in text.split("\n")) + "\n")
            rows.append({"item": item, "scenario": sid, "candidate": letter, "candidate_sha12": sha[:12],
                         **{d: "" for d in DIMENSIONS}, "rank": "", "grounded_evidence": "", "reason": "", "rater": ""})
    key = {"version": VERSION, "set_sha256": set_sha, "seed": seed, "items": items, "skipped": skipped,
           "variants": {v["variant"]: {k: v.get(k) for k in ("environment", "model", "prompt_sha256", "generated_at", "notes")}
                        | ({"context_style": v["context_style"]} if v.get("context_style") else {})
                        for v in variants}}
    if all_samples:
        key["all_samples"] = True
    return rows, "\n".join(md), key


def write_blind(out: Path, rows, md, key) -> None:
    out.mkdir(parents=True, exist_ok=False)
    with (out / "sheet.csv").open("x", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_FIELDS)
        w.writeheader()
        w.writerows(rows)
    _write_new(out / "sheet.md", md)
    _write_new(out / "key.json", json.dumps(key, ensure_ascii=False, indent=1))


def _write_new(path, text: str) -> None:
    # LF on every platform: the set's sha256 must not change when Git normalises line endings.
    with Path(path).open("x", encoding="utf-8", newline="\n") as f:
        f.write(text)


# ---------------------------------------------------------------------------------------------- scoring

def _value(row: dict, dim: str):
    v = (row.get(dim) or "").strip().lower()
    if v == "na" and dim in NA_ALLOWED:
        return None
    if v not in ("0", "1", "2"):
        raise ValueError(f"{row['item']}{row['candidate']} {dim}: expected 0/1/2" + ("/na" if dim in NA_ALLOWED else ""))
    return int(v)


def score(rows: list[dict], key: dict, scenario_set: dict, set_sha: str) -> dict:
    if key.get("set_sha256") != set_sha:
        raise ValueError("key belongs to a different scenario set")
    tags = {s["id"]: s["tags"] for s in scenario_set["scenarios"]}
    kinds = {s["id"]: s["kind"] for s in scenario_set["scenarios"]}
    per = defaultdict(lambda: defaultdict(list))
    by_tag = defaultdict(lambda: defaultdict(list))
    grounded_issues, reasons, ranks = [], [], defaultdict(dict)
    seen = set()
    for row in rows:
        item, letter, rater = row.get("item"), row.get("candidate"), (row.get("rater") or "").strip() or "unnamed"
        cand = (key["items"].get(item) or {}).get("candidates", {}).get(letter)
        if cand is None or key["items"][item]["scenario"] != row.get("scenario"):
            raise ValueError(f"{item}{letter}: not in key")
        if row.get("candidate_sha12") != cand["sha256"][:12]:
            raise ValueError(f"{item}{letter}: candidate text changed after blinding")
        if (item, letter, rater) in seen:
            raise ValueError(f"{item}{letter}: duplicate row for rater {rater}")
        seen.add((item, letter, rater))
        if not (row.get("reason") or "").strip():
            raise ValueError(f"{item}{letter}: reason is required")
        values = {d: _value(row, d) for d in DIMENSIONS}
        if values["grounded"] < 2 and not (row.get("grounded_evidence") or "").strip():
            raise ValueError(f"{item}{letter}: grounded<2 needs the quoted evidence")
        variant, sid = cand["variant"], key["items"][item]["scenario"]
        for d, v in values.items():
            if v is not None:
                per[variant][d].append(v)
                for t in tags[sid]:
                    by_tag[(variant, t)][d].append(v)
        if values["grounded"] < 2:
            grounded_issues.append({"item": item, "scenario": sid, "variant": variant, "score": values["grounded"],
                                    "evidence": row["grounded_evidence"].strip(), "rater": rater})
        reasons.append({"item": item, "scenario": sid, "kind": kinds[sid], "variant": variant, "rater": rater,
                        "scores": values, "reason": row["reason"].strip(), "evidence": (row.get("grounded_evidence") or "").strip()})
        if (row.get("rank") or "").strip():
            ranks[(item, rater)][variant] = int(row["rank"])
    pairwise = defaultdict(lambda: {"wins": 0, "losses": 0})
    for (item, rater), r in ranks.items():
        n = len(key["items"][item]["candidates"])
        if len(r) != n or sorted(r.values()) != list(range(1, n + 1)):
            raise ValueError(f"{item}: rank every candidate once with 1..{n}")
        names = sorted(r)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                pairwise[f"{a} vs {b}"]["wins" if r[a] < r[b] else "losses"] += 1
    summarize = lambda d: {k: {"mean": round(statistics.mean(v), 3), "n": len(v)} for k, v in d.items()}
    return {"version": VERSION, "set_sha256": set_sha, "variants": key["variants"],
            "by_variant": {v: summarize(d) for v, d in per.items()},
            "by_tag": {f"{v}|{t}": summarize(d) for (v, t), d in sorted(by_tag.items())},
            "pairwise_preference": dict(pairwise), "grounded_issues": grounded_issues, "rated_rows": reasons,
            "notice": "评分来自人工盲评；非 real_model 变体的结果须标注模拟。诊断指标见 diagnose，不参与本结果。"}


# ---------------------------------------------------------------------------------------------- diagnostics

def _ngrams(text: str, n: int = 4):
    text = re.sub(r"\s+", "", text)
    return {text[i:i + n] for i in range(len(text) - n + 1)}


def diagnose(scenario_set: dict, variants: list[dict]) -> dict:
    by_id = {s["id"]: s for s in scenario_set["scenarios"]}
    result = {"diagnostic_only": True,
              "notice": "只用于发现回退与模式，不是优化目标；自然度以人工盲评为准。", "variants": {}}
    for v in variants:
        rows, grams = [], Counter()
        for sid, samples in v["replies"].items():
            for lines in samples:
                text = "".join(lines)
                name = (by_id[sid].get("known_facts") or {}).get("address_name") or (by_id[sid].get("known_facts") or {}).get("称呼")
                rows.append({"scenario": sid, "chars": len(text), "messages": len(lines),
                             "user_chars": len(by_id[sid]["user"]),
                             "uses_name": bool(name and name in text),
                             "ends_with_question": bool(lines and re.search(r"[？?]\s*$", lines[-1])),
                             "you_again": "又" in text})
                grams.update(_ngrams(text))
        n = len(rows) or 1
        result["variants"][v["variant"]] = {
            "replies": len(rows),
            "median_chars": statistics.median([r["chars"] for r in rows]) if rows else None,
            "median_ratio_to_user": statistics.median([r["chars"] / max(1, r["user_chars"]) for r in rows]) if rows else None,
            "name_rate": round(sum(r["uses_name"] for r in rows) / n, 3),
            "question_ending_rate": round(sum(r["ends_with_question"] for r in rows) / n, 3),
            "you_again_count": sum(r["you_again"] for r in rows),
            "repeated_4grams": [g for g, c in grams.most_common(15) if c >= 3],
            "rows": rows}
    return result


# ---------------------------------------------------------------------------------------------- CLI

def _variants(args, scenario_set, set_sha):
    out = []
    for item in args.variant:
        out.append(baseline_variant(scenario_set) if item == BASELINE else load_variant(item, scenario_set, set_sha))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--out", required=True)
    sub.add_parser("verify").add_argument("--set")
    bl = sub.add_parser("blind")
    bl.add_argument("--variant", action="append", required=True, help=f"'{BASELINE}' or a variant json file")
    bl.add_argument("--seed", type=int, required=True); bl.add_argument("--out", required=True)
    bl.add_argument("--scenarios", help="comma-separated ids"); bl.add_argument("--set")
    bl.add_argument("--all-samples", action="store_true",
                    help="one item per scenario and sample index (default: sample 0 only)")
    sc = sub.add_parser("score")
    sc.add_argument("--sheet", required=True); sc.add_argument("--key", required=True)
    sc.add_argument("--out", required=True); sc.add_argument("--set")
    dg = sub.add_parser("diagnose")
    dg.add_argument("--variant", action="append", required=True); dg.add_argument("--out", required=True); dg.add_argument("--set")
    args = parser.parse_args(argv)

    if args.cmd == "build":
        _write_new(args.out, dump_set(build_set()))
        print("written", args.out)
        return 0
    scenario_set, set_sha = load_set(args.set)
    if args.cmd == "verify":
        problems = verify(scenario_set)
        print("\n".join(problems) if problems else f"ok: {len(scenario_set['scenarios'])} scenarios, set sha256 {set_sha}")
        return 1 if problems else 0
    if verify(scenario_set):
        raise SystemExit("scenario set sources changed; run verify")
    if args.cmd == "blind":
        ids = set(args.scenarios.split(",")) if args.scenarios else None
        rows, md, key = blind(scenario_set, set_sha, _variants(args, scenario_set, set_sha), args.seed, ids,
                              all_samples=args.all_samples)
        write_blind(Path(args.out), rows, md, key)
        print(f"{len(key['items'])} items, skipped {len(key['skipped'])}; give only sheet.csv/sheet.md to the rater")
        return 0
    if args.cmd == "score":
        with open(args.sheet, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        key = json.loads(Path(args.key).read_text(encoding="utf-8"))
        result = score(rows, key, scenario_set, set_sha)
        result["inputs"] = {"sheet_sha256": sha256_file(args.sheet), "key_sha256": sha256_file(args.key)}
        _write_new(args.out, json.dumps(result, ensure_ascii=False, indent=1))
        print("written", args.out)
        return 0
    result = diagnose(scenario_set, _variants(args, scenario_set, set_sha))
    _write_new(args.out, json.dumps(result, ensure_ascii=False, indent=1))
    print("written", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
