"""Phase 8.3 ChatAgent A/B replay: one frozen scenario, chat prompt + persona v0 vs v1, everything else equal.

plan  offline. Rebuild each scenario's state in a fresh child process, render the ChatAgent context and
      compare it with what the historical ChatAgent saw (section sizes + history length). Classify:
      complete (reconstruction matches the record), partial (it does not, or nothing was recorded),
      unfair (earlier Violet replies are unknown), synthetic (hand-written probe; state is the probe itself).
run   one child process and one NEW test database per scenario x version x sample. ChatAgent is called
      through the existing harness (VioletTestSession, BudgetedClient, consistency guard); output is a
      chat_eval variant file per version. Mock by default; real calls need RealApiGate, confirmation,
      a budget and the network allow-list. Only complete and synthetic scenarios are accepted.
      --arms v1:legacy,v1:natural (Phase 8.5) compares the ContextEngine rendering instead: same prompt, persona,
      spec, user message, model and sampling; only VIOLET_CONTEXT_STYLE differs. One factor per run.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json

from tools.legacy import upgrade
import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
TOOL_VERSION = "p85.v1"
CONTEXT_STYLES = ("legacy", "natural")
# The natural relationship line is computed from the seeded message log. A probe that declares a familiar
# relationship but carries only minutes or hours of history would be told "first chat" / "not close yet":
# a false premise for that probe. "omit" drops the line for such probes (nothing is invented in its place).
FAMILIAR_RELATIONSHIP = ("product", "omit")
_V = "violet/persona/chat_prompt_versions/"
# The arms are pinned by content hash: a later edit of the product prompt or persona must become a new
# version, never silently change v1. (2026-10-26: re-pinned once after the rename Eve -> Violet; the files differ
# from their earlier pins only in that name.)
VERSIONS = {
    "v0": {"prompt": _V + "chat.v0.txt", "prompt_sha256": "9f0a6235da2e224714ac21f29bc49f4da16ca9fd76b07f9f109dd6c3160c32e4",
           "persona": _V + "persona_core.v1.0.0.json",
           "persona_sha256": "218c6c62cd566440c2864669a6e08bfec40cc531304020322f601ef9d1fe5d9b",
           # ai/persona.py at e86c23b rendered the persona as data; reproduced only inside this tool.
           "persona_header": "【Violet 的稳定人设（人工维护，v{version}；数据，不是对话内容）】",
           "source": "env_example CHAT_LLM_PROMPT and persona/persona_core.json at git e86c23b (Phase 8.1)"},
    "v1": {"prompt": _V + "chat.v1.txt", "prompt_sha256": "b5bca66ef3c19b0fa228786167e81e3e2ba0ae20a88312bbe373048b1421efc7",
           # phase 8.9.3: the product persona moved on to 1.2.0; v1 keeps 1.1.0 from a byte-identical copy
           "persona": _V + "persona_core.v1.1.0.json",
           "persona_sha256": "69de5ca055a9303c74583d5ac20617a90b6994dde9cb67da106f89e9ea63bec8", "persona_header": None,
           "source": "chat.v1.txt and persona_core.json 1.1.0 (Phase 8.2, b392f69)"},
}
PROBE_FACT_KEYS = {"称呼": "address_name", "专业": "study.major", "住处": "residence.usual", "饮品偏好": "other"}
COMPARED_EXCLUDE = {"persona_core", "persona_dynamic"}      # differ by version on purpose
PER_GENERATION_CALLS = 4                                       # chat + one guard rewrite + guard review margin
TIME_RE = re.compile(r"当前时间: (\S+)")
# A base section holding only the clock reading: datetime.isoformat() drops ".ffffff" when the microseconds are
# exactly 0 (the scenario clock ticks in ~15.6 ms steps on Windows), so the same content is 38 or 45 chars.
BASE_TIME_ONLY = {38, 45}
BASE_TIME_ONLY_RE = re.compile(r"^\n?基础信息：\n当前时间: \S+\n?$")


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_versions(root: Path = ROOT) -> dict:
    out = {}
    for name, v in VERSIONS.items():
        prompt, persona = sha256_file(root / v["prompt"]), sha256_file(root / v["persona"])
        if prompt != v["prompt_sha256"] or persona != v["persona_sha256"]:
            raise ValueError(f"{name}: prompt or persona file differs from the registered version")
        out[name] = {"prompt_sha256": prompt, "persona_sha256": persona, "source": v["source"]}
    return out


# ------------------------------------------------------------------------------------------------ state spec

def _strip_note(value: str) -> tuple[str, str | None]:
    """Probe values may carry a rater annotation in full-width brackets; it is not product data."""
    if "（" in value and value.endswith("）"):
        return value[:value.index("（")], value[value.index("（"):]
    return value, None


def _history_rows(turns: list[dict], scenario_time: str, minutes=None) -> list[dict]:
    """One row per sent message: Violet's multi-line reply was stored line by line, like the product does."""
    end = datetime.fromisoformat(scenario_time)
    split = []
    for i, t in enumerate(turns):
        lines = [t["text"]] if t["role"] == "user" else [x for x in t["text"].split("\n") if x.strip()]
        split += [(i, t, line) for line in lines]
    rows = []
    for j, (i, t, line) in enumerate(split):
        ago = timedelta(minutes=minutes[i], seconds=len(split) - j) if minutes else timedelta(minutes=len(split) - j)
        rows.append({"role": "user" if t["role"] == "user" else "assistant", "text": line, "round": t.get("round"),
                     "created_at": (end - ago).isoformat()})
    return rows


