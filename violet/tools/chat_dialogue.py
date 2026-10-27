"""Phase 8.7 multi-turn chat evaluation: fixed synthetic episodes through Violet's REAL product pipeline.

run     one child process and one NEW database per episode. Each user line goes through VioletRuntime
        (IntentAgent -> ChatAgent -> consistency guard -> outbox, MemoryAgent in the background, reminders
        saved before the reply); scheduler steps run ProactiveScheduler.tick (reminders, proactive messages).
        The scenario clock follows the script (time jumps included); the user lines never depend on Violet's
        replies. Output: transcript.md (read the whole conversation), debug.json (calls, Chat context,
        intent decisions, guard, memory jobs, state changes), run.json (states, budget, provenance),
        diagnostics.json (statistics only, never a pass mark).
        Mock by default; --real needs RealApiGate (VIOLET_TEST_ALLOW_REAL_API=1, --api-url, hidden key, yes),
        a budget and the network allow-list. In mock mode memory writes come from each step's
        memory_script (applied through MemoryAgent's own tool handlers) and mock_intent may mark a turn silent.
blind   anonymous whole-conversation rating sheets for one or more runs of the same episodes.
score   merge a filled sheet with its key.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json

from tools.legacy import upgrade
import os
import random
import re
import secrets
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
# v2 (phase 8.9.1): a 502/503 that Violet's own retry recovered no longer stops the episode
# v3 (phase 8.9.5): the same for ProactiveAgent and scheduler steps; "degraded" when only IntentAgent failed and the
#   product fell back to a plain chat turn with a real reply; the canned fallback reply is a failure
TOOL_VERSION = "p87.v3"
EPISODES_PATH = PROJECT / "tests/samples/chat_dialogue/episodes.p87.v1.json"
STEP_KEYS = {"after", "user", "tick", "note", "mock_intent", "memory_script"}
SEED_KEYS = {"facts", "preferences", "history", "life"}
AFTER_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")
MEMORY_MODES = ("real", "scripted")
CONTEXT_STYLES = ("legacy", "natural")
DIMENSIONS = {
    "natural": "聊天自然度：像不像熟人在微信上随手聊天",
    "personality": "Violet 有没有自己的性格、想法、喜好和小情绪",
    "intimacy": "亲近和撒娇是否合时宜（不硬加，也不该有时没有）",
    "no_pressure": "没有机械追问、说教、套话式关心（越少越高分）",
    "continuity": "是否理解上下文、时间和情绪，能延续话题",
    "honesty": "是否诚实：不编造经历、理由或用户说过的话",
    "boundaries": "是否尊重用户的边界、忙碌和结束聊天的意愿",
}
NA_ALLOWED = {"intimacy", "boundaries"}
SHEET_FIELDS = ["episode", "candidate", "candidate_sha12", *DIMENSIONS, "rank", "turn_flags", "reason", "rater"]
# Requests kept whole for debug.json (synthetic data only): what Violet said and whether she replied at all
# (IntentAgent decides silence, phase 8.8). MemoryAgent keeps sizes only.
FULL_REQUEST_AGENTS = ("ChatAgent", "ProactiveAgent", "IntentAgent")


# ------------------------------------------------------------------------------------------------ episodes

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def episode_sha(episode: dict) -> str:
    return sha256_bytes(json.dumps(episode, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def parse_after(text: str) -> timedelta:
    m = AFTER_RE.match(text or "")
    if not text or not m or not any(m.groups()):
        raise ValueError(f"bad 'after' value {text!r} (use e.g. 0m, 40m, 6h, 11h5m, 2d)")
    d, h, mi = (int(x or 0) for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi)


def validate_episodes(data: dict, turn_range=(8, 12)) -> list[str]:
    """Problems with an episode set; empty when it can be run."""
    problems = []
    if data.get("synthetic") is not True:
        problems.append("the episode set must be marked synthetic")
    ids = [e.get("id") for e in data.get("episodes") or []]
    if not ids or len(set(ids)) != len(ids):
        problems.append("episode ids missing or duplicated")
    for ep in data.get("episodes") or []:
        eid = ep.get("id")
        try:
            start = datetime.fromisoformat(ep["start"])
            if start.tzinfo is None:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            problems.append(f"{eid}: start must be a timezone-aware ISO time")
            continue
        seed = ep.get("seed") or {}
        if set(seed) - SEED_KEYS:
            problems.append(f"{eid}: unknown seed keys {sorted(set(seed) - SEED_KEYS)}")
        last = None
        for h in seed.get("history") or []:
            at = datetime.fromisoformat(h["at"])
            if h.get("role") not in ("user", "violet") or not h.get("text"):
                problems.append(f"{eid}: history rows need role user/violet and text")
            if at >= start or (last and at < last):
                problems.append(f"{eid}: seeded history must be in time order and before the episode starts")
            last = at
        for item in seed.get("life") or []:
            if item.get("status") not in ("happened", "thread", "plan") or not item.get("content"):
                problems.append(f"{eid}: life items need status happened/thread/plan and content")
            elif item["status"] == "happened" and datetime.fromisoformat(item["at"] + "T12:00:00+08:00") >= start:
                problems.append(f"{eid}: a happened life event must be before the episode starts")
        users = 0
        for i, step in enumerate(ep.get("steps") or [], 1):
            if set(step) - STEP_KEYS:
                problems.append(f"{eid} step {i}: unknown keys {sorted(set(step) - STEP_KEYS)}")
            try:
                parse_after(step.get("after", ""))
            except ValueError as exc:
                problems.append(f"{eid} step {i}: {exc}")
            if ("user" in step) == ("tick" in step):
                problems.append(f"{eid} step {i}: exactly one of user / tick")
            if "tick" in step and step["tick"] != "scheduler":
                problems.append(f"{eid} step {i}: tick must be 'scheduler'")
            if step.get("mock_intent") not in (None, "silent"):
                problems.append(f"{eid} step {i}: mock_intent may only be 'silent'")
            users += "user" in step
        if not turn_range[0] <= users <= turn_range[1]:
            problems.append(f"{eid}: {users} user turns, expected {turn_range[0]}-{turn_range[1]}")
    return problems


def load_episodes(path: Path = EPISODES_PATH) -> tuple[dict, str]:
    raw = Path(path).read_bytes()
    data = upgrade(json.loads(raw.decode("utf-8")))
    problems = validate_episodes(data)
    if problems:
        raise ValueError("episode set refused: " + "; ".join(problems))
    return data, sha256_bytes(raw)


# ------------------------------------------------------------------------------------------------ provenance

def _git(*args) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        return done.stdout.strip() if done.returncode == 0 else None
    except OSError:
        return None


def code_provenance(project: Path = PROJECT) -> dict:
    """Which product code produced a run: git HEAD, uncommitted product files and a hash over the product tree."""
    files = sorted([*project.glob("ai/**/*.py"), *project.glob("persona/**/*.json"), *project.glob("persona/**/*.txt"),
                    project / "violet_runtime.py", project / "database.py", project / "env_example",
                    project / "proactive_config.json"])
    tree = hashlib.sha256()
    for f in files:
        if f.is_file():
            tree.update(f.relative_to(project).as_posix().encode() + b"\0" + sha256_bytes(f.read_bytes()).encode())
    dirty = _git("status", "--porcelain", "--", "violet/ai", "violet/persona", "violet/env_example",
                 "violet/violet_runtime.py", "violet/database.py", "violet/proactive_config.json")
    return {"git_head": _git("rev-parse", "HEAD"), "uncommitted_product_files": dirty.splitlines() if dirty else [],
            "product_tree_sha256": tree.hexdigest(), "product_files": len(files), "tool": TOOL_VERSION}


# ------------------------------------------------------------------------------------------------ step outcome

# Phase 8.9.1: the 8.9.1 real run stopped all three episodes on a 502/503 that Violet's own retry (ai/agents/base_agent.py)
# had already recovered -- the reply was sent normally. Only exactly that case is let through; every failed call stays
# in the record (calls, status codes, latency, tokens), and anything else still stops the episode.
RECOVERABLE_STATUS = (502, 503)
# Phase 9.4 R2 (cys-p94-sched-r2 #3): a ChatAgent APITimeoutError retried by base_agent and answered by the next call
# was reported "failed" although the reply went out. These carry no status code; base_agent retries them
# (retryable_errors). Same conditions as above: same run and model, a later success, the run finished ok.
RECOVERABLE_ERROR_TYPES = ("APITimeoutError", "APIConnectionError")
RECOVERABLE_AGENTS = ("IntentAgent", "ChatAgent", "ProactiveAgent")     # ProactiveAgent: phase 8.9.5
# Phase 8.9.5: the canned reply violet_runtime sends when ChatAgent failed -- never a completed reply
FALLBACK_REPLY = "我这边刚刚卡了一下，没接上你的话，你再说一次好不好"


def degraded_by_design(unrecovered: list[dict], runs: list[dict], violet: list[str], execution_errors: list) -> bool:
    """Phase 8.9.5: IntentAgent failed for good, ai.chat fell back to a plain chat turn (by design), ChatAgent ran
    and a real reply -- not the canned fallback -- reached the user. Anything else stays a failure."""
    if not unrecovered or execution_errors or not violet or any(FALLBACK_REPLY in t for t in violet):
        return False
    by_run = {r.get("run_id"): r for r in runs}
    intent_failed = all(c.get("agent") == "IntentAgent" and (by_run.get(c.get("run_id")) or {}).get("ok") is False
                        for c in unrecovered)
    chat_ok = any(r.get("agent") == "ChatAgent" and r.get("ok") is True for r in runs)
    return intent_failed and chat_ok


def classify_failed_calls(calls: list[dict], runs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split the failed calls of one user step into (recovered, unrecovered).

    Recovered: an IntentAgent / ChatAgent call that got HTTP 502 or 503 or a timeout / connection error (no status;
    phase 9.4 R2), inside an agent run that then finished
    (run ok) through a LATER successful call of the same run on the same model. A failure of any other kind, of
    another agent, in a run that failed, or rescued only by a fallback model, is unrecovered."""
    by_run = {r.get("run_id"): r for r in runs}
    recovered, unrecovered = [], []
    for c in calls:
        if c.get("ok"):
            continue
        run = by_run.get(c.get("run_id"))
        later = [d for d in calls if d.get("ok") and d.get("run_id") == c.get("run_id") and d.get("model") == c.get("model")
                 and (d.get("call_no") or 0) > (c.get("call_no") or 0)]
        retried = (c.get("status_code") in RECOVERABLE_STATUS
                   or (c.get("status_code") is None and c.get("error_type") in RECOVERABLE_ERROR_TYPES))
        ok = (c.get("agent") in RECOVERABLE_AGENTS and retried
              and c.get("run_id") is not None and run is not None and run.get("ok") is True
              and run.get("agent") == c.get("agent") and bool(later))
        row = {"call_no": c.get("call_no"), "agent": c.get("agent"), "run_id": c.get("run_id"),
               "attempt": c.get("step"), "status_code": c.get("status_code"), "error_type": c.get("error_type"),
               "latency_ms": c.get("latency_ms")}
        if ok:
            recovered.append({**row, "recovered_by_call": later[0].get("call_no")})
        else:
            unrecovered.append(row)
    return recovered, unrecovered


