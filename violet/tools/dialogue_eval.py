"""32-round acceptance dialogue against the real Violet pipeline (phase 7 B.1).

    python -m tools.dialogue_eval                          # mock model: checks the plumbing, costs nothing
    python -m tools.dialogue_eval --real --api-url URL     # real model (same gate and budget as violet_test_chat)
    python -m tools.dialogue_eval --summarize tools/output/<run>/report.json [--scores score_sheet.csv]

    python -m tools.dialogue_eval --replay-from tools/output/<real run>/report.json --rounds 6-10
                                                           # rounds 1-5 rebuilt offline from a real report,
                                                           # then 6-10 live (tools/replay.py)

Options: --rounds 1-10, --scripted-memory (apply the plan's memory actions instead
of the memory model -- isolates the chat model), --no-memory (memory model stores
nothing: what does Violet do without memories?), --manual (you send each planned line
yourself: Enter = as planned, or type your own wording, /skip, /quit),
--manual-score (score each round right after it), --quiet-hours off.

Naturalness is judged by people, not by keywords. The automatic fields are
evidence for the reviewer (questions at the end of replies, Violet's past claims
that have no recorded event, mentions of an ended event, whether the intent
model searched memory when recall was needed, calls / tokens / latency / errors).
"""

from __future__ import annotations

import argparse
import csv
import json

from tools.legacy import upgrade
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from tools.violet_test_chat import budget_from, usage_line
from tools.harness import OUTPUT_DIR, PROJECT, IsolationError, summarize_calls
from tools.replay import (PlanTimeError, ReplaySourceError, check_workdir, load_continuation_replay, load_plan,
                          load_replay_chain, new_state, read_state, round_start, run_continuation_replay, run_replay,
                          scenario_target, write_state)

PLAN_PATH = PROJECT / "tests" / "samples" / "acceptance_dialogue_30.json"
CRITICAL_ROUNDS = {9, 10, 14, 24, 25, 27}        # busy, important moment, fabrication, quiet: a 0 here fails the run
QUESTION_ENDINGS = ("?", "？")


def parse_rounds(spec: str | None, total: int) -> set[int]:
    if not spec:
        return set(range(1, total + 1))
    chosen = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        chosen |= set(range(int(a), int(b or a) + 1))
    return chosen


def build_parser() -> argparse.ArgumentParser:
    from tools.violet_test_chat import build_parser as chat_parser
    p = argparse.ArgumentParser(prog="python -m tools.dialogue_eval", parents=[chat_parser()], add_help=False,
                                description="Violet 2.0 自然对话验收（32 轮，虚构用户）")
    p.add_argument("--plan", type=Path, default=PLAN_PATH)
    p.add_argument("--rounds", help="例如 1-10 或 1,5,9-12")
    p.add_argument("--scripted-memory", action="store_true", help="用计划里的记忆动作代替记忆模型")
    p.add_argument("--manual", action="store_true", help="手动模式：每轮由你确认或改写要发的话")
    p.add_argument("--manual-score", action="store_true", help="每轮结束后立即人工打分")
    p.add_argument("--summarize", type=Path, help="汇总已有报告（不调用任何模型）")
    p.add_argument("--scores", type=Path, help="填好分数的 score_sheet.csv")
    p.add_argument("--replay-from", type=Path, action="append",
                   help="用真实测试报告离线回放所选轮次之前的轮次（不调用模型）；多份报告按顺序重复给出")
    p.add_argument("--recheck", type=Path, help="用当前检查器离线复核已完成的报告（复制数据库副本，不调用模型）")
    p.add_argument("--recheck-db", type=Path, help="复核用的数据库（默认：报告里的测试目录的 violet-test.db，会先复制）")
    p.add_argument("--plan-revision", default="auto",
                   help="剧本修正版：auto（默认，用与剧本哈希匹配的最新 .revN.json）、none 或文件路径")
    p.add_argument("--seed-from", type=Path,
                   help="把一个已保存的测试目录（只读）复制到新的 --workdir 再继续；原目录不会被改动")
    p.add_argument("--continue-replay", type=Path,
                   help="从接着该目录跑的真实报告离线回放紧接着的轮次（需要 --seed-from 和 --continue-replay-sha256）")
    p.add_argument("--continue-replay-sha256", help="--continue-replay 报告的 SHA-256；报告被改动时拒绝回放")
    p.add_argument("--replay-only", action="store_true", help="只做回放、保存状态，不运行 --rounds 里的轮次")
    p.add_argument("--accept-incomplete-memory", action="store_true",
                   help="接受测试目录里没完成的记忆任务，仍在上面续跑（会写进报告）")
    return p


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def _system_text(request: dict[str, Any] | None) -> str:
    if not request:
        return ""
    return "\n".join(m.get("content") or "" for m in request.get("messages") or []
                     if m.get("role") == "system" and isinstance(m.get("content"), str))