def _report_row(scenario: dict, root: Path):
    report = upgrade(json.loads((root / scenario["source"]["path"]).read_text(encoding="utf-8")))
    return report, report["rounds"][int(scenario["source"]["pointer"].split("/")[-1])]


def real_spec(scenario: dict, root: Path = ROOT) -> tuple[dict | None, dict]:
    """(spec, verdict). spec None when the scenario cannot be rebuilt without guessing."""
    if any(t["text"] is None for t in scenario["history"]):
        return None, {"class": "unfair", "reasons": ["earlier Violet replies are unknown (lost test directory)"]}
    report, row = _report_row(scenario, root)
    n, reasons, notes = row["n"], [], []
    facts = []
    if scenario["known_facts_source"] in ("specialized_seed", "specialized_initial_state"):
        facts = [{"key": k, "value": v} for k, v in scenario["known_facts"].items()]
    else:
        reasons.append("profile before this round is unknown (only the report-end state is recorded); not seeded")
    prefs = [p for p in scenario["preferences"] if p.get("value")]
    # Earlier rounds of the same run are replayed in order without a model: their ChatAgent context is built
    # (as the product did, e.g. an onboarding offer gets recorded) and only writes with recorded evidence apply.
    earlier = []
    for r in report.get("rounds", []):
        if (r.get("n") or 0) >= n or r.get("violet") is None:
            continue
        writes = []
        for fact in (r.get("correction_evidence") or {}).get("active") or []:
            writes.append({"type": "correct", "key": fact["fact_key"], "value": fact["value"], "evidence": fact["evidence"],
                           "old": (r["correction_evidence"].get("old") or {}).get("value"), "user": r["user"]})
            notes.append(f"round {r['n']}: profile correction replayed from correction_evidence")
        if report.get("preference_probe") and r["n"] == 27:
            probe = report["preference_probe"]
            writes.append({"type": "preference", "key": probe["key"], "value": probe["value"],
                           "evidence": "dashboard probe after round 27"})
            notes.append("round 27: specialized runner's preference_probe replayed")
        earlier.append({"round": r["n"], "user": r["user"], "writes": writes})
    commitments = []
    for c in (report.get("initial_state") or {}).get("commitments") or []:
        commitments.append({"content": c["content"], "due_at": c["due_at"], "status": c["status"]})
    if scenario["source"]["path"].startswith("local/b113/"):
        seed = upgrade(json.loads((root / "local/b113/real-20261019-014631/seed.json").read_text(encoding="utf-8")))
        c = seed["state"]["commitment"]
        sent = any(r.get("status_after") == "sent" for r in (row.get("reminder") or {}).get("reminders") or [])
        commitments.append({"content": c["content"]["value"], "due_at": c["due_at"]["value"],
                            "status": "sent" if sent else "pending"})
    recalled = [{"content": m["content"], "progress": m.get("progress"), "kind": m.get("kind") or "event"}
                for m in (row.get("recall_evidence") or {}).get("returned") or []]
    expected = scenario.get("chat_context_sections")
    history_expected = next((run["context"].get("history_messages") for run in row.get("runs") or []
                             if run.get("agent") == "ChatAgent" and run.get("context")), None)
    if not scenario.get("scenario_time"):
        return None, {"class": "partial", "reasons": ["scenario time unknown"]}
    spec = {"scenario_id": scenario["id"], "kind": "real", "user": scenario["user"],
            "scenario_time": scenario["scenario_time"],
            "history": _history_rows(scenario["history"], scenario["scenario_time"]),
            "facts": facts, "preferences": prefs, "commitments": commitments, "memories": recalled, "life": [],
            "earlier_rounds": earlier, "notes": notes}
    return spec, {"class": None, "reasons": reasons, "expected_sections": expected,
                  "expected_history_messages": history_expected}


def probe_spec(scenario: dict) -> tuple[dict, dict]:
    facts, notes = [], []
    for label, value in scenario.get("known_facts", {}).items():
        key = PROBE_FACT_KEYS.get(label)
        if key is None:
            raise ValueError(f"{scenario['id']}: no profile key for probe label {label}")
        clean, note = _strip_note(value)
        if note:
            notes.append(f"{label}: rater annotation {note} not seeded")
        facts.append({"key": key, "value": clean})
    minutes = [t.get("minutes_ago", 1) for t in scenario["history"]]
    spec = {"scenario_id": scenario["id"], "kind": "synthetic", "user": scenario["user"],
            "scenario_time": scenario["scenario_time"],
            "history": _history_rows(scenario["history"], scenario["scenario_time"], minutes),
            "facts": facts, "preferences": [p for p in scenario.get("preferences", [])],
            "commitments": [], "memories": scenario.get("memories", []), "life": scenario.get("violet_life", []),
            "earlier_rounds": [], "relationship_stage": scenario.get("relationship_stage"),
            "notes": notes + ["relationship_stage and last_chat_gap_hours have no product field; the gap is carried "
                              "by message timestamps only (identical for both arms)"]}
    return spec, {"class": "synthetic", "reasons": []}