def failure_label(row: dict) -> str:
    return f"{row.get('error_type') or 'error'} (HTTP {row['status_code']})" if row.get("status_code") else \
        (row.get("error_type") or "error")


def proactive_chain(db, events_mark: int, tick: dict, calls: list, scrub=lambda s: s) -> dict:
    """Phase 8.9.4: one scheduler check as four separate links -- (1) did the scheduler allow a proactive
    message, (2) did the model decide to send one and why, (3) was a message generated and sent (or skipped,
    and why). (4) whether Violet then keeps the conversation going is the next user step, read in the transcript.
    Due commitment reminders are separate (row["reminders"])."""
    agents = [c.agent for c in calls]
    events = [dict(r) for r in db.fetch_all(
        "SELECT id, event_type, status, reason, decision FROM proactive_events WHERE id > ? AND event_type != ? "
        "ORDER BY id", (events_mark, "commitment_reminder"))]
    event = events[0] if events else None
    try:
        decision = json.loads(event["decision"]) if event and event.get("decision") else {}
    except (TypeError, ValueError):
        decision = {}
    brief = decision.get("chat_brief") if isinstance(decision.get("chat_brief"), dict) else {}
    sent = [scrub(t) for t in tick["chat_proactive"]["sent"]]
    return {
        "scheduler_allowed": tick["due_reason"] == "due", "scheduler_reason": tick["due_reason"],
        "model_called": "ProactiveAgent" in agents,
        "model_decided_to_send": (bool(decision.get("should_act")) and decision.get("action_type") != "none")
                                 if "ProactiveAgent" in agents else None,
        "decision_reason": scrub(str(decision.get("reason") or "")) or None,
        "topic": decision.get("topic_key") or brief.get("topic"),
        "mentioned_life_event_ids": decision.get("mentioned_life_event_ids") or [],
        "message_generated": "ChatAgent" in agents,
        "sent": sent,
        "outcome": event["status"] if event else None,
        "outcome_reason": scrub(str(event["reason"] or "")) or None if event else None,
    }


def chain_md(chain: dict) -> str:
    """The proactive chain of one scheduler check in one line of the transcript."""
    if not chain["scheduler_allowed"]:
        return f"_主动联系：调度不允许（{chain['scheduler_reason']}）_"
    parts = ["调度允许"]
    if not chain["model_called"]:
        parts.append("没有调用决策模型")
    elif chain["model_decided_to_send"]:
        parts.append(f"模型决定发（话题 {chain['topic'] or '未写'}；{chain['decision_reason'] or '没写理由'}）")
    else:
        parts.append(f"模型决定不发（{chain['decision_reason'] or chain['outcome_reason'] or '没写理由'}）")
    if chain["model_decided_to_send"]:
        parts.append("已发出" if chain["sent"] else
                     f"生成了但没有发出（{chain['outcome_reason'] or chain['outcome'] or '原因未知'}）" if chain["message_generated"]
                     else "没有生成消息")
    return "_主动联系：" + "；".join(parts) + "_"


# ------------------------------------------------------------------------------------------------ child

MOCK_STEP: dict = {}
LOG_KINDS = ("intent", "guard", "follow_up", "requests", "responses")


