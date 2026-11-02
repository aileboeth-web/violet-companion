"""Same decisions with fewer tokens? (2026-11-02, user: 只要是同样的效果，token 可以更省)

Runs the memory step and the proactive decision with the server's real model, twice per case:
  A  the full forms   VIOLET_MEMORY_COMPACT_TOOLS=0, VIOLET_DECISION_HISTORY_TURNS=24 / _MAX_CHARS=4000,
                      VIOLET_DECISION_ALWAYS_TASK_RULES=1
  B  the short forms  the defaults since 0.7.36
and prints, per case, what each did (memory: which tools wrote; decision: act or not) and the prompt tokens.
A case passes when B does what is expected (A is shown for comparison; a model is not fully deterministic, so
--repeat runs each case more than once).

Everything is written to a throw-away folder; nothing is sent to any chat. Usage on the server (as the violet user):
  /opt/violet/venv/bin/python /opt/violet/app/tools/decision_eval.py --env /opt/violet/config/violet.env
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FULL = {"VIOLET_MEMORY_COMPACT_TOOLS": "0", "VIOLET_DECISION_HISTORY_TURNS": "24",
        "VIOLET_DECISION_HISTORY_MAX_CHARS": "4000", "VIOLET_DECISION_ALWAYS_TASK_RULES": "1"}
SHORT = {"VIOLET_MEMORY_COMPACT_TOOLS": "1", "VIOLET_DECISION_HISTORY_TURNS": "8",
         "VIOLET_DECISION_HISTORY_MAX_CHARS": "1500", "VIOLET_DECISION_ALWAYS_TASK_RULES": "0"}

# (id, what the user said, Violet's reply, setup, the tools that count as right; [] = nothing should be written)
MEMORY_CASES = [
    ("like", "我喜欢喝杨枝甘露奶茶", "好甜的选择", None, [{"remember_profile_fact"}, {"upsert_core_memory"}, {"save_memory"}]),
    ("remind", "明天早上八点提醒我交作业", "好，明早八点叫你", None, [{"create_commitment"}]),
    ("situation", "我今天下午请假了在宿舍躺着", "那好好休息", None, [{"set_situation"}]),
    ("event", "下周三要去面试，有点紧张", "你可以的", None, [{"save_memory"}]),
    ("style", "以后回复短一点，别那么长", "好", None, [{"set_interaction_preference"}]),
    ("nothing", "哈哈哈好吧", "嘿嘿", None, []),
    ("correct", "我不是学计算机的，是学物理的", "哦哦记错了", "major", [{"correct_profile_fact"}]),
    ("cancel", "不用提醒我交作业了，已经交了", "好嘞", "reminder", [{"cancel_commitment"}]),
]

# (id, setup, expected should_act: True / False / None = either)
DECISION_CASES = [
    ("goodnight", "goodnight", False),
    ("cold", "cold", False),
    ("quiet_day", "quiet_day", None),
]


def apply(env: dict[str, str]) -> None:
    import os
    os.environ.update(env)


def clear(db) -> None:
    from ai import memory_scope
    names = [r["name"] for r in db.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    for name in names:
        if name in memory_scope.SCOPED_TABLES or name in ("inbound_messages", "llm_usage", "web_findings"):
            db.execute(f"DELETE FROM {name}")


def tokens(db) -> int:
    return int(db.fetch_one("SELECT COALESCE(SUM(prompt_tokens), 0) AS n FROM llm_usage")["n"])


def memory_case(db, case) -> dict[str, Any]:
    from ai.agents.memory_agent import MemoryAgent
    from ai.memory_jobs import build_memory_input
    cid, said, reply, setup, _ = case
    clear(db)
    now = db.now()
    if setup == "major":
        db.execute("INSERT INTO user_profile_facts (fact_key, category, value, origin, source, status, created_at, "
                   "updated_at) VALUES ('study.major', 'study', '计算机', 'user_stated', 'memory_agent', 'active', ?, ?)",
                   (now, now))
    if setup == "reminder":
        db.execute("INSERT INTO commitments (kind, content, due_at, status, evidence, created_at, updated_at) "
                   "VALUES ('reminder', '交作业', ?, 'pending', '提醒我交作业', ?, ?)", (now[:10] + "T20:00:00+08:00", now, now))
    message_id = db.add_user_message("text", said)
    db.add_assistant_message("text", reply)
    written: list[dict[str, Any]] = []
    try:
        MemoryAgent().run(build_memory_input(said, reply), call_context={
            "turn_id": f"eval-{cid}", "user_message": said, "assistant_reply": reply, "user_message_id": message_id,
            "background_job": True, "write_log": written, "previous_writes": []})
        error = None
    except Exception as exc:                                  # noqa: BLE001
        error = f"{exc.__class__.__name__}: {exc}"[:200]
    return {"tools": sorted({w.get("tool") for w in written if w.get("tool")}), "tokens": tokens(db), "error": error}


def decision_case(db, case) -> dict[str, Any]:
    from ai.agents.proactive_agent import ProactiveAgent
    from ai.memory_model import parse_time
    from ai.proactive_scheduler import ProactiveScheduler, now_china
    cid, setup, _ = case
    clear(db)
    now = parse_time(db.now())

    def at(minutes_ago: float) -> str:
        return (now - timedelta(minutes=minutes_ago)).isoformat()
    talk = [("user", "今天上课好累"), ("assistant", "辛苦啦，下课了吗"), ("user", "刚下课，准备去吃饭"),
            ("assistant", "去吃点好的"), ("user", "食堂随便吃点"), ("assistant", "那也要吃饱哦")] * 4
    if setup == "goodnight":
        rows = talk + [("user", "我困了，晚安，明天还要早起"), ("assistant", "晚安，早点睡")]
        start = 3 + len(rows)
    elif setup == "cold":
        rows = talk + [("assistant", "在干嘛呀"), ("assistant", "今天天气好好"), ("assistant", "还在忙吗")]
        start = 600
    else:
        rows = talk
        start = 360 + len(rows)
    for i, (role, text) in enumerate(rows):
        when = at(start - i * (1 if setup != "cold" else 60))
        (db.add_user_message if role == "user" else db.add_assistant_message)("text", text, created_at=when)
    if setup == "cold":                       # three messages she started, none answered (cold_streak = 3)
        for _ in range(3):
            db.add_proactive_event("scheduled_check", status="sent", action_type="chat", reason="eval")
    sched = ProactiveScheduler(None, None, None, send_messages=lambda *a: None)
    context = sched._build_event_context(now_china())
    try:
        result = ProactiveAgent().run(json.dumps(context, ensure_ascii=False))
        decision = json.loads(result.get("content") or "{}")
        act, error = bool(decision.get("should_act")), None
    except Exception as exc:                                  # noqa: BLE001
        act, error = None, f"{exc.__class__.__name__}: {exc}"[:200]
    return {"act": act, "tokens": tokens(db), "error": error}


def memory_ok(case, result) -> bool:
    expected = case[4]
    if result["error"]:
        return False
    if not expected:
        return not result["tools"]
    return any(want <= set(result["tools"]) for want in expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", type=Path, required=True, help="the server's violet.env")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="violet-decision-eval-"))
    from tools.scenario_eval import prepare
    prepare(args.env, work, [], mock=False)
    apply({"VIOLET_ASYNC_MEMORY": "0"})
    from ai.chat import violetdb as db
    totals = {"A": 0, "B": 0}
    passed = {"A": 0, "B": 0}
    count = 0
    print("记忆整理（期望 / A 完整说明 / B 精简说明）")
    for case in MEMORY_CASES:
        for _ in range(args.repeat):
            row = {}
            for name, env in (("A", FULL), ("B", SHORT)):
                apply(env)
                row[name] = memory_case(db, case)
                totals[name] += row[name]["tokens"]
                passed[name] += memory_ok(case, row[name])
            count += 1
            want = " 或 ".join("+".join(sorted(w)) for w in case[4]) or "不写"
            print(f"  {case[0]:10} 期望 {want}\n"
                  f"             A {'对' if memory_ok(case, row['A']) else '错'} {row['A']['tools'] or '不写'} {row['A']['tokens']} token"
                  f"{'  ' + row['A']['error'] if row['A']['error'] else ''}\n"
                  f"             B {'对' if memory_ok(case, row['B']) else '错'} {row['B']['tools'] or '不写'} {row['B']['tokens']} token"
                  f"{'  ' + row['B']['error'] if row['B']['error'] else ''}")
    print("主动联系判断")
    for case in DECISION_CASES:
        for _ in range(args.repeat):
            row = {}
            for name, env in (("A", FULL), ("B", SHORT)):
                apply(env)
                row[name] = decision_case(db, case)
                totals[name] += row[name]["tokens"]
                good = row[name]["error"] is None and (case[2] is None or row[name]["act"] == case[2])
                passed[name] += good
            count += 1
            want = {True: "找你", False: "不找", None: "都行"}[case[2]]
            print(f"  {case[0]:10} 期望 {want}  A {row['A']['act']} {row['A']['tokens']} token   "
                  f"B {row['B']['act']} {row['B']['tokens']} token"
                  + "".join(f"  {k}: {row[k]['error']}" for k in "AB" if row[k]["error"]))
    print(f"结果：A 对 {passed['A']}/{count}，B 对 {passed['B']}/{count}；输入 token A {totals['A']}，B {totals['B']}"
          f"（B 省 {totals['A'] - totals['B']}）")
    return 0 if passed["B"] >= passed["A"] else 1


if __name__ == "__main__":
    sys.exit(main())