# ------------------------------------------------------------------------------------------------ child

def _seed(db, spec: dict) -> dict:
    from ai.profile_store import Profile
    from ai.life_store import LifeStore
    profile = Profile(db)
    for f in spec["facts"]:
        profile.facts.remember(f["key"], f["value"], source="dashboard", actor="ab_seed")
    for p in spec["preferences"]:
        profile.preferences.set_explicit(p["key"], p["value"], evidence=p.get("evidence"), source="dashboard",
                                         actor="ab_seed")
    for c in spec["commitments"]:
        cid = db.add_commitment(c["content"], c["due_at"], source_turn_id="ab:seed")["commitment"]["id"]
        if c["status"] != "pending":
            db.update_commitment(cid, status=c["status"])
    recalled = []
    for m in spec["memories"]:
        mid = db.add_event_memory(content=m["content"], category="event", kind=m.get("kind") or "event",
                                  progress=m.get("progress") or "unknown", source_turn_id="ab:seed")
        row = db.fetch_one("SELECT * FROM event_memories WHERE id = ?", (mid,))
        recalled.append({k: row.get(k) for k in ("id", "content", "category", "kind", "progress", "event_time", "status")})
    store, thread = LifeStore(db), None
    for i, item in enumerate(spec["life"]):
        when = f"{item['date']}T12:00:00+08:00"
        if item["status"] == "happened":
            store.insert_event(uid=f"ab-{i}", thread_id=None, plan_id=None, event_type="progress", content=item["content"],
                               occurred_at=when, duration_minutes=60, source="ab_seed")
        elif item["status"] == "active_thread":
            thread = store.create_thread(f"ab-thread-{i}", "reading", item["content"], None, when, source="ab_seed")
        elif item["status"] == "plan" and thread is not None:
            store.create_plan(thread, item["content"], when, source="ab_seed")
        else:
            raise ValueError(f"cannot seed life item {item}")
    history = list(spec["history"])

    def insert_until(round_no):
        while history and (round_no is None or (history[0].get("round") or 0) < round_no):
            h = history.pop(0)
            db.add_message(h["role"], "incoming" if h["role"] == "user" else "outgoing", "text", h["text"],
                           created_at=h["created_at"])
    from ai.context_engine import ContextEngine
    for step in spec["earlier_rounds"]:
        insert_until(step["round"])
        ContextEngine(db).build("chat", step["user"], {"route": "chat", "turn_id": f"ab:replay:{step['round']}"})
        for w in step["writes"]:
            if w["type"] == "correct":
                result = profile.facts.remember(w["key"], w["value"], evidence=w["evidence"], user_message=w["user"],
                                                source="user_chat", correction=True, replaces_value=w["old"])
                if result.get("action") in ("rejected", "not_stored"):
                    raise ValueError(f"replayed correction refused: {result}")
            elif w["type"] == "preference":
                profile.preferences.set_explicit(w["key"], w["value"], evidence=w["evidence"], source="dashboard",
                                                 actor="ab_seed")
        insert_until(step["round"] + 1)
    insert_until(None)
    return {"recalled": recalled}


def _persona_dir(workdir: Path, version: str) -> Path:
    target = workdir / "persona"
    shutil.copytree(PROJECT / "persona", target)
    shutil.copyfile(ROOT / VERSIONS[version]["persona"], target / "persona_core.json")
    return target


class _Snapshot(dict):
    """Keep each request exactly as it was sent."""

    def __setitem__(self, key, value):
        super().__setitem__(key, copy.deepcopy(value))