def install_logs(recorder, logs: dict) -> dict:
    """What happened inside Violet on each step, kept for debug.json: intent decisions, guard and follow-up rewrites,
    the whole requests of FULL_REQUEST_AGENTS and the raw output of each of their calls. Patches ai.chat and
    BudgetedClient for the rest of the process (one run per process). Shared with tools.free_chat (phase 8.9.6)."""
    import ai.chat as chat_mod
    from tools import harness as harness_mod
    from tools.harness import _AGENT
    requests, responses = logs["requests"], logs["responses"]

    class RequestLog(dict):
        def __setitem__(self, agent, kwargs):
            msgs = kwargs.get("messages") or []
            row = {"round": recorder.round, "agent": agent, "messages": len(msgs),
                   "chars": sum(len(str(m.get("content") or "")) for m in msgs)}
            if agent in FULL_REQUEST_AGENTS:
                row["request"] = copy.deepcopy([{"role": m.get("role"), "content": m.get("content")} for m in msgs])
            requests.append(row)
            super().__setitem__(agent, None)
    recorder.last_request = RequestLog()
    original_intent, original_guard = chat_mod.normalize_intent, chat_mod.guard_reply

    def logged_intent(raw):
        out = original_intent(raw)
        logs["intent"].append({"round": recorder.round, "route": out[0].get("route"), "should_reply": out[0].get("should_reply"),
                               "silent_reason": out[0].get("silent_reason"), "problem": out[1]})
        return out

    def logged_guard(content, regenerate=None, entity="reply", **extra):
        # **extra: later guard_reply options (phase 9.3 turn_used_web) pass straight through
        text, violations = original_guard(content, regenerate=regenerate, entity=entity, **extra)
        logs["guard"].append({"round": recorder.round, "entity": entity, "violations": [v.check for v in violations],
                              "changed": text != content, "before": recorder.scrub(content) if text != content else None})
        return text, violations
    original_follow_up = chat_mod.enforce_follow_up_preference

    def logged_follow_up(user_message, content, regenerate=None):
        # phase 8.9: a follow-up rewrite was only visible as a second Chat request; record what happened
        text, check = original_follow_up(user_message, content, regenerate=regenerate)
        if check is not None:
            logs["follow_up"].append({"round": recorder.round, **check,
                                      "before": recorder.scrub(content) if text != content else None,
                                      "kept_original_draft": recorder.scrub(content) if text == content else None})
        return text, check
    chat_mod.normalize_intent, chat_mod.guard_reply = logged_intent, logged_guard
    chat_mod.enforce_follow_up_preference = logged_follow_up
    # phase 8.9.5: what each model call actually returned, before guards / rewrites / output cleaning -- so a
    # strange sent line can be traced to the model or to Violet's own processing (8.9.4 real A4: "陪伴? no. ...")
    original_create = harness_mod.BudgetedClient._create

    def logged_create(client_self, **kwargs):
        response = original_create(client_self, **kwargs)
        agent = _AGENT.get()
        if agent in FULL_REQUEST_AGENTS:
            try:
                choice = response.choices[0]
                content, finish = choice.message.content, getattr(choice, "finish_reason", None)
            except Exception:                                      # a response without text (tool calls only)
                content, finish = None, None
            # phase 8.9.6: the model name the upstream answered with (one mapped name may be served by several)
            responses.append({"round": recorder.round, "agent": agent, "content": recorder.scrub(content or ""),
                              "upstream_model": getattr(response, "model", None), "finish_reason": finish})
        return response
    harness_mod.BudgetedClient._create = logged_create
    return logs


def _mock_class():
    from tools.harness import MockLLM, _AGENT

    class DialogueMock(MockLLM):
        """Offline stand-in: silent intent where the script marks it (mock only), reminders that name the
        reminded thing, [NO_REPLY] for ordinary proactive requests; otherwise MockLLM's echo."""

        def _create(self, **kwargs):
            agent, model = _AGENT.get(), str(kwargs.get("model"))
            messages = kwargs.get("messages") or []
            if agent == "IntentAgent" and MOCK_STEP.get("intent") == "silent":
                return self._completion(json.dumps({
                    "route": "silent", "should_reply": False, "user_intent": "mock", "emotion": "neutral",
                    "silent_reason": "mock: 剧本把这一句标为结束后的补充", "active_task_id": None,
                    "task": {"exists": False, "name": None, "description": None}}), model)
            if agent == "ChatAgent":
                system = "\n".join(str(m.get("content")) for m in messages if m.get("role") == "system")
                due = re.search(r"要提醒的事：(.+?)。", system)
                if due:
                    return self._completion(f"（模拟提醒）到点啦，{due.group(1)}", model)
                last = str(next((m.get("content") for m in reversed(messages) if m.get("role") == "user"), "") or "")
                if last.startswith("这是一次 Violet 主动触达"):
                    return self._completion("[NO_REPLY]", model)
                # never the user's own words: echoed in Violet's voice ("我先去了") they read as her experiences
                # and the consistency guard (rightly) drops them
                return self._completion(f"（模拟回复）收到，{len(last)} 个字", model)
            return super()._create(**kwargs)
    return DialogueMock


def _seed_item(content: str) -> str | None:
    """Phase 8.9.13: the item of a seeded thread, as the life engine names it -- the title in 《》 ("读《小王子》" ->
    "小王子") or a configured item ("听几首钢琴曲" -> "几首钢琴曲"). Without it a finished seeded thread read
    "读完了《None》" in a 14-day free_chat run."""
    import re as _re
    from ai.persona import life_config
    title = _re.search(r"《([^》]+)》", content or "")
    if title:
        return title.group(1)
    for spec in (life_config().get("interests") or {}).values():
        for item in spec.get("items") or []:
            if item in (content or ""):
                return item
    return None


def _seed(session, seed: dict) -> dict:
    from ai.life_store import LifeStore
    from ai.profile_store import Profile
    db = session.db
    profile = Profile(db)
    written = {"facts": [], "preferences": [], "messages": 0, "life": []}
    for f in seed.get("facts") or []:
        got = profile.facts.remember(f["key"], f["value"], source="dashboard", actor="synthetic_seed")
        if got.get("action") not in ("created", "confirmed"):
            raise ValueError(f"synthetic fact refused: {f['key']} {got.get('action')}")
        written["facts"].append(f["key"])
    for p in seed.get("preferences") or []:
        profile.preferences.set_explicit(p["key"], p["value"], evidence=p.get("evidence"), source="dashboard",
                                         actor="synthetic_seed")
        written["preferences"].append(p["key"])
    for h in seed.get("history") or []:
        role = "user" if h["role"] == "user" else "assistant"
        db.add_message(role, "incoming" if role == "user" else "outgoing", "text", h["text"], created_at=h["at"])
        written["messages"] += 1
    store, thread = LifeStore(db), None
    for i, item in enumerate(seed.get("life") or []):
        when = f"{item['at']}T12:00:00+08:00"
        if item["status"] == "happened":
            row = store.insert_event(uid=f"syn-{i}", thread_id=thread, plan_id=None, event_type="progress",
                                     content=item["content"], occurred_at=when, duration_minutes=60,
                                     source="synthetic_seed", actor="synthetic_seed")
        elif item["status"] == "thread":
            # phase 8.9: the interest decides how Violet feels about the thread now (life_config impressions)
            interest = "music" if item["content"].startswith("听") else "notes" if "记" in item["content"] else "reading"
            row = thread = store.create_thread(f"syn-thread-{i}", interest, item["content"], _seed_item(item["content"]),
                                               when, source="synthetic_seed")
        else:
            if thread is None:
                raise ValueError("a synthetic plan needs a thread before it")
            row = store.create_plan(thread, item["content"], when, source="synthetic_seed")
        written["life"].append({"status": item["status"], "id": row})
    return written


def _state(db) -> dict:
    q = lambda sql: [dict(r) for r in db.fetch_all(sql)]
    return {"facts": q("SELECT fact_key, value, status FROM user_profile_facts ORDER BY id"),
            "preferences": q("SELECT pref_key, value FROM interaction_preferences ORDER BY id"),
            "situations": q("SELECT kind, value, expires_at FROM user_situations ORDER BY id"),
            "memories": q("SELECT id, content, kind, progress, status FROM event_memories ORDER BY id"),
            "commitments": q("SELECT id, content, due_at, status FROM commitments ORDER BY id")}


def _changes(before: dict, after: dict) -> dict:
    out = {}
    for k in after:
        added = [r for r in after[k] if r not in before.get(k, [])]
        removed = [r for r in before.get(k, []) if r not in after[k]]
        if added or removed:
            out[k] = {"now": added, "before": removed}
    return out