def _resolve_memory_steps(session, steps: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if steps is None:
        return None
    resolved = []
    for step in steps:
        args = dict(step["args"])
        if step["tool"] == "update_event_progress" and "memory" in args:
            row = session.db.fetch_one("SELECT id FROM event_memories WHERE event_key = ?", (args.pop("memory"),))
            if row is None:
                continue
            args["memory_id"] = row["id"]
        resolved.append({"tool": step["tool"], "args": args})
    return resolved


# Context checks of the plan are literal strings written for the scripted memory. With a real memory
# model the same knowledge can be stored under other (equally valid) profile keys, so a few checks
# also accept a structured equivalent. Only confirmed, active, usable facts that this round's Chat
# context actually carried count -- the bare word "学校" in the prompt never does.
CONFIRMED_ORIGINS = ("explicit", "dashboard")
STUDENT_RE = re.compile(r"(大学生|本科生|研究生|硕士生|博士生)")
UNIVERSITY_RE = re.compile(r"(大学|学院|university|college)", re.I)
GRADE_RE = re.compile(r"^(大[一二三四五]|研[一二三]|博[一二三四五六]|(本科|大学)[一二三四五]年级)")
KNOWN_KEYS_HEADER = "【已经了解过的方面"


def _confirmed_facts(db) -> list[dict[str, Any]]:
    rows = db.fetch_all("SELECT id, fact_key, value, origin FROM user_profile_facts "
                        "WHERE status = 'active' AND use_in_context = 1")
    return [r for r in rows if r["origin"] in CONFIRMED_ORIGINS]


def _known_keys_line(system_text: str) -> str:
    start = system_text.find(KNOWN_KEYS_HEADER)
    if start < 0:
        return ""
    lines = system_text[start:].split("\n")
    return lines[1] if len(lines) > 1 and lines[1].startswith("- ") else ""


def university_student(db, context: dict[str, Any] | None, system_text: str) -> str | None:
    """How this round's Chat context establishes "the user is a university student", or None."""
    from ai.profile_schema import spec_for
    in_context = set((context or {}).get("fact_ids") or [])
    facts = _confirmed_facts(db)
    if any(f["fact_key"] == "identity.status" and f["id"] in in_context and STUDENT_RE.search(f["value"] or "")
           for f in facts):
        return "identity.status"
    school = [f for f in facts if f["fact_key"] == "study.school" and f["id"] in in_context
              and UNIVERSITY_RE.search(f["value"] or "")]
    grade = [f for f in facts if f["fact_key"] == "study.grade" and GRADE_RE.search((f["value"] or "").strip())]
    grade_visible = any(f["id"] in in_context for f in grade) or (
        bool(grade) and spec_for("study.grade").label in _known_keys_line(system_text))
    if school and grade and grade_visible:
        return "study.school+study.grade"
    return None


# Phase 7 B.4: the location checks of rounds 11 / 13 are decided on the situations this round's Chat
# context actually carried (ContextEngine's situation_ids), never on chat text.
HOME_VALUE_RE = re.compile(r"^(?:在|回到?|到|已回)?(?:自己)?(?:家|家里|家中|老家)(?:里|中)?(?:了)?$")
PLACE_WORDS = ("宿舍", "寝室", "学校", "教室", "图书馆", "实验室", "会议室", "家")

# Phase 7 B.5: location values are free text written by the memory model ("今天在家（回家了）"). They are cut
# into clauses; time words and end particles are dropped; each clause is then classified on its own.
_CLAUSE_SPLIT_RE = re.compile(r"[（）()\[\]【】，,、;；。.!！?？/\s]+")
_LEADING_TIME_RE = re.compile(r"^(?:今天|今晚|现在|此刻|目前|刚刚|刚|已经|已|正在|正|还)+")
_TRAILING_PARTICLE_RE = re.compile(r"(?:呢|啦|呀|啊|哦|嘛|了)+$")
_NOT_HOME_RE = re.compile(r"(?:不在|没在|没回|不回|没有回|离开|出了)(?:自己)?(?:的)?(?:老)?家")
_OTHERS_HOME_RE = re.compile(r"(?:朋友|同学|室友|舍友|亲戚|别人|他|她|男朋友|女朋友|对象|爷爷|奶奶|外公|外婆|姥姥|姥爷|"
                             r"叔叔|阿姨|舅舅|姑姑|姐姐|哥哥|弟弟|妹妹)(?:的)?家")
_NEAR_HOME_RE = re.compile(r"家(?:附近|旁边|楼下|门口|对面|周边|路上)|回家(?:的)?路上")
PLACE_CATEGORIES = (("home", ("家",)), ("dorm", ("宿舍", "寝室")),
                    ("school", ("学校", "教室", "图书馆", "实验室", "校园")),
                    ("work", ("公司", "单位", "办公室", "会议室")),
                    ("out", ("咖啡馆", "书店", "商场", "公园", "路上", "外面", "餐厅", "车上")))


def _clauses(value: str) -> list[str]:
    out = []
    for raw in _CLAUSE_SPLIT_RE.split(value or ""):
        clause = _TRAILING_PARTICLE_RE.sub("", _LEADING_TIME_RE.sub("", raw))
        if clause:
            out.append(clause)
    return out


def classify_location(value: str) -> dict[str, Any]:
    """home / other / ambiguous -- conservative: a clause against home, or home and another place at once,
    is never home. The bare character 家 is not enough (朋友家, 家附近的咖啡馆, 回家路上 are not home)."""
    clauses = _clauses(value)
    home, against, others = [], [], []
    for clause in clauses:
        if _NOT_HOME_RE.search(clause):
            against.append(clause)
        elif _OTHERS_HOME_RE.search(clause) or _NEAR_HOME_RE.search(clause):
            others.append(clause)
        elif HOME_VALUE_RE.match(clause):
            home.append(clause)
        elif any(word in clause for _, words in PLACE_CATEGORIES[1:] for word in words):
            others.append(clause)
    if home and (against or others):
        return {"category": "ambiguous", "clauses": clauses,
                "reason": f"同时有在家和其他线索：{home} / {against + others}"}
    if home:
        return {"category": "home", "clauses": clauses, "reason": f"明确在家：{home}"}
    if against:
        return {"category": "other", "clauses": clauses, "reason": f"明确不在家：{against}"}
    return {"category": "other", "clauses": clauses, "reason": f"不是在家：{others or clauses}"}


def place_categories(text: str) -> list[str]:
    """Place categories a reply names. Home only through an unambiguous home clause, never the bare 家."""
    found = []
    for category, words in PLACE_CATEGORIES[1:]:
        if any(word in (text or "") for word in words):
            found.append(category)
    if "家" in (text or "") and classify_location(text)["category"] == "home" or re.search(
            r"(?<![朋友同学室友舍友亲戚别人他她象不没])(?:在|回到?|到)(?:自己)?家", text or ""):
        found.insert(0, "home")
    return found


def _context_situations(db, context: dict[str, Any] | None) -> list[dict[str, Any]]:
    ids = [int(i) for i in (context or {}).get("situation_ids") or []]
    if not ids:
        return []
    rows = db.fetch_all(f"SELECT * FROM user_situations WHERE id IN ({', '.join('?' for _ in ids)})", ids)
    return sorted(rows, key=lambda r: r["id"])


def _round_of_situation(db, situation: dict[str, Any], ctx: dict[str, Any]) -> int | None:
    """The earlier round whose memory job wrote this situation (via its turn id), or None."""
    job = db.fetch_one("SELECT user_message FROM memory_jobs WHERE turn_id = ?", (situation.get("source_turn_id"),))
    if job is None:
        return None
    rounds = [r["n"] for r in (ctx.get("plan") or {}).get("rounds", [])
              if r["n"] < ctx.get("round", 0) and r["user"] == job["user_message"]]
    return max(rounds) if rounds else None


def location_unknown(db, context, system_text, ctx) -> tuple[str | None, str | None]:
    located = [s for s in _context_situations(db, context) if s["kind"] == "location"]
    if located:
        return None, f"本轮上下文里有当前位置：{located[0]['value']}"
    return "no current location in this round's context", None


def location_home(db, context, system_text, ctx) -> tuple[str | None, str | None]:
    located = [s for s in _context_situations(db, context) if s["kind"] == "location"]
    if not located:
        return None, "位置记忆未写入：本轮上下文里没有当前位置（之前的轮次没有保存，或已经过期）"
    verdicts = {s["id"]: classify_location(s["value"] or "") for s in located}
    home = [s for s in located if verdicts[s["id"]]["category"] == "home"]
    if not home:
        first = located[0]
        return None, f"当前位置不是家（{verdicts[first['id']]['reason']}）：{first['value']}"
    situation = home[0]
    n = _round_of_situation(db, situation, ctx)
    if n is None:
        return None, f"位置“{situation['value']}”找不到是本轮之前哪一轮的记忆整理写入的"
    mode = (ctx.get("modes") or {}).get(n) or {}
    who = ("scripted memory" if mode.get("scripted_memory") else
           {"real": "MemoryAgent (real model)", "mock": "MemoryAgent (mock model)",
            "replay": "replay of a real report"}.get(mode.get("mode"), "memory job"))
    if ctx.get("real") and who != "MemoryAgent (real model)":
        return None, f"位置“{situation['value']}”不是真实 MemoryAgent 写入的（{who}，第 {n} 轮）"
    return f"location {situation['value']!r} written by {who} in round {n}", None


UNCERTAIN_RE = re.compile(r"(不知道|猜不到|猜不出|说不准|不确定|瞎猜|乱猜|猜的|我猜|可能|大概|也许|估计|该不会|是不是)")


def location_evidence(db, context: dict[str, Any] | None, user_text: str, reply: str,
                      before: str | None = None) -> dict[str, Any] | None:
    """Evidence for the reviewer when the user asks where they are -- never a pass/fail by itself.

    context: was a current location given (situation, value, how it classifies); reply: which places Violet named,
    where each named place word could come from (this round's location, profile, earlier chat), whether she
    used the known location, contradicted it, or marked a guess as a guess when nothing was known."""
    if not any(w in user_text for w in ("在哪", "哪儿", "哪里", "什么地方")):
        return None
    located = [s for s in _context_situations(db, context) if s["kind"] == "location"]
    known = None
    if located:
        verdict = classify_location(located[0]["value"] or "")
        named = place_categories(located[0]["value"] or "")
        known = {"situation_id": located[0]["id"], "value": located[0]["value"],
                 "category": verdict["category"] if verdict["category"] != "other" else (named[0] if named else "other"),
                 "reason": verdict["reason"]}
    reply_places = place_categories(reply)
    in_context_facts = set((context or {}).get("fact_ids") or [])
    facts = db.fetch_all("SELECT id, fact_key, value FROM user_profile_facts WHERE status = 'active'")
    history = db.fetch_all("SELECT content FROM messages WHERE created_at < ? ORDER BY id",
                           (before or "9999",)) if before else []
    words = sorted({w for _, ws in PLACE_CATEGORIES for w in ws if w in reply})
    sources = {}
    for word in words:
        where = []
        if located and word in (located[0]["value"] or ""):
            where.append("this round's location situation")
        for f in facts:
            if word in (f["value"] or ""):
                where.append(f"profile {f['fact_key']} ({'in' if f['id'] in in_context_facts else 'not in'} this context)")
        if any(word in (m["content"] or "") for m in history):
            where.append("earlier chat")
        sources[word] = where or ["no source"]
    uncertain = bool(UNCERTAIN_RE.search(reply))
    flags = []
    uses_known = None
    if known is None:
        if reply_places:
            flags.append("unknown_location_guess_marked_uncertain" if uncertain else "unknown_location_named_as_fact")
        elif not uncertain:
            flags.append("unknown_location_no_uncertainty_marker")
    else:
        uses_known = known["category"] in reply_places
        others = [p for p in reply_places if p != known["category"]]
        if others and not uses_known:
            flags.append("known_location_contradicted")
        elif not uses_known:
            flags.append("known_location_not_mentioned")
    return {"context_location": known, "reply_places": reply_places, "place_word_sources": sources,
            "uses_known_location": uses_known, "uncertainty_marked": uncertain, "reply_location_flags": flags}


def _identity(db, context, system_text, ctx) -> tuple[str | None, str | None]:
    matched = university_student(db, context, system_text)
    return matched, None if matched else "没有已确认的大学生身份，也没有已确认的大学名称加年级"


SEMANTIC_CONTEXT_CHECKS: dict[str, Callable[..., tuple[str | None, str | None]]] = {
    "身份：大学生": _identity,
    "此刻在哪里、在做什么：不知道": location_unknown,
    "此刻所在：家里": location_home,
}


# decided on structured data even when the literal line is present (who wrote the location matters)
ALWAYS_STRUCTURED = {"此刻在哪里、在做什么：不知道", "此刻所在：家里"}


def context_checks(db, checks: dict[str, Any], system_text: str, context: dict[str, Any] | None,
                   ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    has, how, failures = {}, {}, {}
    for text in checks.get("context_has", []):
        if text in system_text and text not in ALWAYS_STRUCTURED:
            has[text], how[text] = True, "literal"
            continue
        rule = SEMANTIC_CONTEXT_CHECKS.get(text)
        matched, failure = rule(db, context, system_text, ctx or {}) if rule else (None, "上下文里没有这句话")
        has[text], how[text] = matched is not None, matched
        if matched is None:
            failures[text] = failure
    return {"has": has, "lacks": {t: t not in system_text for t in checks.get("context_lacks", [])},
            "matched_by": how, "failures": failures}


PROACTIVE_CATEGORIES = {
    "not_triggered": "未触发调度", "quiet_hours": "静默时段拦截", "busy": "用户忙碌拦截", "cooldown": "冷却时间拦截",
    "policy": "用户偏好 / 每日上限 / 冷淡期拦截", "declined": "模型决定不发送", "sent": "已发送", "error": "错误",
    "other": "其他", "commitment_reminder": "到期提醒（不是普通主动聊天）",
}
TIME_JUMP_DELAY = "测试时间跳跃导致的延迟"


def classify_proactive(due_reason: str | None, event: dict[str, Any] | None, sent: bool, error: str | None) -> str:
    """Why a proactive tick did or did not send, from the scheduler's own gate reason and event."""
    if error:
        return "error"
    if sent:
        return "sent"
    reason = due_reason or ""
    if reason.startswith("the user said they are busy"):
        return "busy"
    if reason == "quiet hours":
        return "quiet_hours"
    if reason.startswith("too soon after"):
        return "cooldown"
    if reason in ("not due yet", "next proactive time initialized", "no user message yet"):
        return "not_triggered"
    if reason.startswith(("the user asked Violet not to", "daily proactive limit", "waiting for the user")):
        return "policy"
    if reason == "due":
        if event is None:
            return "not_triggered"                       # the tick returned early (a user turn was running)
        if event.get("status") == "skipped":
            return "declined"
        if event.get("status") == "failed":
            return "error"
    return "other"


def _situation_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if row is None else {k: row.get(k) for k in ("id", "kind", "value", "source", "status", "expires_at")}


def reminder_facts(session, due_before: list[dict[str, Any]], events_mark: int,
                   time_advance: dict[str, Any] | None) -> list[dict[str, Any]]:
    """What one scheduler tick did with each reminder that was due before it (phase 7 B.10.1): its
    commitment, the reminder event, the outbox rows and whether the user really got a message about it.
    Sent means: commitment `sent`, its commitment_reminder event `sent`, an outbox row of that event `sent`
    and naming the reminded thing -- never just "some proactive event exists"."""
    from ai.memory_model import mentions_topic, parse_time
    db, now = session.db, session.clock.now()
    events = {e["id"]: e for e in db.fetch_all("SELECT * FROM proactive_events WHERE id > ? ORDER BY id",
                                               (events_mark,))}
    facts = []
    for before in due_before:
        row = db.get_commitment(before["id"]) or {}
        event = next((e for e in events.values() if e["event_type"] == "commitment_reminder"
                      and json.loads(e["decision"] or "{}").get("commitment_id") == before["id"]), None)
        outbox = [dict(o) for o in db.fetch_all("SELECT id, status, content FROM outbound_messages WHERE "
                                                "proactive_event_id = ? ORDER BY id", (event["id"],))] if event else []
        named = [o for o in outbox if o["status"] == "sent" and mentions_topic(o["content"] or "", row.get("content") or "")]
        if row.get("status") == "sent" and event and event["status"] == "sent" and named:
            delivery = "sent"
        elif row.get("status") == "pending" and event is None and session.scheduler._in_quiet_hours(now):
            delivery = "postponed_quiet_hours"
        elif row.get("status") == "sent":
            delivery = "sent_without_matching_message"
        else:
            delivery = "not_sent"
        due = parse_time(before["due_at"])
        late = int((now - due).total_seconds() // 60) if due and now > due else 0
        cause = None
        if late:
            jump_from = parse_time((time_advance or {}).get("from"))
            jump_to = parse_time((time_advance or {}).get("to"))
            if jump_from and jump_to and jump_from < due <= jump_to:
                cause = (f"{TIME_JUMP_DELAY}：场景时钟一次推进了 {time_advance.get('hours')} 小时，测试工具不运行后台调度线程，"
                         f"到期提醒只在这次检查里处理")
            else:
                cause = "到期后测试工具一直没有运行调度检查"
        facts.append({"commitment_id": before["id"], "content": row.get("content"), "due_at": before["due_at"],
                      "status_after": row.get("status"), "delivered_at": row.get("delivered_at"),
                      "proactive_event_id": event["id"] if event else None,
                      "event_status": event["status"] if event else None,
                      "outbox": [{"id": o["id"], "status": o["status"]} for o in outbox],
                      "delivery": delivery, "late_minutes": late, "late_cause": cause})
    return facts


def _proactive(session, what: str, situation_ids_before: set[int] | None = None,
               time_advance: dict[str, Any] | None = None, make_due: bool = True) -> dict[str, Any]:
    if what == "ended_event_not_allowed":
        state = session.scheduler._conversation_state()
        ended = [m["id"] for m in session.db.fetch_all("SELECT id FROM event_memories WHERE progress = 'done'")]
        allowed = [m["id"] for m in state["allowed_memories"]]
        # phase 7 B.6.1: an event recorded as ended while the same event is still open ("进展未知") is a
        # memory failure even when nothing ended may be raised proactively
        contradictions = session.db.ended_event_contradictions()
        return {"check": what, "ended_events": ended, "contradictory_events": contradictions,
                "ok": not set(ended) & set(allowed) and not contradictions}
    busy = None
    if what == "blocked_busy":
        busy = session.scheduler._busy_situation()      # what this round's memory job saved
        session.advance(1.6)                            # past the 90-minute gap, still inside a 2-hour meeting
    error = None
    now = session.clock.now()
    due_before = session.db.list_due_commitments(now.isoformat())
    events_mark = int(session.db.fetch_one("SELECT COALESCE(MAX(id), 0) AS m FROM proactive_events")["m"])
    quiet = session.scheduler._in_quiet_hours(now)
    try:
        result = session.proactive_tick() if make_due else session.proactive_tick(make_due=False)
    except Exception as exc:
        error = exc.__class__.__name__
        result = {"due_reason": None, "event": None, "sent": []}
    reminders = reminder_facts(session, due_before, events_mark, time_advance)
    reminder_events = {r["proactive_event_id"] for r in reminders if r["proactive_event_id"]}
    chat_events = [dict(e) for e in session.db.fetch_all(
        "SELECT id, event_type, status, reason FROM proactive_events WHERE id > ? ORDER BY id", (events_mark,))
        if e["id"] not in reminder_events]
    reminder_texts = [o["content"] for e in reminder_events for o in session.db.fetch_all(
        "SELECT content FROM outbound_messages WHERE proactive_event_id = ? AND status = 'sent'", (e,))]
    chat_sent = [t for t in result["sent"] if t not in reminder_texts]
    category = ("commitment_reminder" if reminder_events and not error else
                classify_proactive(result["due_reason"], chat_events[0] if chat_events else None, bool(chat_sent), error))
    entry = {"check": what, **result, "category": category, "category_label": PROACTIVE_CATEGORIES[category],
             "error": error, "ok": None, "reminders": reminders,
             "chat_proactive": {"events": chat_events, "sent": chat_sent}}
    if what == "allowed_or_skipped_without_bypass":
        failures = []
        if error:
            failures.append(f"调度检查出错：{error}")
        if chat_sent and result["due_reason"] != "due":
            failures.append(f"普通主动消息没有经过调度闸门就发出了（闸门结果：{result['due_reason']}）")
        if quiet and any(r["delivery"].startswith("sent") for r in reminders):
            failures.append("到期提醒在静默时段发出了")
        for r in reminders:
            if r["delivery"] not in ("sent", "postponed_quiet_hours"):
                failures.append(f"到期提醒 #{r['commitment_id']} 没有成功发出（{r['delivery']}，提醒状态 {r['status_after']}）")
        entry.update(failures=failures, ok=not failures, quiet_hours=quiet)
        return entry
    if what == "blocked_busy":
        saved = busy is not None and busy["id"] not in (situation_ids_before or set())
        failures = []
        if not saved:
            failures.append("本轮没有保存忙碌状态（没有新的 availability / activity 忙碌情境）")
        if category != "busy":
            failures.append(f"主动消息不是被忙碌规则拦下，而是：{PROACTIVE_CATEGORIES[category]}")
        if result["sent"]:
            failures.append("忙碌期间给用户发出了主动消息")
        entry.update(busy_saved=saved, busy_situation=_situation_view(busy), failures=failures, ok=not failures)
    return entry


def _busy_cleared(session, plan_round: dict[str, Any]) -> dict[str, Any]:
    """The round after a busy round: the busy state must no longer be current (expired or updated)."""
    current = session.scheduler._busy_situation()
    rows = session.db.fetch_all("SELECT * FROM user_situations WHERE kind IN ('availability', 'activity') ORDER BY id")
    failures = [] if current is None else [f"忙碌状态仍然有效：{current['value']}（到 {current['expires_at'][5:16]}）"]
    return {"check": "busy_cleared", "after_round": plan_round["n"] - 1, "ok": not failures, "failures": failures,
            "situations": [_situation_view(r) for r in rows]}


def _round_follows_busy(plan: dict[str, Any], n: int) -> bool:
    previous = next((r for r in plan["rounds"] if r["n"] == n - 1), None)
    if previous is None:
        return False
    checks = list((previous.get("before") or {}).values()) + list((previous.get("after") or {}).values())
    return "blocked_busy" in checks


def _chat_context_of_round(session, result: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """System text and context report of THIS round's reply (never a later proactive run's)."""
    if not any(c["agent"] == "ChatAgent" for c in result["calls"]):
        return "", None
    chat_runs = [r for r in result.get("runs") or [] if r["agent"] == "ChatAgent" and r.get("context")]
    return _system_text(session.recorder.last_request.get("ChatAgent")), (chat_runs[-1]["context"] if chat_runs else None)


def _auto_fields(session, rnd: dict[str, Any], result: dict[str, Any], ended_contents: list[str],
                 chat_context: tuple[str, dict[str, Any] | None] | None = None,
                 ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    from ai.consistency import ConsistencyGuard
    from ai.memory_model import mentions_topic
    violet = result["violet"]
    joined = "\n".join(violet)
    by_agent: dict[str, int] = {}
    for call in result["calls"]:
        by_agent[call["agent"]] = by_agent.get(call["agent"], 0) + 1
    intent_tools = [t for c in result["calls"] if c["agent"] == "IntentAgent" for t in c["tool_calls"]]
    checks = rnd.get("checks") or {}
    system, report = chat_context if chat_context is not None else _chat_context_of_round(session, result)
    context = context_checks(session.db, checks, system, report, {**(ctx or {}), "round": rnd["n"]})
    try:
        flags = [f"{v.check}: {v.line}" for v in ConsistencyGuard(session.db).check_reply(joined)]
    except Exception as exc:                            # evidence only; never break the run
        flags = [f"guard error: {exc.__class__.__name__}"]
    return {
        "ends_with_question": bool(violet) and violet[-1].rstrip().endswith(QUESTION_ENDINGS),
        "question_lines": sum(line.rstrip().endswith(QUESTION_ENDINGS) for line in violet),
        "consistency_flags": flags,
        "mentions_ended_event": [c for c in ended_contents if joined and mentions_topic(joined, c)
                                 and not mentions_topic(rnd["user"], c)],
        "recall_needed": bool(rnd.get("intent_query")),
        # evidence for the reviewer only: which places Violet named when asked where the user is
        "places_in_reply": [w for w in PLACE_WORDS if w in joined] if "在哪" in rnd["user"] else [],
        "location": location_evidence(session.db, report, rnd["user"], joined,
                                      (ctx or {}).get("scenario_time")),
        "intent_searched_memory": "query_memory" in intent_tools,
        "calls_by_agent": by_agent,
        "context_checks": context,
        "feature_off": ("image" if rnd.get("image") and not getattr(session, "image_mock", False)
                        else ("web" if "violet-web__web_search" in str(checks) else None)),
    }


# ---------------------------------------------------------------------------
# Pictures (phase 7 B.8.1): three separate facts, never inferred from one another
# ---------------------------------------------------------------------------
# A line that says a picture is being sent / shown / was drawn for the user. Negated lines ("发不了图",
# "没法画") do not count. Evidence for the reviewer and a check when no picture was actually sent.
PICTURE_CLAIM_RE = re.compile(r"(给你(?:画|发|拍|做)(?:了|好了)|画好了|发给你了|发你了|发过去了|传给你了|附上|如图|"
                              r"看(?:看)?这张|这张(?:图|画|插画|照片)|就是这张|给你看(?:看)?(?:这|我))")
PICTURE_NEGATED_RE = re.compile(r"(没|不能|不了|发不|画不|没法|无法|暂时|不会|做不到)")


def picture_facts(images: dict[str, Any] | None, calls: list[dict[str, Any]]) -> dict[str, Any]:
    """tool_called / generated / sent_recorded, each from its own evidence."""
    images = images or {}
    tool_called = any("create_image" in (c.get("tool_calls") or []) for c in calls if c.get("agent") == "ChatAgent")
    generated = [a for a in images.get("assets") or []
                 if a["direction"] == "outbound" and a["ai_generated"] and a["status"] == "ready"
                 and any(t["status"] == "succeeded" and a["asset_id"] in t["output_asset_ids"]
                         for t in images.get("tasks") or [] if t["kind"] == "image_generate")]
    sent_rows = {o["asset"] for o in images.get("outbound") or [] if o["status"] == "sent"}
    recorded = [x for x in images.get("sent") or [] if x.get("recorded")]
    sent_recorded = [a for a in generated if f"asset:{a['asset_id']}" in sent_rows
                     and any(x.get("sha256") == a["sha256"] for x in recorded)]
    providers = sorted({a["provider_id"] for a in generated if a.get("provider_id")})
    return {
        "tool_called": tool_called,
        "generated": bool(generated), "generated_asset_ids": [a["asset_id"] for a in generated],
        "violet_scene": any(a.get("scene_kind") for a in generated),
        "sent_recorded": bool(sent_recorded), "sent_asset_ids": [a["asset_id"] for a in sent_recorded],
        "provider": images.get("provider", "off"), "asset_providers": providers,
        "task_status": [t["status"] for t in images.get("tasks") or []],
        "task_errors": [t["error_code"] for t in images.get("tasks") or [] if t.get("error_code")],
        "non_mock_calls": [c["entry_id"] for c in images.get("calls") or [] if not str(c["entry_id"]).startswith("mock/")],
        "send_failures": [x.get("error") for x in images.get("sent") or [] if not x.get("recorded")]
                         + [o["asset"] for o in images.get("outbound") or [] if o["status"] == "failed"],
    }


def picture_premise(db) -> dict[str, Any]:
    """Did Violet actually send an AI picture before this round? (The basis of "这张是你拍的吗".)"""
    row = db.fetch_one("SELECT id, provider_id, model_id, scene_kind, sent_at FROM media_assets WHERE direction = 'outbound' "
                       "AND ai_generated = 1 AND send_status = 'sent' ORDER BY id DESC LIMIT 1")
    if row is None:
        return {"required": True, "met": False, "reason": "no AI picture was sent before this round"}
    return {"required": True, "met": True, "asset_id": row["id"], "provider_id": row["provider_id"],
            "scene_kind": row["scene_kind"]}


def picture_reply_flags(violet: list[str], facts: dict[str, Any]) -> list[str]:
    from ai.consistency import CAMERA_CLAIM_RE, CAMERA_NEGATED_RE
    flags = []
    for line in violet:
        if CAMERA_CLAIM_RE.search(line) and not CAMERA_NEGATED_RE.search(line):
            flags.append("claims_photo_or_camera")                 # an AI (mock) picture told as a photo
        if (not facts["sent_recorded"] and PICTURE_CLAIM_RE.search(line)
                and not PICTURE_NEGATED_RE.search(line)):
            flags.append("claims_picture_without_one_sent")        # failed / no picture, said as sent or shown
    return sorted(set(flags))


# ---------------------------------------------------------------------------
# Plan checks by category (phase 7 B.9.1): tools_has / tools_lacks / db
# ---------------------------------------------------------------------------
# PASS / FAIL as usual; NOT_APPLICABLE when the feature is off in this run (untested -- never a pass);
# UNSUPPORTED when the tool cannot tell (an unknown tool or db check -- reported, never silently skipped).
PASS, FAIL, NOT_APPLICABLE, UNSUPPORTED = "PASS", "FAIL", "NOT_APPLICABLE", "UNSUPPORTED"
IMAGE_TOOL_NAMES = {"create_image", "edit_image", "look_at_image"}
OFF_REASON = {"image": "图片功能在本次运行中关闭（没有加 --image-mock）：未测试",
              "web": "测试工具没有接入网页工具（web MCP 关闭）：网页搜索未测试"}


def tool_category(name: str) -> str:
    if name in IMAGE_TOOL_NAMES:
        return "image"
    if name.startswith("violet-web__") or name in ("web_search", "fetch_url"):
        return "web"
    return "other"


def run_features(session) -> dict[str, bool]:
    import os
    servers = os.environ.get("MCP_SERVERS") or ""
    return {"image": bool(getattr(session, "image_mock", False)),
            "web": os.environ.get("VIOLET_WEB_ENABLED") == "1" and "violet-web" in servers}


def _chat_tools_called(calls: list[dict[str, Any]]) -> set[str]:
    return {t for c in calls if c.get("agent") == "ChatAgent" for t in (c.get("tool_calls") or [])}


def expected_due(spec: dict[str, Any], scenario_time: str):
    """The due time a create_commitment step of the plan asks for, in Violet's time zone, relative to the
    round's scenario date. None when the plan's wording is not understood (reported as UNSUPPORTED)."""
    from datetime import date, timedelta
    from ai.settings import local_timezone
    tz = local_timezone()
    if spec.get("due_time"):
        try:
            return datetime.fromisoformat(spec["due_time"]).astimezone(tz)
        except ValueError:
            return None
    base = datetime.fromisoformat(scenario_time).astimezone(tz).date()
    hint = str(spec.get("due_date") or "").strip()
    offsets = {"今天": 0, "today": 0, "明天": 1, "tomorrow": 1, "后天": 2, "day_after_tomorrow": 2}
    if hint in offsets:
        day = base + timedelta(days=offsets[hint])
    else:
        try:
            day = date.fromisoformat(hint)
        except ValueError:
            return None
    clock = str(spec.get("due_clock") or "")
    if not re.fullmatch(r"\d{1,2}:\d{2}", clock):
        return None
    hour, minute = (int(x) for x in clock.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


def commitment_check(db, rnd: dict[str, Any], since_id: int, user_text: str, scenario_time: str) -> dict[str, Any]:
    """db: commitment_created -- a reminder created IN THIS ROUND (id > since_id), pending, about the planned
    content, with the user's words as evidence (the product's own rule) and due at the planned time."""
    from ai.memory_model import check_explicit_request, mentions_topic, parse_time
    spec = next((dict(step.get("args") or {}) for step in rnd.get("memory") or []
                 if step.get("tool") == "create_commitment"), None)
    if spec is None or not spec.get("content"):
        return {"status": UNSUPPORTED, "reason": "剧本没有给出期望的提醒（create_commitment 参数）"}
    due = expected_due(spec, scenario_time)
    if due is None:
        return {"status": UNSUPPORTED, "reason": f"看不懂剧本的提醒时间：{spec.get('due_date')} {spec.get('due_clock')}"}
    rows = db.fetch_all("SELECT * FROM commitments WHERE id > ? ORDER BY id", (since_id,))
    if not rows:
        return {"status": FAIL, "reason": "这一轮没有新建提醒", "expected_due": due.isoformat()}
    best = None
    for row in rows:
        problems = []
        if row["status"] != "pending":
            problems.append(f"状态是 {row['status']}，不是 pending")
        if not mentions_topic(row["content"] or "", spec["content"]):
            problems.append("提醒内容和剧本的事情对不上")
        ok, why = check_explicit_request(row["evidence"], user_text)
        if not ok:
            problems.append(f"证据不是用户原话里的明确请求（{why}）")
        stored = parse_time(row["due_at"])
        if stored is None or stored != due:
            problems.append(f"时间是 {row['due_at']}，应为 {due.isoformat()}")
        if not problems:
            return {"status": PASS, "reason": "", "commitment_id": row["id"], "due_at": row["due_at"]}
        if best is None or len(problems) < len(best[1]):
            best = (row, problems)
    return {"status": FAIL, "reason": "；".join(best[1]), "commitment_id": best[0]["id"],
            "expected_due": due.isoformat()}


def plan_checks(rnd: dict[str, Any], features: dict[str, bool], chat_tools: set[str], facts: dict[str, Any],
                commitment: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    checks = rnd.get("checks") or {}
    out: list[dict[str, Any]] = []

    def add(check: str, category: str, status: str, reason: str = "") -> None:
        out.append({"check": check, "category": category, "status": status, "reason": reason})
    for tool in checks.get("tools_has") or []:
        category = tool_category(tool)
        if category == "other":
            add(f"tools_has:{tool}", category, UNSUPPORTED, "测试工具不知道这个工具在本次运行中是否可用")
        elif not features.get(category):
            add(f"tools_has:{tool}", category, NOT_APPLICABLE, OFF_REASON[category])
        elif tool in chat_tools:
            add(f"tools_has:{tool}", category, PASS)
        else:
            add(f"tools_has:{tool}", category, FAIL, "模型这一轮没有调用这个工具")
    for tool in checks.get("tools_lacks") or []:
        category = tool_category(tool)
        if category == "other":
            add(f"tools_lacks:{tool}", category, UNSUPPORTED, "测试工具不知道这个工具在本次运行中是否可用")
        elif not features.get(category):
            add(f"tools_lacks:{tool}", category, NOT_APPLICABLE, OFF_REASON[category] + "（工具本来就不可用，这项检查没有意义）")
        elif tool in chat_tools:
            add(f"tools_lacks:{tool}", category, FAIL, "模型这一轮不该调用却调用了")
        else:
            add(f"tools_lacks:{tool}", category, PASS)
    db_check = checks.get("db")
    if db_check == "violet_picture_sent":
        if not features.get("image"):
            add("db:violet_picture_sent", "image", NOT_APPLICABLE, OFF_REASON["image"])
        elif facts["tool_called"] and facts["generated"] and facts["violet_scene"] and facts["sent_recorded"]:
            add("db:violet_picture_sent", "image", PASS)
        else:
            missing = [name for name, have in (("调用 create_image", facts["tool_called"]),
                                               ("Mock 生成成功", facts["generated"]),
                                               ("画的是 Violet 的场景", facts["violet_scene"]),
                                               ("测试发送器记录了这张图", facts["sent_recorded"])) if not have]
            add("db:violet_picture_sent", "image", FAIL, "缺少：" + "、".join(missing))
    elif db_check == "commitment_created":
        result = commitment or {"status": UNSUPPORTED, "reason": "这一轮没有做提醒检查"}
        add("db:commitment_created", "memory", result["status"], result.get("reason", ""))
    elif db_check:
        add(f"db:{db_check}", "other", UNSUPPORTED, "测试工具没有实现这项数据库检查")
    return out


def picture_checks(rnd: dict[str, Any], facts: dict[str, Any], premise: dict[str, Any] | None,
                   flags: list[str], features: dict[str, bool] | None = None,
                   checks: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The picture part of a round's checks (image category only; other categories never land here)."""
    features = features if features is not None else {"image": True, "web": False}
    if checks is None:
        checks = plan_checks(rnd, features, {"create_image"} if facts["tool_called"] else set(), facts)
    image = [c for c in checks if c["category"] == "image"]
    if not image and premise is None and not flags and not facts["non_mock_calls"]:
        return None
    results = {c["check"]: c["status"] == PASS for c in image if c["status"] in (PASS, FAIL)}
    failures = {c["check"]: c["reason"] for c in image if c["status"] == FAIL}
    not_applicable = {c["check"]: c["reason"] for c in image if c["status"] == NOT_APPLICABLE}
    if facts["non_mock_calls"]:
        failures["image_provider"] = f"出现了非 Mock 的图片调用：{facts['non_mock_calls']}"
    for flag in flags:
        failures[f"reply:{flag}"] = {"claims_photo_or_camera": "把 AI 图片说成照片 / 拍的",
                                     "claims_picture_without_one_sent": "没有图片发出却说发了 / 给你看"}[flag]
    valid: bool | None = True
    if premise is not None:
        if not features.get("image"):
            valid = None                                               # untested, not invalid and not passed
            not_applicable["premise"] = OFF_REASON["image"] + "（上一轮不可能发图，这一轮的图片真实性验收未测试）"
        elif not premise["met"]:
            valid = False                                              # the question about "this picture" has no basis
            failures["premise"] = "上一轮没有真的发出 AI 图片，这一轮的图片真实性验收无效"
    if not features.get("image") and not results and not failures:
        valid = None
    status = "not_applicable" if valid is None else ("invalid" if valid is False else "evaluated")
    return {"results": results, "failures": failures, "not_applicable": not_applicable, "premise": premise,
            "valid": valid, "status": status,
            "facts": {k: facts[k] for k in ("tool_called", "generated", "violet_scene", "sent_recorded", "provider")}}


def preload(session, plan: dict[str, Any]) -> None:
    """Violet's recorded life before round 1 (fictional): one active thread and one happened event."""
    from datetime import timedelta
    from ai.life_store import LifeStore
    from ai.memory_model import parse_time
    spec = plan.get("preload") or {}
    if not spec.get("life_thread"):
        return
    store = LifeStore(session.db)
    now = parse_time(session.db.now())
    title = spec["life_thread"]
    thread_id = store.create_thread(f"eval:{title}", "reading", title, title.strip("读《》"),
                                    (now - timedelta(days=5)).isoformat(), source="dialogue_eval")
    event = spec.get("life_event")
    if event and thread_id:
        store.insert_event(uid=f"eval:{event['content']}", thread_id=thread_id, plan_id=None, event_type="progress",
                           content=event["content"],
                           occurred_at=(now - timedelta(hours=float(event.get("hours_ago", 20)))).isoformat(),
                           duration_minutes=60, source="dialogue_eval")


def run_plan(session, plan: dict[str, Any], rounds: set[int], scripted_memory: bool,
             score_fn: Callable[[dict[str, Any]], tuple[int | None, str]] | None = None,
             out: Callable[[str], None] = print, records: list[dict[str, Any]] | None = None,
             manual_fn: Callable[[dict[str, Any]], str | None] | None = None,
             on_round_done: Callable[[int, dict[str, Any]], None] | None = None,
             env: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run the plan; finished rounds are appended to `records` as they complete (kept on Ctrl+C).

    env: times (round -> scenario start of earlier rounds), modes (round -> mode / scripted_memory),
    mode ("real" / "mock") of this run -- used for plan times and for where a memory came from."""
    records = [] if records is None else records
    env = env or {}
    times: dict[int, str] = dict(env.get("times") or {})
    modes: dict[int, dict[str, Any]] = dict(env.get("modes") or {})
    run_mode = env.get("mode") or ("real" if session.real else "mock")
    preload(session, plan)
    for rnd in plan["rounds"]:
        if rnd["n"] not in rounds:
            continue
        if session.recorder.stopped:
            out(f"预算已用完，停止于第 {rnd['n']} 轮：{session.recorder.stopped}")
            break
        user_text = rnd["user"]
        if manual_fn is not None:
            typed = manual_fn(rnd)
            if typed is None:
                continue
            user_text = typed or rnd["user"]
        time_advance = None
        if rnd.get("time"):
            from ai.settings import local_timezone
            spec = rnd["time"]
            target = scenario_target(spec, round_start(session.db, plan, int(spec["base_round"]), times),
                                     local_timezone())
            if target < session.clock.now():
                raise PlanTimeError(f"第 {rnd['n']} 轮的剧本时间 {target.isoformat()} 早于当前场景时间")
            time_advance = session.advance_to(target)
        elif rnd.get("advance_hours"):
            time_advance = session.advance(rnd["advance_hours"])
        times[rnd["n"]] = session.clock.now().isoformat()
        before = [_proactive(session, v, time_advance=time_advance) for k, v in (rnd.get("before") or {}).items()]
        situation_ids = {r["id"] for r in session.db.fetch_all("SELECT id FROM user_situations")}
        previous = next((r for r in plan["rounds"] if r["n"] == rnd["n"] - 1), None)
        premise = picture_premise(session.db) if previous and previous.get("image") else None
        commitment_mark = int(session.db.fetch_one("SELECT COALESCE(MAX(id), 0) AS m FROM commitments")["m"])
        session.memory_script = _resolve_memory_steps(session, rnd.get("memory") or []) if scripted_memory else None
        result = session.say(user_text, rnd["n"])
        session.memory_script = None
        chat_context = _chat_context_of_round(session, result)          # before a proactive run replaces it
        after = [_proactive(session, v, situation_ids) for k, v in (rnd.get("after") or {}).items()]
        if _round_follows_busy(plan, rnd["n"]):
            after.append(_busy_cleared(session, rnd))
        ended = [m["content"] for m in session.db.fetch_all("SELECT content FROM event_memories WHERE progress = 'done'")]
        facts = picture_facts(result.get("images"), result["calls"])
        features = run_features(session)
        commitment = (commitment_check(session.db, rnd, commitment_mark, user_text, times[rnd["n"]])
                      if (rnd.get("checks") or {}).get("db") == "commitment_created" else None)
        checks_done = plan_checks(rnd, features, _chat_tools_called(result["calls"]), facts, commitment)
        image_checks = picture_checks(rnd, facts, premise, picture_reply_flags(result["violet"], facts), features,
                                      checks_done)
        record = {"n": rnd["n"], "user": user_text, "planned_user": rnd["user"], "violet": result["violet"],
                  "rubric": rnd.get("rubric", ""),
                  "reply_latency_ms": result["reply_latency_ms"], "calls": len(result["calls"]),
                  "tokens": result["tokens"], "errors": result["errors"], "proactive": before + after,
                  "auto": _auto_fields(session, rnd, result, ended, chat_context,
                                       {"plan": plan, "modes": modes, "real": session.real,
                                        "scenario_time": times[rnd["n"]]}),
                  "score": None, "note": "", "scenario_time": times[rnd["n"]], "time_advance": time_advance,
                  "call_details": result["calls"], "runs": result["runs"],
                  "images": result.get("images"), "image_checks": image_checks, "checks": checks_done,
                  "commitment": commitment, "memory_jobs": result.get("memory_jobs", []),
                  "background_complete": result.get("background_complete", True),
                  "execution_errors": result.get("execution_errors", [])}
        modes[rnd["n"]] = {"mode": run_mode, "scripted_memory": bool(scripted_memory)}
        out(f"[{rnd['n']:>2}] 你: {user_text}")
        for line in result["violet"] or ["（没有回复）"]:
            out(f"     Violet: {line}")
        for problem in record["execution_errors"]:
            out(f"     ⚠ 本轮没有完整执行：{execution_error_text(problem)}")
        if score_fn is not None:
            record["score"], record["note"] = score_fn(record)
        records.append(record)
        if on_round_done is not None:
            on_round_done(rnd["n"], record)
    return records


def write_outputs(records: list[dict[str, Any]], plan: dict[str, Any], session, args, out_dir: Path,
                  final_state: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> Path:
    from tools.harness import write_report
    report = {"created_at": datetime.now().isoformat(timespec="seconds"), "real": session.real,
              "models": {k: v for k, v in _models().items()}, "options": {
                  "scripted_memory": args.scripted_memory, "no_memory": args.no_memory, "rounds": args.rounds,
                  "image_mock": bool(getattr(args, "image_mock", False))},
              "image_provider": "mock" if getattr(session, "image_mock", False) else "off",
              "network_guard": session.network_guard.summary() if getattr(session, "network_guard", None) else None,
              "usage": usage_line(session.recorder), "budget": session.recorder.budget_state(),
              "global_criteria": plan.get("global_criteria", {}),
              "call_summary": summarize_calls(session.recorder.calls, session.recorder.runs),
              "rounds": records, "final_state": final_state if final_state is not None else session.state(),
              **(extra or {})}
    path = write_report("report.json", report, session.recorder, out_dir)
    with open(out_dir / "score_sheet.csv", "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["round", "user", "violet", "rubric", "auto_flags", "score(0/1/2)", "note"])
        for r in records:
            flags = []
            if r["auto"]["ends_with_question"]:
                flags.append("以问句结尾")
            if r["auto"]["consistency_flags"]:
                flags.append("一致性：" + "; ".join(r["auto"]["consistency_flags"]))
            if r["auto"]["mentions_ended_event"]:
                flags.append("提到已结束的事")
            if r["auto"]["recall_needed"] and not r["auto"]["intent_searched_memory"]:
                flags.append("需要回忆但没有搜索记忆")
            if r["errors"]:
                flags.append(f"API 错误 {len(r['errors'])}")
            for problem in r.get("execution_errors") or []:
                flags.append("执行错误：" + execution_error_text(problem))
            for text, reason in (r["auto"]["context_checks"].get("failures") or {}).items():
                flags.append(f"上下文检查“{text}”未通过：{reason}")
            if r["auto"].get("places_in_reply"):
                flags.append("回复提到地点：" + "、".join(r["auto"]["places_in_reply"]))
            for flag in ((r["auto"].get("location") or {}).get("reply_location_flags") or []):
                flags.append(f"位置证据：{flag}（供人工评分参考）")
            for p in r["proactive"]:
                if p.get("ok") is False:
                    flags.append(f"{p['check']} 未通过：" + "；".join(p.get("failures") or []))
            for name, reason in ((r.get("image_checks") or {}).get("failures") or {}).items():
                flags.append(f"图片检查 {name}：{reason}")
            for c in r.get("checks") or []:
                if c["status"] == FAIL and c["category"] != "image":
                    flags.append(f"检查 {c['check']} 未通过：{c['reason']}")
                elif c["status"] in (NOT_APPLICABLE, UNSUPPORTED):
                    flags.append(f"未测试 {c['check']}（{c['status']}）：{c['reason']}")
            writer.writerow([r["n"], session.recorder.scrub(r["user"]), session.recorder.scrub(" / ".join(r["violet"])),
                             r["rubric"], " | ".join(flags), "" if r["score"] is None else r["score"], r["note"]])
    return path


def _models() -> dict[str, str]:
    import os
    from tools.violet_test_chat import MODEL_KEYS
    return {role: os.environ.get(key, "?") for role, key in MODEL_KEYS.items()}


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def summarize(report: dict[str, Any], scores: dict[int, tuple[int | None, str]] | None = None) -> dict[str, Any]:
    from tools.replay import require_continuous_scope
    require_continuous_scope(report)
    rounds = report["rounds"]
    for r in rounds:
        if scores and r["n"] in scores:
            r["score"], r["note"] = scores[r["n"]]
    scored = [r for r in rounds if r.get("score") is not None]
    total, maximum = sum(r["score"] for r in scored), 2 * len(rounds)
    replies = [r for r in rounds if r["violet"]]
    question_ratio = round(sum(r["auto"]["ends_with_question"] for r in replies) / len(replies), 3) if replies else 0.0
    streak = best = 0
    for r in rounds:
        streak = streak + 1 if r["auto"]["ends_with_question"] else 0
        best = max(best, streak)
    recall = [r for r in rounds if r["auto"]["recall_needed"]]
    calls = sum(r["calls"] for r in rounds)
    errors = sum(len(r["errors"]) for r in rounds)
    critical_zero = [r["n"] for r in scored if r["n"] in CRITICAL_ROUNDS and r["score"] == 0]
    busy_sent = [p for r in rounds for p in r["proactive"] if p.get("check") == "blocked_busy" and p.get("sent")]
    failed_checks = [{"round": r["n"], "check": p.get("check"), "failures": p.get("failures") or []}
                     for r in rounds for p in r["proactive"] if p.get("ok") is False]
    context_failures = [{"round": r["n"], "check": text, "reason": reason} for r in rounds
                        for text, reason in ((r["auto"].get("context_checks") or {}).get("failures") or {}).items()]
    reply_location_flags = [{"round": r["n"], "flags": (r["auto"].get("location") or {}).get("reply_location_flags")}
                            for r in rounds if (r["auto"].get("location") or {}).get("reply_location_flags")]
    image_failures = [{"round": r["n"], "check": name, "reason": reason} for r in rounds
                      for name, reason in ((r.get("image_checks") or {}).get("failures") or {}).items()]
    check_failures = [{"round": r["n"], **c} for r in rounds for c in r.get("checks") or []
                      if c["status"] == FAIL and c["category"] != "image"]
    untested = [{"round": r["n"], **c} for r in rounds for c in r.get("checks") or []
                if c["status"] in (NOT_APPLICABLE, UNSUPPORTED)]
    exec_errors = [{"round": r["n"], **e} for r in rounds for e in r.get("execution_errors") or []]
    untested += [{"round": r["n"], "check": "premise", "category": "image", "status": NOT_APPLICABLE, "reason": reason}
                 for r in rounds for name, reason in ((r.get("image_checks") or {}).get("not_applicable") or {}).items()
                 if name == "premise"]
    result = {
        "rounds": len(rounds), "scored": len(scored), "score": total, "max_score": maximum,
        "question_ending_ratio": question_ratio, "longest_question_streak": best,
        "consistency_flag_rounds": [r["n"] for r in rounds if r["auto"]["consistency_flags"]],
        "ended_event_mentions": [r["n"] for r in rounds if r["auto"]["mentions_ended_event"]],
        "recall_rounds": len(recall), "recall_searched": sum(r["auto"]["intent_searched_memory"] for r in recall),
        "proactive_sent_while_busy": len(busy_sent),
        "calls_total": calls, "calls_per_round": round(calls / len(rounds), 2) if rounds else 0,
        "tokens_total": sum(r["tokens"] for r in rounds),
        "avg_reply_latency_ms": int(sum(r["reply_latency_ms"] for r in rounds) / len(rounds)) if rounds else 0,
        "api_error_rate": round(errors / calls, 3) if calls else 0.0,
        "critical_rounds_with_zero": critical_zero,
        "failed_checks": failed_checks,
        "context_check_failures": context_failures,
        "reply_location_flags": reply_location_flags,           # evidence only: not part of the verdict
        "image_check_failures": image_failures,
        "check_failures": check_failures,
        "untested_checks": untested,
        "image_rounds_not_applicable": [r["n"] for r in rounds if (r.get("image_checks") or {}).get("valid") is None
                                        and r.get("image_checks")],
        "image_rounds_invalid": [r["n"] for r in rounds if (r.get("image_checks") or {}).get("valid") is False],
        "execution_errors": exec_errors,
        "rounds_with_unfinished_background_work": [r["n"] for r in rounds if r.get("background_complete") is False],
    }
    complete = len(scored) == len(rounds)
    result["verdict"] = ("incomplete: score every round first" if not complete else
                         "pass" if total >= round(maximum * 54 / 64) and not critical_zero and not busy_sent
                         and not failed_checks and not image_failures and not check_failures
                         and question_ratio <= 0.4 and best < 3 else "fail")
    if result["verdict"] == "pass" and untested:
        result["verdict"] = "pass_with_untested_checks"          # NOT_APPLICABLE / UNSUPPORTED are never a pass
    if exec_errors and result["verdict"].startswith("pass"):
        result["verdict"] = "fail_execution_incomplete"         # zero API errors is not a complete round
    return result


def execution_error_text(problem: dict[str, Any]) -> str:
    if problem["kind"] == "memory_job":
        return (f"记忆任务 #{problem['job_id']} 状态 {problem['status']}（尝试 {problem['attempts']} 次，"
                f"{problem.get('error_type') or '无错误类型'}）")
    if problem["kind"] == "budget_refused":
        return f"预算拒绝了 {problem['agent']} 的调用（没有发出）：{problem['reason']}"
    return f"用户消息处理状态 {problem['status']}"


def load_scores(path: Path) -> dict[int, tuple[int | None, str]]:
    from tools.replay import SPECIALIZED_PURPOSES
    scores = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("purpose") in SPECIALIZED_PURPOSES or row.get("synthetic", "").lower() == "true":
                raise ValueError("合成专项评分不得计入原32轮总分")
            raw = (row.get("score(0/1/2)") or "").strip()
            if raw:
                if raw not in ("0", "1", "2"):
                    raise ValueError(f"round {row['round']}: score must be 0, 1 or 2")
                scores[int(row["round"])] = (int(raw), row.get("note") or "")
    return scores


LOCATION_CHECKS = ("此刻在哪里、在做什么：不知道", "此刻所在：家里")


def recheck_report(report_path: Path, db_source: Path | None = None, out: Callable[[str], None] = print) -> dict[str, Any]:
    """Re-run today's location checks and reply evidence on a finished report, offline.

    The database is COPIED into a fresh test directory first; the original test directory is only read."""
    import shutil
    from tools.harness import cleanup, prepare_environment
    from tools.replay import read_state
    report = upgrade(json.loads(Path(report_path).read_text(encoding="utf-8")))
    from tools.replay import require_continuous_scope
    require_continuous_scope(report)
    workdir = Path((report.get("continuation") or {}).get("workdir") or "")
    source = Path(db_source) if db_source else workdir / "violet-test.db"
    if not source.exists():
        raise FileNotFoundError(f"找不到复核用的数据库：{source}")
    temp = prepare_environment(None, environ={**os.environ})
    shutil.copy(source, temp / "recheck.db")
    from database import VioletDatabase
    db = VioletDatabase(temp / "recheck.db")
    try:
        plan, _ = load_plan(Path(report.get("plan", {}).get("plan") or PLAN_PATH))
        state = read_state(workdir) if workdir.exists() else None
        modes = {r["n"]: {"mode": r.get("mode"), "scripted_memory": bool(r.get("scripted_memory"))}
                 for r in (state or {}).get("rounds", [])}
        plan_rounds = {r["n"]: r for r in plan["rounds"]}
        rounds = []
        for record in report["rounds"]:
            rnd = plan_rounds.get(record["n"], {})
            chat = [r for r in record.get("runs") or [] if r.get("agent") == "ChatAgent" and r.get("context")]
            context = chat[-1]["context"] if chat else None
            checks = {"context_has": [t for t in (rnd.get("checks") or {}).get("context_has", []) if t in LOCATION_CHECKS]}
            ctx = {"plan": plan, "round": record["n"], "modes": modes, "real": bool(report.get("real"))}
            rechecked = context_checks(db, checks, "", context, ctx) if checks["context_has"] else None
            evidence = location_evidence(db, context, record["user"], "\n".join(record["violet"]), record.get("scenario_time"))
            before = (record.get("auto") or {}).get("context_checks") or {}
            rounds.append({"n": record["n"], "user": record["user"], "violet": record["violet"],
                           "context_checks_before": {k: before.get(k) for k in ("has", "matched_by", "failures")},
                           "location_checks_now": rechecked, "location_evidence_now": evidence})
        return {"report": str(report_path), "database_copy_of": str(source), "rounds": rounds}
    finally:
        db.close()
        cleanup(temp)


def main(argv: list[str] | None = None, input_fn: Callable[[str], str] = input,
         db_factory: Callable[[Path], object] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.recheck:
        result = recheck_report(args.recheck, args.recheck_db)
        target = (args.output_dir or args.recheck.parent) / "recheck.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"复核结果：{target}")
        return 0
    if args.summarize:
        report = upgrade(json.loads(args.summarize.read_text(encoding="utf-8")))
        result = summarize(report, load_scores(args.scores) if args.scores else None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        (args.summarize.parent / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                            encoding="utf-8")
        return 0
    from tools.violet_test_chat import open_session, print_leftovers
    from tools.harness import ReplayError, ScenarioClock, finish_session
    # everything that can refuse is checked before the environment, the gate or a session exist
    try:
        plan, plan_meta = load_plan(args.plan, args.plan_revision)
        rounds = parse_rounds(args.rounds, len(plan["rounds"]))
        if args.replay_from and (args.seed_from or args.continue_replay):
            raise ReplaySourceError("--replay-from 不能和 --seed-from / --continue-replay 一起用")
        if args.continue_replay and not args.seed_from:
            raise ReplaySourceError("续跑回放只在 --seed-from 复制出的新目录里进行（原目录保持不动）")
        if args.replay_only and not (args.replay_from or args.continue_replay):
            raise ReplaySourceError("--replay-only 需要 --replay-from 或 --continue-replay")
        if args.seed_from and (args.workdir is None or Path(args.workdir).exists()):
            raise ReplaySourceError("--seed-from 需要一个还不存在的 --workdir（复制到新目录，不覆盖任何目录）")
        replay = (load_replay_chain(args.replay_from, plan, min(rounds), plan_meta["changed_rounds"])
                  if args.replay_from else None)
        source_dir = args.seed_from or args.workdir
        check_rounds = set(rounds)
        if args.continue_replay:
            seed_state = read_state(source_dir) if Path(source_dir).exists() else None
            done = [r["n"] for r in (seed_state or {}).get("rounds") or []]
            if done:
                check_rounds |= set(range(max(done) + 1, min(rounds)))
        continuation = check_workdir(source_dir, replay is not None, check_rounds, args.real, plan_meta,
                                     accept_incomplete_memory=args.accept_incomplete_memory)
        if args.seed_from and continuation["mode"] != "continue":
            raise ReplaySourceError(f"{args.seed_from} 不是已完成若干轮的测试目录，不能用作 --seed-from")
        continuation_replay = (load_continuation_replay(args.continue_replay, args.continue_replay_sha256, plan,
                                                        plan_meta, continuation["state"], min(rounds))
                               if args.continue_replay else None)
    except ReplaySourceError as exc:
        print(f"未启动：{exc}")
        return 2
    if plan_meta["revision"] > 1:
        print(f"剧本：{Path(plan_meta['plan']).name} 修正版 {plan_meta['revision']}（改动第 {plan_meta['changed_rounds']} 轮）")
    try:
        session = open_session(args, input_fn=input_fn, **({"seed_from": args.seed_from} if args.seed_from else {}),
                               **({"db_factory": db_factory} if db_factory else {}))
        # Phase 8.9: the 32-round plan is compared with reports recorded before ordinary-day items existed
        # (ai/daily_life.py); they would add life events none of those reports has, so this tool keeps them off
        os.environ["VIOLET_DAILY_LIFE"] = "0"
    except (PermissionError, IsolationError) as exc:
        print(f"未启动：{exc}")
        return 2
    try:
        from ai.settings import now_local
        state = continuation["state"] or new_state(args.plan, replay)
        state.setdefault("plan_revisions", {})[str(plan_meta["revision"])] = {
            "file": plan_meta["revision_file"], "sha256": plan_meta["revision_sha256"],
            "changed_rounds": plan_meta["changed_rounds"]}
        if replay is not None:
            start = datetime.fromisoformat(replay["anchor"])
        elif continuation["mode"] == "continue":
            start = datetime.fromisoformat(state["clock_end"])
        else:
            start = now_local()
        session.use_clock(ScenarioClock(start))
        clock_start = session.clock.now().isoformat()
        completed_before = [r["n"] for r in state["rounds"]] if continuation["mode"] == "continue" else []
        if min(rounds) > 1 and replay is None and continuation["mode"] == "fresh":
            print(f"注意：没有回放第 1-{min(rounds) - 1} 轮，本次从空状态开始。")
        env = {"times": {r["n"]: r["scenario_time"] for r in state["rounds"] if r.get("scenario_time")},
               "modes": {r["n"]: {"mode": r.get("mode"), "scripted_memory": bool(r.get("scripted_memory"))}
                         for r in state["rounds"]},
               "mode": "real" if session.real else "mock"}
        # a plan time that depends on an already finished round is checked before any call is made
        from ai.settings import local_timezone
        for rnd in plan["rounds"]:
            spec = rnd.get("time")
            if rnd["n"] in rounds and spec and int(spec["base_round"]) in completed_before:
                target = scenario_target(spec, round_start(session.db, plan, int(spec["base_round"]), env["times"]),
                                         local_timezone())
                if target < session.clock.now():
                    raise PlanTimeError(f"第 {rnd['n']} 轮的剧本时间 {target.isoformat()} 早于当前场景时间")
    except PlanTimeError as exc:
        print(f"未启动：{exc}")
        finish_session(session, args.keep)
        return 2
    except BaseException:
        finish_session(session, args.keep)
        raise

    def ask(record):
        raw = input_fn(f"第 {record['n']} 轮评分 0/1/2（{record['rubric']}），回车跳过: ").strip()
        note = input_fn("备注: ").strip() if raw else ""
        return (int(raw) if raw in ("0", "1", "2") else None), note
    def manual(rnd):
        raw = input_fn(f"[{rnd['n']}] 计划：{rnd['user']}\n    回车照发 / 输入改写内容 / /skip 跳过 / /quit 停止: ").strip()
        if raw == "/quit":
            raise KeyboardInterrupt
        return None if raw == "/skip" else raw
    out_dir = args.output_dir or OUTPUT_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    records: list[dict[str, Any]] = []
    mode = "real" if session.real else "mock"

    def round_done(n: int, record: dict[str, Any]) -> None:
        state["rounds"].append({"n": n, "mode": mode, "scripted_memory": bool(args.scripted_memory),
                                "scenario_time": record["scenario_time"], "plan_revision": plan_meta["revision"],
                                "image_provider": "mock" if session.image_mock else "off",
                                # the round ran; whether its background work finished is a separate fact
                                "background_complete": record["background_complete"],
                                "memory_jobs": [{k: j[k] for k in ("job_id", "status", "attempts", "error_type")}
                                                for j in record["memory_jobs"]]})
        state["clock_end"] = session.clock.now().isoformat()
        write_state(session.workdir, state)
    replay_summary = None
    try:
        if replay is not None:
            write_state(session.workdir, state)          # status in_progress: never mistaken for a finished replay
            preload(session, plan)                      # Violet's life as of the original run's start
            try:
                replay_summary = run_replay(session, replay)
            except BaseException as exc:
                state["replay"]["status"] = "failed"
                state["replay"]["error"] = exc.__class__.__name__
                write_state(session.workdir, state)
                raise
            state["replay"]["status"] = "complete"
            starts = {s["n"]: s["scenario_time"] for s in replay_summary["scenario_clock"]["round_starts"]}
            state["rounds"] = [{"n": n, "mode": "replay", "scenario_time": starts.get(n)}
                               for n in replay_summary["rounds"]]
            env["times"].update(starts)
            env["modes"].update({n: {"mode": "replay", "scripted_memory": False} for n in replay_summary["rounds"]})
            state["clock_end"] = replay_summary["scenario_clock"]["end_of_replay"]
            write_state(session.workdir, state)
        if continuation_replay is not None:
            entry = {"source": continuation_replay["source"], "sha256": continuation_replay["sha256"],
                     "rounds": [r["n"] for r in continuation_replay["rounds"]], "seeded_from": session.seed,
                     "status": "in_progress"}
            state.setdefault("continuation_replays", []).append(entry)
            write_state(session.workdir, state)          # never mistaken for a finished replay if it stops here
            try:
                replay_summary = run_continuation_replay(session, continuation_replay)
            except BaseException as exc:
                entry["status"], entry["error"] = "failed", exc.__class__.__name__
                write_state(session.workdir, state)
                raise
            entry["status"] = "complete"
            starts = {s["n"]: s["scenario_time"] for s in replay_summary["scenario_clock"]["round_starts"]}
            state["rounds"] += [{"n": n, "mode": "replay", "scenario_time": starts[n], "source": "continuation_replay",
                                 "plan_revision": plan_meta["revision"]} for n in replay_summary["rounds"]]
            env["times"].update(starts)
            env["modes"].update({n: {"mode": "replay", "scripted_memory": False} for n in replay_summary["rounds"]})
            state["clock_end"] = replay_summary["scenario_clock"]["end"]
            write_state(session.workdir, state)
        try:
            if not args.replay_only:
                run_plan(session, plan, rounds, args.scripted_memory, ask if args.manual_score else None,
                         records=records, manual_fn=manual if args.manual else None, on_round_done=round_done,
                         env=env)
        except (KeyboardInterrupt, EOFError):
            print(f"\n已停止，保存已完成的 {len(records)} 轮。")
        final_state = session.state()
        clock_end = session.clock.now().isoformat()
    except ReplayError as exc:
        print(f"回放失败（没有发起任何计费调用）：{exc}")
        finish_session(session, args.keep)
        return 4
    except PlanTimeError as exc:
        print(f"剧本时间错误，已停止（已完成的轮次已保存）：{exc}")
        finish_session(session, args.keep)
        return 4
    except BaseException:
        finish_session(session, args.keep)          # never raises: the original error stays the one reported
        raise
    cleaned = finish_session(session, args.keep)
    extra = {"scenario_clock": {"start": clock_start, "end": clock_end},
             "continuation": {"mode": continuation["mode"], "workdir": str(session.workdir),
                              "completed_rounds_before": completed_before, "seeded_from": session.seed,
                              "accepted_unfinished_memory_jobs": continuation.get("unfinished_memory_jobs") or [],
                              "accepted_rounds_with_unfinished_background_work":
                                  continuation.get("rounds_with_unfinished_background_work") or []},
             "replay": replay_summary, "plan": plan_meta}
    path = write_outputs(records, plan, session, args, out_dir, final_state, extra)
    print(f"报告：{path}\n评分表：{out_dir / 'score_sheet.csv'}\n{usage_line(session.recorder)}")
    print_leftovers(exclude=session.workdir)
    return 0 if cleaned else 3


if __name__ == "__main__":
    sys.exit(main())