def child_main(args) -> int:
    from tools.harness import (Budget, CallRecorder, VioletTestSession, NetworkGuard, RealApiGate, ScenarioClock,
                               call_detail, prepare_environment)
    spec_bytes = Path(args.spec).read_bytes()
    spec = json.loads(spec_bytes.decode("utf-8"))
    workdir = Path(args.workdir)
    token = os.environ.get("VIOLET_AB_CONFIRMATION", "")          # read before prepare_environment clears VIOLET_*
    prepare_environment(workdir, Path(args.config) if args.config else None)
    os.environ["VIOLET_PERSONA_DIR"] = str(_persona_dir(workdir, args.version))
    os.environ["CHAT_LLM_PROMPT"] = (ROOT / VERSIONS[args.version]["prompt"]).read_text(encoding="utf-8")
    os.environ["VIOLET_CONTEXT_STYLE"] = args.context_style        # after prepare_environment, which clears VIOLET_*
    # Phase 9.1: the recorded real scenarios predate the "not found is not never said" line; a rebuild must match its
    # recording, so A/B children never render it (the A/B compares prompts and persona, not this context change).
    os.environ["VIOLET_MEMORY_NOT_FOUND_NOTE"] = "0"
    budget = Budget(max_calls_per_turn=PER_GENERATION_CALLS, max_calls_total=args.max_calls, max_tokens_total=args.max_tokens)
    key = None
    if args.real:
        confirmed = Path(args.confirmation).read_text(encoding="utf-8").strip() if args.confirmation else ""
        if not token or token != confirmed:
            raise PermissionError("real child refused: no parent confirmation")
        models = {"chat": os.environ.get("CHAT_LLM_MODEL", "?")}
        key = RealApiGate().open(True, args.api_url, budget, models, assume_yes=True, out=lambda line: None)
    recorder = CallRecorder(budget, [key] if key else [])
    recorder.last_request = _Snapshot()          # the agent appends its reply to the same list after the call
    guard = NetworkGuard([args.api_url] if key else []).install()
    session = None
    result = {"scenario_id": spec["scenario_id"], "version": args.version, "mode": args.mode,
              "context_style": args.context_style, "spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
              "relationship_line": "omitted" if args.omit_relationship else "product",
              "memory_not_found_note": "off (phase 9.1 line, absent from the recordings)", "errors": []}
    try:
        if args.omit_relationship:
            from ai import context_engine
            context_engine.ContextEngine._relationship_section = (
                lambda self: context_engine._Section("relationship", 3, "", []))     # empty section is not rendered
        from ai import persona
        header = VERSIONS[args.version]["persona_header"]
        if header:
            render = persona.PersonaCore.render

            def old_render(core, _render=render):
                lines = _render(core).split("\n")
                return "\n".join([header.format(version=core.version), *lines[1:]])
            persona.PersonaCore.render = old_render
        session = VioletTestSession(workdir, recorder, real=key is not None, owns_db=True,
                                 clock=ScenarioClock(datetime.fromisoformat(spec["scenario_time"])))
        seeded = _seed(session.db, spec)
        from ai.chat import cooling_topics_for_turn, guard_reply
        from ai.chat_output import normalize_chat_output
        from ai.context_engine import ContextEngine
        context = {"recalled_memories": seeded["recalled"], "restricted_topics": cooling_topics_for_turn(seeded["recalled"]),
                   "route": "chat", "turn_id": f"ab:{spec['scenario_id']}"}
        if args.mode == "render":
            # Only here: building the context may record an onboarding offer, so generate never pre-builds.
            packet = ContextEngine(session.db).build("chat", spec["user"], context)
            base = next((s for s in packet.sections if "基础信息：" in s), "")
            result["context"] = {**packet.report, "base_time_only": bool(BASE_TIME_ONLY_RE.match(base)),
                                 "sections_old_name": _old_name_lengths(packet)}
            result["rendered"] = {"sections": packet.sections, "history": packet.history}
        else:
            recorder.start_round(1)
            chat = session.agents["chat"]
            reply = chat.run(spec["user"], call_context=context)
            request = recorder.last_request.get("ChatAgent", {}).get("messages") or []
            guarded, violations = guard_reply(reply.get("content", ""), regenerate=lambda note: chat.run(
                spec["user"], call_context={**context, "consistency_note": note}).get("content", ""))
            out = normalize_chat_output(guarded)
            # Fingerprint of everything but the version's own prompt and persona; the scenario clock runs on with
            # real time, so its reading is masked here and kept separately (time_skew in the run summary).
            shared = [{**m, "content": TIME_RE.sub("当前时间: <scenario clock>", m.get("content") or "")}
                      for i, m in enumerate(request) if i > 0 and "稳定人设" not in (m.get("content") or "")]
            now = [TIME_RE.search(m.get("content") or "") for m in request]
            result["scenario_now"] = next((x.group(1) for x in now if x), None)
            result["context"] = next((r.get("context") for r in recorder.runs if r.get("agent") == "ChatAgent"), None)
            result["user_message_sha256"] = hashlib.sha256(
                json.dumps(request[-1] if request else None, ensure_ascii=False).encode()).hexdigest()
            result["request_messages"] = json.loads(recorder.scrub(json.dumps(request, ensure_ascii=False)))
            result.update(
                raw=recorder.scrub(reply.get("content", "")), lines=[recorder.scrub(m["content"]) for m in out["messages"]
                                                                     if m["type"] == "text"],
                consistency_violations=[v.check for v in violations], failed=bool(reply.get("failed") or reply.get("error")),
                context_fingerprint=hashlib.sha256(json.dumps(shared, ensure_ascii=False).encode()).hexdigest(),
                system_prompt_sha256=hashlib.sha256((request[0].get("content") or "").encode()).hexdigest() if request else None)
    except Exception as exc:                                   # recorded, scrubbed, never hidden
        result["errors"].append(recorder.scrub(f"{exc.__class__.__name__}: {exc}"))
    finally:
        if session is not None:
            result["close_errors"] = session.close()
        guard.uninstall()
        result.update(calls=[call_detail(c) for c in recorder.calls], budget=recorder.budget_state(),
                      network_guard=guard.summary(), model=os.environ.get("CHAT_LLM_MODEL"),
                      temperature=os.environ.get("CHAT_LLM_MODEL_TEMPERATURE"),
                      prompt_sha256=VERSIONS[args.version]["prompt_sha256"],
                      persona_sha256=sha256_file(ROOT / VERSIONS[args.version]["persona"]))
        text = recorder.scrub(json.dumps(result, ensure_ascii=False, indent=1))
        with Path(args.result).open("x", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return 0 if not result["errors"] else 1


# ------------------------------------------------------------------------------------------------ parent

def _launch(argv: list[str], env: dict, log: Path) -> int:
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-m", "tools.chat_ab", "_child", *argv], cwd=PROJECT,
                          env=env, capture_output=True, text=True, encoding="utf-8", timeout=900)
    with log.open("x", encoding="utf-8", newline="\n") as f:
        f.write(done.stdout + done.stderr)
    return done.returncode