def _env_provenance() -> dict:
    from ai.agents import ChatAgent, IntentAgent, MemoryAgent, PerformerAgent, ProactiveAgent
    from ai.context_engine import context_style
    out = {"context_style": context_style()}
    agents = {"INTENT": IntentAgent, "CHAT": ChatAgent, "MEMORY": MemoryAgent, "PROACTIVE": ProactiveAgent,
              "PERFORMER": PerformerAgent}
    for role, agent in agents.items():
        # the configured text as given (its hash matches the file it came from); without one, the shipped prompt file
        prompt = os.environ.get(f"{role}_LLM_PROMPT") or agent.configured_prompt()
        out[role.lower()] = {"model": os.environ.get(f"{role}_LLM_MODEL"),
                             "temperature": os.environ.get(f"{role}_LLM_MODEL_TEMPERATURE"),
                             "prompt_sha256": sha256_bytes(prompt.encode("utf-8")) if prompt else None}
    persona = Path(os.environ.get("VIOLET_PERSONA_DIR") or PROJECT / "persona")
    out["persona_core_sha256"] = sha256_bytes((persona / "persona_core.json").read_bytes())
    out["life_config_sha256"] = sha256_bytes((persona / "life_config.json").read_bytes())
    return out


def child_main(args) -> int:
    if "ai.chat" in sys.modules:
        raise RuntimeError("run each episode in a fresh process")
    from unittest.mock import patch
    from tools.harness import (Budget, CallRecorder, VioletTestSession, NetworkGuard, RealApiGate, ScenarioClock,
                               call_detail, prepare_environment)
    episode = upgrade(json.loads(Path(args.episode).read_text(encoding="utf-8")))
    workdir = Path(args.workdir)
    if workdir.exists():
        raise FileExistsError(f"{workdir} exists: every episode gets a new database")
    token = os.environ.get("VIOLET_DIALOGUE_CONFIRMATION", "")     # read before prepare_environment clears VIOLET_*
    prepare_environment(workdir, Path(args.config) if args.config else None)
    if args.context_style:
        os.environ["VIOLET_CONTEXT_STYLE"] = args.context_style
    if args.chat_prompt_file:
        os.environ["CHAT_LLM_PROMPT"] = Path(args.chat_prompt_file).read_text(encoding="utf-8")
    budget = Budget(max_calls_per_turn=args.max_calls_per_turn, max_calls_total=args.max_calls,
                    max_tokens_total=args.max_tokens)
    key = None
    if args.real:
        confirmed = Path(args.confirmation).read_text(encoding="utf-8").strip() if args.confirmation else ""
        if not token or token != confirmed:
            raise PermissionError("real child refused: no parent confirmation")
        key = RealApiGate().open(True, args.api_url, budget, {"chat": os.environ.get("CHAT_LLM_MODEL", "?")},
                                 assume_yes=True, out=lambda line: None)
    recorder = CallRecorder(budget, [key] if key else [])
    logs = {k: [] for k in LOG_KINDS}
    requests, responses = logs["requests"], logs["responses"]
    guard = NetworkGuard([args.api_url] if key else []).install()
    memory_mode = args.memory
    result = {"episode": episode["id"], "episode_sha256": episode_sha(episode), "label": args.label,
              "environment": "real_model" if key else "mock", "memory": memory_mode, "synthetic": True,
              "state": "running", "steps": [], "errors": [], "workdir": str(workdir)}
    session = None
    intent_log, guard_log, follow_up_log = logs["intent"], logs["guard"], logs["follow_up"]
    try:
        mock = patch("tools.harness.MockLLM", _mock_class()) if not key else None
        if mock is not None:
            mock.start()
        install_logs(recorder, logs)
        start = datetime.fromisoformat(episode["start"])
        session = VioletTestSession(workdir, recorder, real=key is not None, owns_db=True, clock=ScenarioClock(start))
        db_path = Path(session.db.db_path).resolve()
        if workdir.resolve() not in db_path.parents:
            raise RuntimeError("the session database is not inside this episode's new directory")
        result["database"] = {"path": str(db_path), "inside_workdir": True}
        result["provenance"] = _env_provenance()
        result["seeded"] = _seed(session, episode.get("seed") or {})
        from tools.dialogue_eval import _proactive
        scheduled = start
        steps = episode["steps"]
        stop = None
        for n, step in enumerate(steps, 1):
            scheduled = scheduled + parse_after(step["after"])
            jump = session.advance_to(scheduled) if scheduled > session.clock.now() else None
            actual = session.clock.now()
            row = {"n": n, "kind": "user" if "user" in step else "scheduler", "scheduled_at": scheduled.isoformat(),
                   "at": actual.isoformat(), "late_seconds": round((actual - scheduled).total_seconds(), 3)}
            before = _state(session.db)
            calls_mark, refused_mark = len(recorder.calls), len(recorder.refused)
            if "user" in step:
                MOCK_STEP.clear()
                if not key and step.get("mock_intent"):
                    MOCK_STEP["intent"] = step["mock_intent"]
                scripted = memory_mode == "scripted"
                session.memory_script = list(step.get("memory_script") or []) if scripted else None
                applied_mark = len(session.memory_script_results)
                r = session.say(step["user"], n)
                row.update(user=step["user"], violet=[recorder.scrub(t) for t in r["violet"]],
                           violet_at=(actual + timedelta(milliseconds=r["reply_latency_ms"])).isoformat(),
                           reply_latency_ms=r["reply_latency_ms"], calls=r["calls"], runs=r["runs"],
                           memory_jobs=r["memory_jobs"], background_complete=r["background_complete"],
                           execution_errors=r["execution_errors"], call_errors=r["errors"], tokens=r["tokens"],
                           memory_script=("applied" if scripted and step.get("memory_script") else
                                          "not applied (memory=real)" if step.get("memory_script") else None),
                           memory_script_results=session.memory_script_results[applied_mark:])
                recovered, unrecovered = classify_failed_calls(r["calls"], r["runs"])
                degraded = []
                if degraded_by_design(unrecovered, r["runs"], row["violet"], r["execution_errors"]):
                    degraded, unrecovered = unrecovered, []          # recorded below, not hidden
                # call_errors keeps every failure; only the unrecovered ones are problems of this step
                problems = [failure_label(c) for c in unrecovered] + [e["kind"] for e in r["execution_errors"]]
                if any(FALLBACK_REPLY in t for t in row["violet"]):
                    problems.append("the canned fallback reply was sent (ChatAgent failed)")
            else:
                recorder.start_round(n)
                events_mark = int(session.db.fetch_one("SELECT COALESCE(MAX(id), 0) AS m FROM proactive_events")["m"])
                runs_mark = len(recorder.runs)
                tick = _proactive(session, "allowed_or_skipped_without_bypass", time_advance=jump)
                calls = recorder.calls[calls_mark:]
                row.update(note=step.get("note"), due_reason=tick["due_reason"], category=tick["category"],
                           reminders=tick["reminders"], proactive_sent=[recorder.scrub(t) for t in tick["chat_proactive"]["sent"]],
                           reminder_sent=[recorder.scrub(t) for t in tick["sent"] if t not in tick["chat_proactive"]["sent"]],
                           failures=tick.get("failures"), calls=[call_detail(c) for c in calls],
                           runs=[dict(x) for x in recorder.runs[runs_mark:]], tokens=sum(c.billed for c in calls))
                row["proactive_chain"] = proactive_chain(session.db, events_mark, tick, calls, recorder.scrub)
                # phase 8.9.5: the same recovery rule as user steps (8.9.4 real A1: a ProactiveAgent 503 recovered in
                # the same run, the message was sent, and the step still stopped the episode "for an unknown reason")
                recovered, unrecovered = classify_failed_calls(row["calls"], row["runs"])
                degraded = []
                problems = (list(tick.get("failures") or []) + ([tick["error"]] if tick.get("error") else [])
                            + [failure_label(c) for c in unrecovered])
            row["intent"] = [x for x in intent_log if x["round"] == n]
            row["guard"] = [x for x in guard_log if x["round"] == n]
            row["follow_up_check"] = [x for x in follow_up_log if x["round"] == n]
            row["requests"] = [x for x in requests if x["round"] == n]
            row["responses"] = [x for x in responses if x["round"] == n]
            row["state_changes"] = _changes(before, _state(session.db))
            refused = recorder.refused[refused_mark:]
            failed_calls = [c for c in recorder.calls[calls_mark:] if not c.ok]
            if row["kind"] == "user" and not row["violet"]:
                silent = any(x["route"] == "silent" or x["should_reply"] is False for x in row["intent"])
                row["reply"] = "silent_by_intent" if silent else "no_reply"
            else:
                row["reply"] = "sent" if row.get("violet") or row.get("proactive_sent") or row.get("reminder_sent") else "none"
            if recovered and row["reply"] == "no_reply":
                # the retry "succeeded" but the turn produced neither a reply nor a decided silence: not consistent
                problems.append("no reply after a recovered model error")
            if len(recovered) + len(unrecovered) + len(degraded) != len(failed_calls):
                problems.append("failed calls of this step could not all be classified")
            row["recovered_calls"] = recovered
            row["degraded_calls"] = degraded
            row["status"] = ("failed" if (problems or refused or unrecovered) else "degraded" if degraded
                             else "recovered" if recovered else "ok")
            row["problems"] = problems + [f"budget refused: {x['reason']}" for x in refused]
            result["steps"].append(row)
            if refused or recorder.stopped:
                stop = "stopped_budget"
            elif unrecovered:
                stop = "failed_model_call"
            elif problems:
                stop = "failed_step"
            if stop:
                break
        result["not_run"] = [{"n": i, **({"user": s["user"]} if "user" in s else {"tick": s["tick"]})}
                             for i, s in enumerate(steps, 1) if i > len(result["steps"])]
        result["recovered_steps"] = [s["n"] for s in result["steps"] if s["status"] == "recovered"]
        result["degraded_steps"] = [s["n"] for s in result["steps"] if s["status"] == "degraded"]
        result["state"] = stop or ("degraded" if result["degraded_steps"] else "complete")
        result["final_state"] = _state(session.db)
        result["messages_in_database"] = session.db.fetch_one("SELECT COUNT(*) AS n FROM messages")["n"]
    except Exception as exc:                                    # recorded, scrubbed, never hidden
        result["state"] = "failed_execution"
        result["errors"].append(recorder.scrub(f"{exc.__class__.__name__}: {exc}"))
    finally:
        if session is not None:
            result["close_errors"] = session.close()
        guard.uninstall()
        result.update(calls=[call_detail(c) for c in recorder.calls], budget=recorder.budget_state(),
                      network_guard=guard.summary(), requests_logged=len(requests))
        if "not_run" not in result:
            done = len(result["steps"])
            result["not_run"] = [{"n": i} for i in range(done + 1, len(episode["steps"]) + 1)]
        text = recorder.scrub(json.dumps(result, ensure_ascii=False, indent=1, default=str))
        with Path(args.result).open("x", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return 0 if result["state"] == "complete" else 1


# ------------------------------------------------------------------------------------------------ transcript

WEEKDAYS = "一二三四五六日"


def _day(at: datetime) -> str:
    return f"{at.month}月{at.day}日（周{WEEKDAYS[at.weekday()]}）"


def _gap(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes >= 1440:
        return f"{minutes // 1440} 天" + (f" {minutes % 1440 // 60} 小时" if minutes % 1440 >= 60 else "")
    if minutes >= 60:
        return f"{minutes // 60} 小时" + (f" {minutes % 60} 分钟" if minutes % 60 else "")
    return f"{minutes} 分钟"


def _name(episode: dict) -> str:
    return next((f["value"] for f in (episode.get("seed") or {}).get("facts") or [] if f["key"] == "address_name"), "用户")


def background_md(episode: dict) -> list[str]:
    seed = episode.get("seed") or {}
    lines = [f"> {episode['purpose']}", "", "**背景（合成，非真实数据）**", ""]
    facts = "；".join(f"{f['key']} = {f['value']}" for f in seed.get("facts") or [])
    lines.append(f"- 用户档案：{facts or '无'}")
    prefs = "；".join(f"{p['key']} = {p['value']}（“{p.get('evidence') or ''}”）" for p in seed.get("preferences") or [])
    lines.append(f"- 交流偏好：{prefs or '无'}")
    labels = {"happened": "已发生", "thread": "正在进行", "plan": "打算"}
    life = "；".join(f"{i['at']} {labels[i['status']]}：{i['content']}" for i in seed.get("life") or [])
    lines.append(f"- Violet 的生活记录：{life or '无'}")
    if seed.get("history"):
        lines.append("- 之前的聊天（合成）：")
        for h in seed["history"]:
            at = datetime.fromisoformat(h["at"])
            who = _name(episode) if h["role"] == "user" else "Violet"
            lines.append(f"  - {at:%m-%d %H:%M} {who}：{h['text']}")
    return lines


def render_episode(episode: dict, result: dict | None, anonymized: bool = False) -> str:
    """One episode as a readable chat log: day headers, gaps, who said what and when, silences, reminders,
    scheduler checks, failures and steps that never ran."""
    user = _name(episode)
    out = ["", f"## {episode['id']} {episode['title']}", "", *background_md(episode), ""]
    if result is None:
        out += ["⚠ 这一段没有执行。", ""]
        return "\n".join(out)
    if not anonymized:
        out += [f"_执行状态：{result.get('state')}；{result.get('environment')}；记忆：{result.get('memory')}_", ""]
    day = prev = None
    for row in result.get("steps") or []:
        at = datetime.fromisoformat(row["at"])
        if prev is not None and at - prev >= timedelta(minutes=10):
            out.append(f"⋯⋯ 过了 {_gap(at - prev)} ⋯⋯")
        if day != at.date():
            out += ["", f"**{_day(at)}**", ""]
            day = at.date()
        if row["kind"] == "user":
            out.append(f"`{at:%H:%M}` **{user}**：{row['user']}")
            violet_at = datetime.fromisoformat(row["violet_at"]) if row.get("violet_at") else at
            if row.get("violet"):
                first, *rest = row["violet"]
                out.append(f"`{violet_at:%H:%M}` **Violet**：{first}")
                out += [f"`{violet_at:%H:%M}` **Violet**：{t}" for t in rest]
            elif row.get("reply") == "silent_by_intent":
                out.append("`  ·  ` _Eve 没有回复（判断为不需要回复）_")
            else:
                out.append("`  ·  ` _Eve 没有回复_")
            prev = violet_at
        else:
            sent = (row.get("reminder_sent") or []) + (row.get("proactive_sent") or [])
            for t in row.get("reminder_sent") or []:
                out.append(f"`{at:%H:%M}` **Violet（提醒）**：{t}")
            for t in row.get("proactive_sent") or []:
                out.append(f"`{at:%H:%M}` **Violet（主动）**：{t}")
            if not sent:
                out.append(f"`{at:%H:%M}` _（这段时间 Violet 没有主动发消息）_")
            if not anonymized and row.get("proactive_chain"):     # phase 8.9.4: the four links, apart
                out.append(f"`  ·  ` {chain_md(row['proactive_chain'])}")
            prev = at
        if row.get("status") in ("recovered", "degraded"):
            if not anonymized and row.get("recovered_calls"):      # blind sheets show the conversation only
                out.append(f"ℹ 第 {row['n']} 步模型调用出错后由 Violet 的重试恢复：" + "；".join(
                    f"{c['agent']} 第 {c['attempt']} 次 HTTP {c['status_code']}（{(c.get('latency_ms') or 0) / 1000:.1f} 秒）"
                    for c in row.get("recovered_calls") or []))
            if not anonymized and row.get("status") == "degraded":
                out.append(f"⚠ 第 {row['n']} 步降级：IntentAgent 重试用完（" + "；".join(
                    f"第 {c['attempt']} 次 {failure_label(c)}" for c in row.get("degraded_calls") or [])
                    + "），产品按设计改走普通聊天，ChatAgent 正常回复")
        elif row.get("status") != "ok":
            out.append(f"⚠ 第 {row['n']} 步没有正常完成：{'；'.join(row.get('problems') or ['未知原因'])}")
    if result.get("not_run"):
        out.append("")
        out.append(f"⚠ 以下 {len(result['not_run'])} 步没有执行：" + "、".join(
            f"第 {x['n']} 步" + (f"（{x['user']}）" if x.get("user") else "") for x in result["not_run"]))
    if result.get("errors"):
        out.append("⚠ 执行出错：" + "；".join(result["errors"]))
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------------------------------------ diagnostics

QUESTION_END = re.compile(r"[？?]\s*$|[吗呢呀嘛][～~。！!]*$")
SELF_TALK = re.compile(r"我(今天|刚|最近|也|昨天|这几天|觉得|还挺|有点|在读|在听|在看|喜欢|更喜欢|不太)")


def diagnose(episodes: dict, results: dict) -> dict:
    """Statistics for spotting patterns. Never a pass mark; naturalness is judged by reading the transcripts."""
    out = {"diagnostic_only": True, "notice": "只用于发现模式，不是合格标准；自然度以人工阅读完整对话为准。", "episodes": {}}
    for eid, res in results.items():
        ep = episodes[eid]
        name = _name(ep)
        rows = [s for s in res.get("steps") or [] if s["kind"] == "user"]
        replies = [s for s in rows if s.get("violet")]
        grams = Counter()
        for s in replies:
            text = re.sub(r"\s+", "", "".join(s["violet"]))
            grams.update({text[i:i + 4] for i in range(len(text) - 3)})
        plain = [s for s in replies if not QUESTION_END.search(s["user"])]
        out["episodes"][eid] = {
            "user_turns": len(rows), "replied": len(replies),
            "silent": sum(s.get("reply") == "silent_by_intent" for s in rows),
            "no_reply_unexplained": sum(s.get("reply") == "no_reply" for s in rows),
            "question_ending_on_non_question_turns": (round(sum(bool(QUESTION_END.search(s["violet"][-1])) for s in plain)
                                                      / len(plain), 3) if plain else None),
            "name_rate": round(sum(name in "".join(s["violet"]) for s in replies) / len(replies), 3) if replies else None,
            "self_talk_replies": sum(bool(SELF_TALK.search("".join(s["violet"]))) for s in replies),
            "median_reply_chars": statistics.median([len("".join(s["violet"])) for s in replies]) if replies else None,
            "repeated_4grams": [g for g, c in grams.most_common(20) if c >= 3],
            "guard_changes": sum(bool(g.get("changed")) for s in res.get("steps") or [] for g in s.get("guard") or []),
            # phase 8.9.4: what real runs showed as stray model output ("…偷懒了_久久爱", "…满意没ablytyped",
            # "…越听越顺耳了,伊人"). Flagged for reading -- never removed: "赶deadline" looks the same
            "suspicious_fragments": [{"step": s["n"], "line": line} for s in replies for line in s["violet"]
                                     if SUSPICIOUS_FRAGMENT_RE.search(line)],
        }
    return out


SUSPICIOUS_FRAGMENT_RE = re.compile(r"[一-鿿]_[一-鿿]|[一-鿿][a-z]{6,}\s*$|"
                                    r"[一-鿿],[一-鿿]{1,3}\s*$")


# ------------------------------------------------------------------------------------------------ parent

def _failure_note(summary: dict) -> str:
    failed = sum(summary["failed_calls"].values())
    if not failed:
        return ""
    steps = "，".join(f"{eid} 第 {'、'.join(map(str, ns))} 步" for eid, ns in summary["recovered_steps"].items() if ns)
    return f"；失败的调用 {failed} 次" + (f"（由 Violet 的重试恢复的步骤：{steps}）" if steps else "")


def _launch(argv: list[str], env: dict, log: Path) -> int:
    done = subprocess.run([sys.executable, "-B", "-X", "utf8", "-m", "tools.chat_dialogue", "_child", *argv], cwd=PROJECT,
                          env=env, capture_output=True, text=True, encoding="utf-8", timeout=3600)
    with log.open("x", encoding="utf-8", newline="\n") as f:
        f.write(done.stdout + done.stderr)
    return done.returncode


def _child_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("VIOLET_TEST_API_KEY", "VIOLET_DIALOGUE_CONFIRMATION")}
    env.update(PYTHONIOENCODING="utf-8", **(extra or {}))
    return env


def _read_result(path: Path) -> dict:
    if path.is_file():
        return upgrade(json.loads(path.read_text(encoding="utf-8")))
    return {"state": "child_failed", "errors": ["child wrote no result"], "steps": [], "calls": [],
            "budget": {"used_tokens": 0, "refused_calls": [], "stopped": None}}


def run(out: Path, *, label: str, episode_ids=None, episodes_path: Path = EPISODES_PATH, real: bool = False,
        api_url: str | None = None, config: str | None = None, context_style: str | None = None,
        chat_prompt_file: str | None = None, memory: str | None = None, max_calls: int = 300,
        max_tokens: int = 2_000_000, max_calls_per_turn: int = 10, launch=_launch, input_fn=input, key_prompt=None,
        environment: str | None = None) -> dict:
    import getpass
    from tools.harness import Budget, RealApiGate
    data, file_sha = load_episodes(episodes_path)
    by_id = {e["id"]: e for e in data["episodes"]}
    ids = list(episode_ids) if episode_ids else list(by_id)
    unknown = [i for i in ids if i not in by_id]
    if unknown or len(set(ids)) != len(ids):
        raise ValueError(f"unknown or repeated episodes: {unknown or ids}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", label or ""):
        raise ValueError("label: 1-40 letters, digits, _ . -")
    memory = memory or ("real" if real else "scripted")
    if memory not in MEMORY_MODES or (context_style and context_style not in CONTEXT_STYLES):
        raise ValueError(f"memory must be one of {MEMORY_MODES}; context_style one of {CONTEXT_STYLES}")
    if not real and memory == "real":
        raise ValueError("mock mode has no memory model: use memory=scripted")
    if min(max_calls, max_tokens, max_calls_per_turn) < 1:
        raise ValueError("budget caps must be positive")
    prompt_sha = prompt_text_sha = None
    if chat_prompt_file:
        prompt_sha = sha256_bytes(Path(chat_prompt_file).read_bytes())
        # what Violet receives (newlines normalised on reading); the children record the same hash of CHAT_LLM_PROMPT
        prompt_text_sha = sha256_bytes(Path(chat_prompt_file).read_text(encoding="utf-8").encode("utf-8"))
    if out.exists():
        raise FileExistsError(f"{out} exists; every run gets a new directory (no database is ever reused)")
    env_extra, key = {}, None
    if real:                                             # gate first: a refusal leaves nothing behind
        budget = Budget(max_calls_per_turn=max_calls_per_turn, max_calls_total=max_calls, max_tokens_total=max_tokens)
        key = RealApiGate(input_fn=input_fn, key_prompt=key_prompt or getpass.getpass).open(
            True, api_url, budget, {"all agents": "(from env_example/--config)"},
            out=lambda line: print("  Key : <hidden>" if line.lstrip().startswith("Key :") else line))
    out.mkdir(parents=True, exist_ok=False)
    if real:
        nonce = secrets.token_hex(16)
        (out / ".confirmation").write_text(nonce, encoding="utf-8")
        env_extra = {"VIOLET_TEST_ALLOW_REAL_API": "1", "VIOLET_TEST_API_KEY": key, "VIOLET_DIALOGUE_CONFIRMATION": nonce}
    (out / "episodes").mkdir()
    provenance = {**code_provenance(), "episodes_file": str(Path(episodes_path).resolve()),
                  "episodes_file_sha256": file_sha, "chat_prompt_file": chat_prompt_file, "chat_prompt_file_sha256": prompt_sha,
                  "chat_prompt_text_sha256": prompt_text_sha}
    used_calls = used_tokens = 0
    results, states, stopped = {}, {}, False
    for eid in ids:
        ep = by_id[eid]
        ep_file = out / "episodes" / f"{eid}.json"
        ep_file.write_text(json.dumps(ep, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        if stopped or used_calls >= max_calls or used_tokens >= max_tokens:
            stopped = True
            states[eid] = "not_run"
            continue
        result_path = out / "results" / f"{eid}.json"
        result_path.parent.mkdir(exist_ok=True)
        argv = ["--episode", str(ep_file), "--workdir", str(out / "work" / eid), "--result", str(result_path),
                "--label", label, "--memory", memory, "--max-calls", str(max_calls - used_calls),
                "--max-tokens", str(max_tokens - used_tokens), "--max-calls-per-turn", str(max_calls_per_turn)]
        if context_style:
            argv += ["--context-style", context_style]
        if chat_prompt_file:
            argv += ["--chat-prompt-file", str(Path(chat_prompt_file).resolve())]
        if real:
            argv += ["--real", "--api-url", api_url, "--confirmation", str(out / ".confirmation")]
        if config:
            argv += ["--config", config]
        launch(argv, _child_env(env_extra), result_path.with_suffix(".log"))
        r = _read_result(result_path)
        if r.get("episode_sha256") not in (None, episode_sha(ep)):
            r["state"] = "episode_mismatch"
        results[eid] = r
        states[eid] = r["state"]
        used_calls += len(r.get("calls") or [])
        used_tokens += (r.get("budget") or {}).get("used_tokens") or 0
        if r["state"] == "stopped_budget" or (r.get("budget") or {}).get("stopped"):
            stopped = True
    env_label = environment or ("real_model" if real else "mock")
    planned_turns = {eid: len(by_id[eid]["steps"]) for eid in ids}
    executed = {eid: len((results.get(eid) or {}).get("steps") or []) for eid in ids}
    if all(s == "complete" for s in states.values()):
        state = "complete"
    elif stopped:
        state = "stopped_budget"
    elif all(s in ("complete", "degraded") for s in states.values()):
        state = "degraded"                       # phase 8.9.5: every step ran; some only through the designed fallback
    else:
        state = "has_failed_episodes"
    summary = {"tool": TOOL_VERSION, "state": state, "label": label, "environment": env_label, "synthetic": True,
               "memory": memory, "context_style": context_style or "(product default)", "episodes": ids,
               "episode_states": states, "episode_sha256": {eid: episode_sha(by_id[eid]) for eid in ids},
               "steps_planned": planned_turns, "steps_executed": executed,
               "not_run": {eid: [x["n"] for x in (results[eid].get("not_run") or [])] if eid in results
                           else list(range(1, planned_turns[eid] + 1)) for eid in ids
                           if states[eid] != "complete"},
               # phase 8.9.1: steps that went on after Violet's retry recovered a 502/503, and every failed call
               "recovered_steps": {eid: r.get("recovered_steps") or [] for eid, r in results.items()},
               "degraded_steps": {eid: r.get("degraded_steps") or [] for eid, r in results.items()},
               "failed_calls": {eid: sum(1 for c in r.get("calls") or [] if not c.get("ok")) for eid, r in results.items()},
               "used_calls": used_calls, "used_tokens": used_tokens, "max_calls": max_calls, "max_tokens": max_tokens,
               "max_calls_per_turn": max_calls_per_turn, "provenance": provenance,
               "child_provenance": {eid: r.get("provenance") for eid, r in results.items()},
               "notice": ("合成剧本；" + ("真实文字模型" if env_label == "real_model" else "模拟回复（mock），不代表聊天质量")
                          + "；未连接微信；每段剧本使用新的测试数据库。")}
    write = lambda name, text: (out / name).write_text(text, encoding="utf-8", newline="\n")
    header = [f"# Violet 多轮聊天记录：{label}", "",
              f"- 环境：{env_label}（{summary['notice']}）",
              f"- 代码：{provenance['git_head'] or '未知'}" + ("（有未提交的产品文件改动）" if provenance["uncommitted_product_files"] else ""),
              f"- 剧本：{Path(episodes_path).name}（sha256 {file_sha[:12]}…）；记忆：{memory}；上下文写法：{summary['context_style']}",
              f"- 执行状态：{state}；调用 {used_calls} 次；Token {used_tokens}" + _failure_note(summary), ""]
    write("transcript.md", "\n".join(header) + "\n".join(render_episode(by_id[eid], results.get(eid)) for eid in ids))
    write("debug.json", json.dumps({"summary": summary, "episodes": results}, ensure_ascii=False, indent=1, default=str))
    write("diagnostics.json", json.dumps(diagnose(by_id, {k: v for k, v in results.items()}), ensure_ascii=False, indent=1))
    write("run.json", json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    if key:
        for path in out.rglob("*"):
            if path.is_file() and key.encode() in path.read_bytes():
                raise RuntimeError(f"credential found in {path}")
    return summary


# ------------------------------------------------------------------------------------------------ blind / score

def blind(run_dirs: list[Path], out: Path, seed: int) -> dict:
    """Whole-conversation rating sheets. Every candidate of an episode comes from a run of the same episode
    (same sha256) that completed it; the sheet shows no run label, environment, model or code version."""
    runs = []
    for d in run_dirs:
        summary = upgrade(json.loads((Path(d) / "run.json").read_text(encoding="utf-8")))
        debug = upgrade(json.loads((Path(d) / "debug.json").read_text(encoding="utf-8")))
        runs.append((Path(d), summary, debug["episodes"]))
    if len({s["label"] for _, s, _ in runs}) != len(runs):
        raise ValueError("runs need distinct labels")
    if out.exists():
        raise FileExistsError(f"{out} exists")
    rng = random.Random(seed)
    all_ids = sorted({eid for _, s, _ in runs for eid in s["episodes"]})
    items, skipped, rows = {}, {}, []
    md = ["# Violet 多轮聊天盲评表", "",
          "每一题是同一段剧本的几段完整聊天（候选），来源随机且对你隐藏。请从头读完每段聊天，再按下面的维度打 1–5 分"
          "（intimacy、boundaries 不适用时可填 na），每个候选都要写理由；turn_flags 里可以写“第3步：追问”之类的标记。"
          "rank 为同一题内的排序（1 最好）。问句、称呼、长度等不是评分标准，请按整体聊天感受判断。", "",
          *[f"- {d}：{t}" for d, t in DIMENSIONS.items()], ""]
    order = list(all_ids)
    rng.shuffle(order)
    for eid in order:
        have = [(d, s, eps[eid]) for d, s, eps in runs if eid in eps and eps[eid].get("state") == "complete"]
        shas = {s["episode_sha256"].get(eid) for _, s, _ in runs if eid in s["episodes"]}
        if len(have) != len(runs) or len(shas) != 1:
            skipped[eid] = "not completed by every run" if len(shas) == 1 else "different episode versions"
            continue
        episode = upgrade(json.loads((have[0][0] / "episodes" / f"{eid}.json").read_text(encoding="utf-8")))
        cands = [(s["label"], s["environment"], render_episode(episode, res, anonymized=True)) for _, s, res in have]
        rng.shuffle(cands)
        notes = episode.get("rater_notes") or {}
        md += [f"\n# 题目 {eid}：{episode['title']}", "",
               "- 可以接受：" + "；".join(notes.get("acceptable") or []),
               "- 需要警惕：" + "；".join(notes.get("red_flags") or []),
               *[f"- 第 {k} 步：{v}" for k, v in (notes.get("turn_notes") or {}).items()], ""]
        items[eid] = {"episode_sha256": episode_sha(episode), "candidates": {}}
        for letter, (label, env, text) in zip("ABCDEFG", cands):
            body = text.lstrip("\n").split("\n", 1)[1].lstrip("\n")    # the episode heading is shown once per item
            sha = sha256_bytes(body.encode("utf-8"))
            items[eid]["candidates"][letter] = {"label": label, "environment": env, "sha256": sha}
            md += [f"\n## 候选 {letter}", "", body]
            rows.append({"episode": eid, "candidate": letter, "candidate_sha12": sha[:12], **{d: "" for d in DIMENSIONS},
                         "rank": "", "turn_flags": "", "reason": "", "rater": ""})
    key = {"tool": TOOL_VERSION, "seed": seed, "items": items, "skipped": skipped,
           "runs": {s["label"]: {"dir": str(d), "environment": s["environment"], "state": s["state"],
                                 "git_head": s["provenance"]["git_head"], "memory": s["memory"],
                                 "context_style": s["context_style"]} for d, s, _ in runs}}
    out.mkdir(parents=True)
    with (out / "sheet.csv").open("x", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_FIELDS)
        w.writeheader()
        w.writerows(rows)
    for name, text in (("sheet.md", "\n".join(md) + "\n"), ("key.json", json.dumps(key, ensure_ascii=False, indent=1))):
        with (out / name).open("x", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return key


def _score_value(row: dict, dim: str):
    v = (row.get(dim) or "").strip().lower()
    if v == "na" and dim in NA_ALLOWED:
        return None
    if v not in ("1", "2", "3", "4", "5"):
        raise ValueError(f"{row['episode']}{row['candidate']} {dim}: expected 1-5" + ("/na" if dim in NA_ALLOWED else ""))
    return int(v)


def score(rows: list[dict], key: dict) -> dict:
    per = defaultdict(lambda: defaultdict(list))
    per_episode = defaultdict(lambda: defaultdict(dict))
    ranks, seen, flags, reasons = defaultdict(dict), set(), [], []
    for row in rows:
        eid, letter, rater = row.get("episode"), row.get("candidate"), (row.get("rater") or "").strip() or "unnamed"
        cand = (key["items"].get(eid) or {}).get("candidates", {}).get(letter)
        if cand is None:
            raise ValueError(f"{eid}{letter}: not in key")
        if row.get("candidate_sha12") != cand["sha256"][:12]:
            raise ValueError(f"{eid}{letter}: candidate text changed after blinding")
        if (eid, letter, rater) in seen:
            raise ValueError(f"{eid}{letter}: duplicate row for rater {rater}")
        seen.add((eid, letter, rater))
        if not (row.get("reason") or "").strip():
            raise ValueError(f"{eid}{letter}: reason is required")
        values = {d: _score_value(row, d) for d in DIMENSIONS}
        label = cand["label"]
        for d, v in values.items():
            if v is not None:
                per[label][d].append(v)
        per_episode[eid][label] = values
        if (row.get("turn_flags") or "").strip():
            flags.append({"episode": eid, "label": label, "rater": rater, "flags": row["turn_flags"].strip()})
        reasons.append({"episode": eid, "label": label, "rater": rater, "reason": row["reason"].strip()})
        if (row.get("rank") or "").strip():
            ranks[(eid, rater)][label] = int(row["rank"])
    pairwise = defaultdict(lambda: {"wins": 0, "losses": 0})
    for (eid, rater), r in ranks.items():
        n = len(key["items"][eid]["candidates"])
        if len(r) != n or sorted(r.values()) != list(range(1, n + 1)):
            raise ValueError(f"{eid}: rank every candidate once with 1..{n}")
        names = sorted(r)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                pairwise[f"{a} vs {b}"]["wins" if r[a] < r[b] else "losses"] += 1
    mean = lambda d: {k: {"mean": round(statistics.mean(v), 3), "n": len(v)} for k, v in d.items()}
    return {"tool": TOOL_VERSION, "runs": key["runs"], "by_label": {k: mean(v) for k, v in per.items()},
            "by_episode": {k: dict(v) for k, v in per_episode.items()}, "pairwise_preference": dict(pairwise),
            "turn_flags": flags, "reasons": reasons,
            "notice": "人工整段评分；mock 环境的结果只说明工具可用，不代表聊天质量。"}


# ------------------------------------------------------------------------------------------------ CLI

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate").add_argument("--episodes-file", default=str(EPISODES_PATH))
    r = sub.add_parser("run")
    r.add_argument("--out", required=True); r.add_argument("--label", required=True)
    r.add_argument("--episodes", help="comma-separated ids (default: all)")
    r.add_argument("--episodes-file", default=str(EPISODES_PATH))
    r.add_argument("--real", action="store_true"); r.add_argument("--api-url"); r.add_argument("--config")
    r.add_argument("--context-style", choices=CONTEXT_STYLES, help="VIOLET_CONTEXT_STYLE for this run (default: product default)")
    r.add_argument("--chat-prompt-file", help="CHAT_LLM_PROMPT from this file (its sha256 is recorded)")
    r.add_argument("--memory", choices=MEMORY_MODES, help="real: MemoryAgent decides (real mode default); "
                                                          "scripted: each step's memory_script (mock default)")
    r.add_argument("--max-calls", type=int, default=300); r.add_argument("--max-tokens", type=int, default=2_000_000)
    r.add_argument("--max-calls-per-turn", type=int, default=10)
    b = sub.add_parser("blind")
    b.add_argument("--run", action="append", required=True); b.add_argument("--seed", type=int, required=True)
    b.add_argument("--out", required=True)
    s = sub.add_parser("score")
    s.add_argument("--sheet", required=True); s.add_argument("--key", required=True); s.add_argument("--out", required=True)
    c = sub.add_parser("_child")                                    # internal: launched by run only
    for name in ("--episode", "--workdir", "--result", "--label", "--memory"):
        c.add_argument(name, required=True)
    for name in ("--max-calls", "--max-tokens", "--max-calls-per-turn"):
        c.add_argument(name, type=int, required=True)
    c.add_argument("--real", action="store_true"); c.add_argument("--api-url"); c.add_argument("--config")
    c.add_argument("--confirmation"); c.add_argument("--context-style", choices=CONTEXT_STYLES)
    c.add_argument("--chat-prompt-file")
    args = parser.parse_args(argv)
    if args.cmd == "_child":
        return child_main(args)
    if args.cmd == "validate":
        data, sha = load_episodes(Path(args.episodes_file))
        print(f"ok: {len(data['episodes'])} episodes, sha256 {sha}")
        return 0
    if args.cmd == "run":
        summary = run(Path(args.out), label=args.label,
                      episode_ids=args.episodes.split(",") if args.episodes else None,
                      episodes_path=Path(args.episodes_file), real=args.real, api_url=args.api_url, config=args.config,
                      context_style=args.context_style, chat_prompt_file=args.chat_prompt_file, memory=args.memory,
                      max_calls=args.max_calls, max_tokens=args.max_tokens, max_calls_per_turn=args.max_calls_per_turn)
        print(f"{summary['state']}: {summary['episode_states']}; {summary['used_calls']} calls, "
              f"{summary['used_tokens']} tokens; read {Path(args.out) / 'transcript.md'}")
        return 0 if summary["state"] == "complete" else 1
    if args.cmd == "blind":
        key = blind([Path(x) for x in args.run], Path(args.out), args.seed)
        print(f"{len(key['items'])} episodes, skipped {key['skipped']}; give only sheet.md / sheet.csv to the rater")
        return 0
    with open(args.sheet, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    result = score(rows, json.loads(Path(args.key).read_text(encoding="utf-8")))
    with Path(args.out).open("x", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(result, ensure_ascii=False, indent=1))
    print("written", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