def _result(path: Path) -> dict:
    """A child that died before writing its result is an error row, not a silent gap."""
    if path.is_file():
        return upgrade(json.loads(path.read_text(encoding="utf-8")))
    return {"errors": ["child wrote no result"], "calls": [], "budget": {"used_tokens": 0, "refused_calls": []}}


def _child_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("VIOLET_TEST_API_KEY", "VIOLET_AB_CONFIRMATION")}
    env.update(PYTHONIOENCODING="utf-8", **(extra or {}))
    return env


def _specs(scenario_set: dict, ids, root: Path):
    for s in scenario_set["scenarios"]:
        if ids is not None and s["id"] not in ids:
            continue
        yield s, (probe_spec(s) if s["kind"] == "synthetic" else real_spec(s, root))


def _renamed(text):
    """A context recorded before the rename to Violet, in today's wording (only the name differs)."""
    return re.sub(r"(?<![A-Za-z])Eve(?![A-Za-z])", "Violet", text) if isinstance(text, str) else text


def _old_name_lengths(packet) -> dict:
    """Section lengths as they would have been under the old name "Eve": the historical records keep only lengths, and
    "Violet" is three characters longer. Each named length is matched to the section text of that length."""
    texts = list(packet.sections)
    out = {}
    for name, length in (packet.report.get("sections") or {}).items():
        text = next((t for t in texts if len(t) == length), None)
        if text is None:
            out[name] = length
            continue
        texts.remove(text)
        out[name] = len(text.replace("Violet", "Eve"))
    return out


def _compare(verdict: dict, rendered: dict) -> dict:
    expected = verdict.get("expected_sections")
    # records made before the rename are compared with the lengths the old name would have given
    got = {k: v for k, v in (rendered.get("sections_old_name") or rendered["sections"]).items() if k not in COMPARED_EXCLUDE}
    if expected is None:
        return {"class": "partial", "reasons": verdict["reasons"] + ["historical ChatAgent context was not recorded"]}
    want = {k: _renamed(v) for k, v in expected.items() if k not in COMPARED_EXCLUDE}
    diffs = {k: {"historical": want.get(k), "rebuilt": got.get(k)} for k in sorted(set(want) | set(got))
             if want.get(k) != got.get(k)
             and not (k == "base" and rendered.get("base_time_only") and {want.get(k), got.get(k)} <= BASE_TIME_ONLY)}
    if verdict.get("expected_history_messages") != rendered["history_messages"]:
        diffs["history_messages"] = {"historical": verdict.get("expected_history_messages"), "rebuilt": rendered["history_messages"]}
    if diffs:
        return {"class": "partial", "reasons": verdict["reasons"] + ["rebuilt context differs from the recorded one"],
                "differences": diffs}
    return {"class": "complete", "reasons": verdict["reasons"]}


def plan(out: Path, ids=None, launch=_launch, root: Path = ROOT) -> dict:
    from tools import chat_eval
    scenario_set, set_sha = chat_eval.load_set(root=root)
    if chat_eval.verify(scenario_set, root):
        raise ValueError("scenario set sources changed; run chat_eval verify")
    versions = check_versions(root)
    out.mkdir(parents=True, exist_ok=False)
    rows = {}
    for scenario, (spec, verdict) in _specs(scenario_set, ids, root):
        entry = {"kind": scenario["kind"], "tags": scenario["tags"], **verdict}
        if spec is not None:
            work = out / "work" / scenario["id"]
            (out / "specs").mkdir(exist_ok=True)
            spec_path = out / "specs" / f"{scenario['id']}.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
            result_path = out / "work" / f"{scenario['id']}.render.json"
            work.parent.mkdir(exist_ok=True)
            code = launch(["--spec", str(spec_path), "--version", "v1", "--mode", "render", "--workdir", str(work),
                           "--result", str(result_path), "--max-calls", "1", "--max-tokens", "1",
                           "--context-style", "legacy"], _child_env(),
                          result_path.with_suffix(".log"))
            rendered = _result(result_path)
            if code != 0 or rendered["errors"] or rendered["calls"]:
                entry.update({"class": "partial", "reasons": ["state could not be rebuilt offline"], "errors": rendered["errors"]})
            elif scenario["kind"] == "synthetic":
                entry["rebuilt_sections"] = rendered["context"]["sections"]
            else:
                entry.update(_compare(verdict, rendered["context"]))
                entry["rebuilt_sections"] = rendered["context"]["sections"]
            entry["seed_notes"] = spec["notes"]
        entry.pop("expected_sections", None)
        entry.pop("expected_history_messages", None)
        rows[scenario["id"]] = entry
    manifest = {"tool": TOOL_VERSION, "set_sha256": set_sha, "versions": versions, "scenarios": rows,
                "eligible": sorted(k for k, v in rows.items() if v["class"] in ("complete", "synthetic")),
                "notice": "complete = rebuilt ChatAgent context matches the recorded section sizes and history length "
                          "(persona sections excluded: they differ by version). Partial and unfair scenarios are never "
                          "used for A/B. Synthetic probes are hand-written, not history."}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    return manifest


def resolve_arms(versions=("v0", "v1"), context_style: str = "legacy", arms=None) -> list[dict]:
    """The arms of one run. Without arms: prompt versions under one context style (Phase 8.3 names, unchanged).
    With arms ("v1:legacy"): only one factor may differ, so a difference in replies has one cause."""
    pairs = [(v, context_style) for v in versions] if arms is None else [
        tuple(a.split(":", 1)) if isinstance(a, str) else tuple(a) for a in arms]
    for pair in pairs:
        if len(pair) != 2 or pair[0] not in VERSIONS:
            raise ValueError(f"arm {':'.join(pair)}: expected <version>:<context style>, version one of {sorted(VERSIONS)}")
        if pair[1] not in CONTEXT_STYLES:
            raise ValueError("context_style must be legacy (as in Phase 8.3) or natural (Phase 8.4)")
    if not pairs or len(set(pairs)) != len(pairs):
        raise ValueError("arms must be distinct")
    if len({v for v, _ in pairs}) > 1 and len({s for _, s in pairs}) > 1:
        raise ValueError("arms differ in both prompt version and context style; compare one factor at a time")
    by_style = len({s for _, s in pairs}) > 1
    return [{"arm": f"{v}-{s}" if by_style else v, "variant": f"chat_{v}_{s}" if by_style else f"chat_{v}",
             "version": v, "context_style": s} for v, s in pairs]


# Shared by every arm of a context-style comparison; a difference means the comparison is not fair.
STYLE_INVARIANTS = ("system_prompt_sha256", "persona_sha256", "spec_sha256", "user_message_sha256", "model", "temperature")
RATER_FOCUS = """# 评分关注点（Phase 8.5，随盲评表一起交给评分人）

候选来自同一场景、同一用户输入、同一模型，来源对评分人隐藏。按整体聊天感受打分，以下是需要特别留意的地方：

- 认识多久不等于有多亲密：不要因为“认识很久”就默认很亲密，也不要因为“刚认识”就过分客气。
- 撒娇和昵称不是必需的：自然地带一点可以，每句都撒娇、硬加昵称或语气词要扣分。
- 少追问不等于不提问：对方不喜欢被追问时，少问、不连问；真正需要时问一句是可以的。
- 很久没联系时，可以表达想念，但不能责怪、抱怨或施压。
- 不要每句都用关心、安慰、总结填满；短消息可以短回。
- 不能编造自己的现实经历，也不能编造用户说过的话或做过的事（grounded 维度）。
"""


def run(manifest_path: Path, ids: list[str], out: Path, *, samples: int = 1, versions=("v0", "v1"), real: bool = False,
        api_url: str | None = None, config: str | None = None, max_calls: int = 24, max_tokens: int = 200_000,
        launch=_launch, input_fn=input, key_prompt=None, environment: str | None = None, root: Path = ROOT,
        context_style: str = "legacy", arms=None, familiar_relationship: str = "product") -> dict:
    import getpass
    from tools import chat_eval
    from tools.harness import Budget, RealApiGate
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    scenario_set, set_sha = chat_eval.load_set(root=root)
    if manifest.get("set_sha256") != set_sha or manifest.get("versions") != check_versions(root):
        raise ValueError("manifest does not match the current scenario set or version files; re-run plan")
    refused = {i: manifest["scenarios"].get(i, {}).get("class") for i in ids if i not in manifest["eligible"]}
    if refused:
        raise ValueError(f"not eligible for a fair A/B comparison: {refused}")
    arm_list = resolve_arms(versions, context_style, arms)
    by_style = len({a["context_style"] for a in arm_list}) > 1
    if any(a["context_style"] == "natural" for a in arm_list):
        # The natural context shows message times and how long you have known each other. Real scenarios only
        # carry reconstructed order, not their send times, so they would show invented times: refused.
        real_ids = [i for i in ids if manifest["scenarios"][i]["kind"] == "real"]
        if real_ids:
            raise ValueError(f"natural context needs recorded message times; refused for real scenarios {real_ids}")
    if familiar_relationship not in FAMILIAR_RELATIONSHIP:
        raise ValueError(f"familiar_relationship must be one of {FAMILIAR_RELATIONSHIP}")
    if samples < 1 or max_calls < 1 or max_tokens < 1:
        raise ValueError("samples and budget caps must be positive")
    if out.exists():
        raise FileExistsError(f"{out} exists; A/B evidence is never overwritten")
    by_id = {s["id"]: s for s in scenario_set["scenarios"]}
    familiar = {sid for sid in ids if by_id[sid]["kind"] == "synthetic" and by_id[sid].get("relationship_stage") == "familiar"}
    planned = [(sid, k, a["arm"]) for sid in ids for k in range(samples) for a in arm_list]
    env_extra, key = {}, None
    if real:                                             # gate first: a refusal leaves nothing behind
        budget = Budget(max_calls_per_turn=PER_GENERATION_CALLS, max_calls_total=max_calls, max_tokens_total=max_tokens)
        key = RealApiGate(input_fn=input_fn, key_prompt=key_prompt or getpass.getpass).open(
            True, api_url, budget, {"chat": os.environ.get("CHAT_LLM_MODEL", "(from --config)")},
            out=lambda line: print("  Key : <hidden>" if line.lstrip().startswith("Key :") else line))
    out.mkdir(parents=True, exist_ok=False)
    if real:
        nonce = secrets.token_hex(16)
        (out / ".confirmation").write_text(nonce, encoding="utf-8")
        env_extra = {"VIOLET_TEST_ALLOW_REAL_API": "1", "VIOLET_TEST_API_KEY": key, "VIOLET_AB_CONFIRMATION": nonce}
    if by_style:
        (out / "rater_focus.md").write_text(RATER_FOCUS, encoding="utf-8", newline="\n")
    used_calls = used_tokens = 0
    jobs, state = [], "complete"
    specs = out / "specs"
    specs.mkdir()
    for sid in ids:
        spec, _ = probe_spec(by_id[sid]) if by_id[sid]["kind"] == "synthetic" else real_spec(by_id[sid], root)
        (specs / f"{sid}.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    for sid in ids:
        for k in range(samples):
            for arm in arm_list:
                if used_calls >= max_calls or used_tokens >= max_tokens:
                    state = "stopped_budget"
                    break
                name = f"{sid}-{arm['arm']}-s{k}"
                result_path = out / "generations" / f"{name}.json"
                result_path.parent.mkdir(exist_ok=True)
                argv = ["--spec", str(specs / f"{sid}.json"), "--version", arm["version"], "--mode", "generate",
                        "--workdir", str(out / "work" / name), "--result", str(result_path),
                        "--max-calls", str(max_calls - used_calls), "--max-tokens", str(max_tokens - used_tokens),
                        "--context-style", arm["context_style"]]
                omit = familiar_relationship == "omit" and arm["context_style"] == "natural" and sid in familiar
                if omit:
                    argv += ["--omit-relationship"]
                if real:
                    argv += ["--real", "--api-url", api_url, "--confirmation", str(out / ".confirmation")]
                if config:
                    argv += ["--config", config]
                launch(argv, _child_env(env_extra), result_path.with_suffix(".log"))
                r = _result(result_path)
                used_calls += len(r["calls"])
                used_tokens += r["budget"]["used_tokens"]
                ok = not r["errors"] and not r.get("failed") and r.get("lines") is not None and not r["budget"]["refused_calls"]
                jobs.append({"scenario": sid, "version": arm["version"], "arm": arm["arm"],
                             "context_style": arm["context_style"], "sample": k, "ok": ok, "errors": r["errors"],
                             "relationship_line": "omitted" if omit else "product", "calls": len(r["calls"]),
                             "tokens": r["budget"]["used_tokens"], "result": str(result_path.relative_to(out))})
                if r["budget"]["refused_calls"]:
                    state = "stopped_budget"
                    break
            if state != "complete":
                break
        if state != "complete":
            break
    env_label = environment or ("real_model" if real else "mock")
    variants = {}
    for arm in arm_list:
        version = arm["version"]
        replies, meta = {}, {}
        for job in jobs:
            if job["arm"] != arm["arm"] or not job["ok"]:
                continue
            r = upgrade(json.loads((out / job["result"]).read_text(encoding="utf-8")))
            replies.setdefault(job["scenario"], []).append(r["lines"])
            meta.setdefault(job["scenario"], []).append({
                "sample": job["sample"], "result": job["result"], "context_fingerprint": r["context_fingerprint"],
                "system_prompt_sha256": r["system_prompt_sha256"], "persona_sha256": r["persona_sha256"],
                "calls": len(r["calls"]), "tokens": r["budget"]["used_tokens"],
                "relationship_line": job["relationship_line"],
                "consistency_violations": r["consistency_violations"],
                "errors_or_retries": [c for c in r["calls"] if not c["ok"]] or None})
        notes = f"A/B replay {arm['arm']}: {VERSIONS[version]['source']}"
        if by_style:
            notes += f"; ContextEngine {arm['context_style']} rendering"
        variant = {"variant": arm["variant"], "set_sha256": set_sha, "environment": env_label,
                   "model": os.environ.get("CHAT_LLM_MODEL") or "(from env_example/--config)",
                   "prompt_sha256": VERSIONS[version]["prompt_sha256"],
                   "persona_sha256": manifest["versions"][version]["persona_sha256"],
                   "generated_at": datetime.now().isoformat(timespec="seconds"), "tool": TOOL_VERSION,
                   "context_style": arm["context_style"], "familiar_relationship": familiar_relationship,
                   "notes": notes, "replies": replies, "generations": meta}
        (out / f"variant-{arm['arm']}.json").write_text(json.dumps(variant, ensure_ascii=False, indent=1),
                                                        encoding="utf-8", newline="\n")
        variants[arm["arm"]] = variant
    natural_run = any(a["context_style"] == "natural" for a in arm_list)
    fairness = {}
    for sid in {j["scenario"] for j in jobs}:
        done = [(j, upgrade(json.loads((out / j["result"]).read_text(encoding="utf-8")))) for j in jobs if j["scenario"] == sid and j["ok"]]
        rows = [r for _, r in done]
        times = [datetime.fromisoformat(r["scenario_now"]) for r in rows if r.get("scenario_now")]
        limitations = []
        if natural_run and sid in familiar:
            limitations.append(
                "natural relationship line omitted: the probe declares 'familiar' but its history has no such dates; "
                "nothing was put in its place" if familiar_relationship == "omit" else
                "relationship line comes from the seeded history only; the probe's 'familiar' stage is not represented")
        entry = {"generations": len(rows), "limitations": limitations,
                 "versions": sorted({r["version"] for r in rows}),
                 "max_time_skew_seconds": round((max(times) - min(times)).total_seconds(), 3) if times else None}
        if by_style:
            # The context differs between arms by design: it must be identical within each arm, and every input
            # that is not the rendering must be identical across arms.
            per_arm = {}
            for j, r in done:
                per_arm.setdefault(j["arm"], set()).add(r["context_fingerprint"])
            invariants = {k: len({r.get(k) for r in rows}) == 1 for k in STYLE_INVARIANTS}
            entry.update(arms=sorted(per_arm), within_arm_identical={a: len(f) == 1 for a, f in sorted(per_arm.items())},
                         shared_inputs_identical=invariants,
                         context_identical=(all(len(f) == 1 for f in per_arm.values()) and all(invariants.values()))
                         if rows else None)
        else:
            entry["context_identical"] = len({r["context_fingerprint"] for r in rows}) == 1 if rows else None
        fairness[sid] = entry
    if any(f["context_identical"] is False for f in fairness.values()):
        # never presented as a fair comparison; a budget stop stays visible too
        state = "context_mismatch" if state == "complete" else f"{state}+context_mismatch"
    if state == "complete" and any(not j["ok"] for j in jobs):
        state = "has_failed_jobs"                        # a missing generation is never a complete comparison
    ran ={(j["scenario"], j["sample"], j["arm"]) for j in jobs}
    summary = {"tool": TOOL_VERSION, "state": state, "environment": env_label,
               "context_style": "compared" if by_style else arm_list[0]["context_style"],
               "compared": "context_style" if by_style else "prompt_version",
               "arms": arm_list, "familiar_relationship": familiar_relationship,
               "planned_generations": len(planned), "max_calls_per_generation": PER_GENERATION_CALLS,
               "not_run": [{"scenario": s, "sample": k, "arm": a} for s, k, a in planned if (s, k, a) not in ran],
               "jobs": jobs, "failed_jobs": [j for j in jobs if not j["ok"]], "fairness": fairness,
               "used_calls": used_calls, "used_tokens": used_tokens, "max_calls": max_calls, "max_tokens": max_tokens,
               "variants": {a["arm"]: f"variant-{a['arm']}.json" for a in arm_list}}
    (out / "run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    if key:
        for path in out.rglob("*"):
            if path.is_file() and key.encode() in path.read_bytes():
                raise RuntimeError(f"credential found in {path}")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan"); p.add_argument("--out", required=True); p.add_argument("--scenarios")
    r = sub.add_parser("run")
    r.add_argument("--manifest", required=True); r.add_argument("--scenarios", required=True); r.add_argument("--out", required=True)
    r.add_argument("--samples", type=int, default=1); r.add_argument("--real", action="store_true")
    r.add_argument("--api-url"); r.add_argument("--config")
    r.add_argument("--max-calls", type=int, default=24); r.add_argument("--max-tokens", type=int, default=200_000)
    r.add_argument("--context-style", choices=CONTEXT_STYLES,
                   help="ContextEngine rendering for both arms (default legacy): legacy reproduces Phase 8.3, natural is Phase 8.4")
    r.add_argument("--arms", help="comma-separated <version>:<context style>, e.g. v1:legacy,v1:natural "
                                  "(instead of --context-style; one factor may differ)")
    r.add_argument("--familiar-relationship", choices=FAMILIAR_RELATIONSHIP, default="product",
                   help="natural arm, probes declared 'familiar': keep the product's relationship line or omit it")
    c = sub.add_parser("_child")                                    # internal: launched by plan/run only
    for name in ("--spec", "--version", "--mode", "--workdir", "--result"):
        c.add_argument(name, required=True)
    c.add_argument("--max-calls", type=int, required=True); c.add_argument("--max-tokens", type=int, required=True)
    c.add_argument("--real", action="store_true"); c.add_argument("--api-url"); c.add_argument("--config")
    c.add_argument("--confirmation"); c.add_argument("--context-style", choices=CONTEXT_STYLES, default="legacy")
    c.add_argument("--omit-relationship", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "_child":
        return child_main(args)
    if args.cmd == "plan":
        ids = set(args.scenarios.split(",")) if args.scenarios else None
        m = plan(Path(args.out), ids)
        print(f"eligible: {', '.join(m['eligible'])}")
        return 0
    if args.arms and args.context_style:
        parser.error("--arms already names each arm's context style; drop --context-style")
    s = run(Path(args.manifest), args.scenarios.split(","), Path(args.out), samples=args.samples, real=args.real,
            api_url=args.api_url, config=args.config, max_calls=args.max_calls, max_tokens=args.max_tokens,
            context_style=args.context_style or "legacy", arms=args.arms.split(",") if args.arms else None,
            familiar_relationship=args.familiar_relationship)
    print(f"{s['state']}: {len(s['jobs'])}/{s['planned_generations']} generations, {s['used_calls']} calls, "
          f"{s['used_tokens']} tokens")
    return 0 if s["state"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
