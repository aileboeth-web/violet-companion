"""Round 29–32 suite for the controlled synthetic_rounds runner; no API client of its own."""
from __future__ import annotations

import copy
import json

from tools.legacy import upgrade
import re
from pathlib import Path

from tools.synthetic_rounds import PROJECT, _plain, build_seed, digest, write_new

PURPOSE = "round29_32_specialized_test"
PLAN = "violet/tests/samples/acceptance_dialogue_30.json"
REVISION = "violet/tests/samples/acceptance_dialogue_30.rev2.json"
SENT_REPORT = "local/b113/real-20261019-014631/specialized_report.json"
ROUND29_REPORT = "local/b115/real-20261019-041807/specialized_report.json"

# Round 30 rubric: "记得结果（顺利结束），不当作未完成". v1 required "顺利" plus a fixed completion
# word and failed the real reply "答辩当时挺顺利的" (B.11.9). v2 needs one plain statement with a
# positive outcome framed as finished or past; unfinished, forgetting or denial anywhere fails it,
# and a guess, wish or question cannot be that statement.
ANSWER_ASSERTION = "round30.answer_recalls_outcome/v2"
UNFINISHED = "还没结束|还没完成|还在准备|准备得怎么样|什么时候答辩"
_OUTCOME = re.compile("顺利|成功|通过|过了|过关|圆满")
_FINISHED = re.compile("结束|完成|搞定|答完|通过|过了|过关|已经|当时|那时|那天|那次|(?:顺利|成功|圆满)[的地]?了")
_CONTRADICTED = re.compile(
    "不(?:太|是很|怎么|大)?顺利|没(?:有)?(?:通过|过关|成功|结束|完成|答)|没过|不是答辩|失败|挂了"
    "|还没(?:有)?(?:结束|完成|答|开始|考|过|到)|尚未|还在准备|正在准备|准备中|准备得(?:怎么样|如何)|还要准备"
    "|快要答辩|即将答辩|要答辩了|什么时候答辩"
    "|不记得|想不起|记不清|记不太清|(?<![没会])(?<!没有)忘(?:了|记)")
_TENTATIVE = re.compile("应该|大概|可能|也许|好像|估计|猜|不确定|是不是|吧|祝|希望|愿|加油|会顺利|[？?]")


def recalls_completed_outcome(text):
    if _CONTRADICTED.search(text):
        return False
    sentences = re.findall(r"[^。！!？?\n]+[。！!？?]*", text)
    return any(_OUTCOME.search(s) and _FINISHED.search(s) and not _TENTATIVE.search(s) for s in sentences)


def round30_checks(text, *, complete, calls, recalled, chat_lines, target):
    """Shared by the live suite and the post-hoc reassessment; structure is checked from evidence, not text."""
    matches = [m for m in recalled if m.get("id") == target["id"] and m.get("content") == target["content"]
               and m.get("progress") == "done"]
    return {"execution_complete": complete,
        "intent_queried": any(c["agent"] == "IntentAgent" and "query_memory" in c["tool_calls"] for c in calls),
        "correct_target_done": bool(matches),
        "correct_event_in_chat": any(target["content"] in line and "已结束" in line for line in chat_lines),
        "answer_recalls_outcome": recalls_completed_outcome(text),
        "no_unfinished_claim": not re.search(UNFINISHED, text)}


ROUND30_REPORT = "local/b118/real-20261019-060349/specialized_report.json"
ROUND30_REASSESSMENT = "local/b119/reassess-20261019-063152/round30_reassessment.json"
# Registered at B.11.10; a changed byte in either file refuses --rounds 31-32. There is deliberately no flag
# that can stand in for this evidence.
REGISTERED_SHA256 = {ROUND30_REPORT: "4a5260152502662bd55a8347de832da6fc00f0c60e5e1c24f6b412d55fedc642",
                     ROUND30_REASSESSMENT: "d835902b508282c73b868d95486c6dfa0399bf6460649c80593fda2f63a79686"}
ROUND30_EXPECTED_DIFF = [{"check": "answer_recalls_outcome", "recorded": False, "v1": False, "v2": True}]
ROUND30_STRUCTURAL = ("execution_complete", "intent_queried", "correct_target_done", "correct_event_in_chat",
                      "no_unfinished_claim")


def select_rounds(value):
    if value not in (None, "29-32", "30-32", "31-32"):
        raise ValueError("Only 29-32, 30-32 or 31-32 is supported; round31 must precede round32")
    return {"30-32": (30, 31, 32), "31-32": (31, 32)}.get(value, (29, 30, 31, 32))


def round30_evidence(seed, project=PROJECT):
    """Skip round 30 only on the registered real run plus its committed v2 reassessment, re-checked now."""
    from tools.reassess_tail import KIND, reassess
    root = Path(project).parent
    paths = {rel: root / rel for rel in REGISTERED_SHA256}
    problems = [f"missing {rel}" if not p.is_file() else f"sha256 differs from registered value: {rel}"
                for rel, p in paths.items() if not p.is_file() or digest(p) != REGISTERED_SHA256[rel]]
    if problems:
        raise ValueError("Round30 evidence refused: " + "; ".join(problems))
    source = upgrade(json.loads(paths[ROUND30_REPORT].read_text(encoding="utf-8")))
    stored = upgrade(json.loads(paths[ROUND30_REASSESSMENT].read_text(encoding="utf-8")))
    expected = next(r["definition"]["value"] for r in seed["requirements"] if r["definition"]["value"]["n"] == 30)
    if (source.get("real") is not True or source.get("verification_environment") is not None
            or source.get("purpose") != PURPOSE):
        problems.append("round30 source is not a real run of this suite")
    rows = [(i, r) for i, r in enumerate(source.get("rounds", [])) if r.get("n") == 30]
    if len(rows) != 1:
        problems.append("round30 row missing or duplicated")
        rows = [(None, {})]
    i, row = rows[0]
    checks = row.get("checks") or {}
    if row.get("user") != expected["user"]:
        problems.append("round30 user input differs from the plan")
    if not row.get("calls") or any(c.get("round") != 30 or c.get("ok") is not True for c in row["calls"]):
        problems.append("round30 model calls missing or failed")
    if not all(checks.get(k) is True for k in ROUND30_STRUCTURAL):
        problems.append("round30 recorded structural checks did not all pass")
    if {k for k, v in checks.items() if v is not True} != {"answer_recalls_outcome"}:
        problems.append("round30 recorded failures are not exactly the v1 answer check")
    head = stored.get("violet_head") or {}
    result = stored.get("round30") or {}
    if stored.get("kind") != KIND or stored.get("model_calls") != 0 or stored.get("real_model_reexecuted") is not False:
        problems.append("reassessment is not a post-hoc offline reassessment without model calls")
    if (stored.get("assertion") or {}).get("new") != ANSWER_ASSERTION:
        problems.append("reassessment did not use " + ANSWER_ASSERTION)
    if not head.get("head") or head.get("tools_uncommitted") is not False:
        problems.append("reassessment code was not committed")
    src = stored.get("source") or {}
    if (not str(src.get("dir", "")).replace("\\", "/").endswith(str(Path(ROUND30_REPORT).parent.as_posix()))
            or (src.get("sha256") or {}).get("specialized_report.json") != REGISTERED_SHA256[ROUND30_REPORT]):
        problems.append("reassessment source is not the registered round30 report")
    if result.get("status") != "通过" or result.get("evidence_problems") != [] or result.get("differences") != ROUND30_EXPECTED_DIFF:
        problems.append("reassessment result or v1/v2 difference is not the expected one")
    if any(r.get("reassessed") is not False for r in stored.get("rounds_31_32") or [{}]):
        problems.append("reassessment claims rounds 31-32")
    if not problems:
        live = reassess(paths[ROUND30_REPORT].parent, project)[0]["round30"]
        if (live["status"] != "通过" or live.get("differences") != ROUND30_EXPECTED_DIFF
                or live.get("reassessed_checks") != result.get("reassessed_checks")):
            problems.append("current v2 assertion does not reproduce the stored reassessment")
    if problems:
        raise ValueError("Round30 evidence refused: " + "; ".join(problems))
    return {"n": 30, "status": "未执行", "reason": "not_selected_no_model_call",
        "historical_evidence": {"path": ROUND30_REPORT, "sha256": REGISTERED_SHA256[ROUND30_REPORT],
            "pointer": f"/rounds/{i}", "historical_status": row.get("status"), "real": True,
            "recorded_failed_checks": ["answer_recalls_outcome"]},
        "reassessment": {"path": ROUND30_REASSESSMENT, "sha256": REGISTERED_SHA256[ROUND30_REASSESSMENT],
            "kind": KIND, "assertion": ANSWER_ASSERTION, "violet_head": head.get("head"), "status": result["status"],
            "differences": ROUND30_EXPECTED_DIFF, "note": "Post-hoc offline reassessment; not a re-executed round."},
        "history_imported": False}


def selection_evidence(seed, numbers, project=PROJECT):
    skipped = []
    if 29 not in numbers:
        path = Path(project).parent / ROUND29_REPORT
        source = upgrade(json.loads(path.read_text(encoding="utf-8")))
        rows = [(i, r) for i, r in enumerate(source.get("rounds", [])) if r.get("n") == 29]
        if (source.get("real") is not True or source.get("verification_environment") is not None
                or source.get("purpose") != PURPOSE or len(rows) != 1):
            raise ValueError("Missing real round29 evidence")
        i, row = rows[0]
        expected = seed["requirements"][0]["definition"]["value"]
        if (row.get("status") != "通过" or row.get("user") != expected["user"]
                or not row.get("checks") or not all(row["checks"].values())
                or not row.get("calls") or any(c.get("round") != 29 or c.get("ok") is not True for c in row["calls"])):
            raise ValueError("Round29 evidence is not a completed successful real round")
        skipped.append({"n": 29, "status": "未执行", "reason": "not_selected_no_model_call",
            "historical_evidence": {"path": ROUND29_REPORT, "sha256": digest(path),
                "pointer": f"/rounds/{i}", "historical_status": row["status"], "real": True},
            "history_imported": False})
    if 30 not in numbers:
        skipped.append(round30_evidence(seed, project))
    return {"requested_rounds": list(numbers), "skipped_rounds": skipped,
            "state_policy": "Fresh synthetic seed; no prior database, dialogue or writes of skipped rounds imported."}


def field(value, source="synthetic", evidence=None, note=None):
    return {"value": value, "source": source, "evidence": evidence, "note": note}


def build_tail_seed(project=PROJECT):
    """No DB copying: recover only evidence-backed fields, never the earlier suite's probes."""
    root = Path(project).parent
    plan = upgrade(json.loads((root / PLAN).read_text(encoding="utf-8")))
    revision = upgrade(json.loads((root / REVISION).read_text(encoding="utf-8")))
    if digest(root / PLAN) != revision["base_sha256"]:
        raise ValueError("Original plan / rev2 hash mismatch")
    rounds = [r for r in plan["rounds"] if 29 <= r["n"] <= 32]
    if [r["n"] for r in rounds] != [29, 30, 31, 32] or any(
            set(r) & {"time", "advance_hours", "before", "after"} for r in rounds):
        raise ValueError("Unexpected tail plan: audit before changing time or scheduling")
    source = upgrade(json.loads((root / SENT_REPORT).read_text(encoding="utf-8")))
    if (source.get("real") is not True or source.get("status") != "通过"
            or source.get("purpose") != "round27_28_specialized_test"
            or source.get("verification_environment") is not None):
        raise ValueError("A completed real specialized reminder report is required")
    i, r28 = next((i, r) for i, r in enumerate(source["rounds"]) if r["n"] == 28)
    reminders = r28["reminder"]["reminders"]
    if len(reminders) != 1 or not all(reminders[0].get(k) == "sent" for k in
                                   ("delivery", "status_after", "event_status")):
        raise ValueError("Reminder delivery evidence incomplete")
    if not reminders[0]["outbox"] or any(o["status"] != "sent" for o in reminders[0]["outbox"]):
        raise ValueError("Missing sent outbox evidence")
    original = build_seed(project)
    def workspace_fields(node):
        node = copy.deepcopy(node)
        def visit(value):
            if isinstance(value, dict):
                if "source" in value and value.get("evidence") and value["evidence"]["path"].startswith("tools/"):
                    value["evidence"]["path"] = "violet/" + value["evidence"]["path"]
                for child in value.values(): visit(child)
            elif isinstance(value, list):
                for child in value: visit(child)
        visit(node)
        return node
    historical = workspace_fields(original["state"])
    sources = {"plan": {"path": PLAN, "sha256": digest(root / PLAN)},
               "rev2": {"path": REVISION, "sha256": digest(root / REVISION)},
               "sent": {"path": SENT_REPORT, "sha256": digest(root / SENT_REPORT)}}
    def sent(value, pointer): return field(value, "report", {**sources["sent"], "pointer": pointer})
    prefix = f"/rounds/{i}/reminder/reminders/0"
    reminder = reminders[0]
    commitment = {k: sent(reminder[k], prefix + "/" + k) for k in
                  ("content", "due_at", "delivered_at")}
    commitment["status"] = sent(reminder["status_after"], prefix + "/status_after")
    commitment["message"] = sent(r28["reminder"]["sent"][0], f"/rounds/{i}/reminder/sent/0")
    facts = [f for f in historical["facts"] if f["fact_key"]["value"] in ("address_name", "study.major")]
    if next(f["value"]["value"] for f in facts if f["fact_key"]["value"] == "study.major") != "天文":
        raise ValueError("Old major must be astronomy; never seed the correction")
    [defense] = [e for e in historical["events"] if "答辩" in e["content"]["value"]]
    if defense["progress"]["value"] != "done" or "顺利" not in defense["content"]["value"]:
        raise ValueError("No completed defense outcome")
    return {"synthetic": True, "purpose": PURPOSE, "specialized_test": True,
            "continuous_acceptance_eligible": False, "counts_toward_original_32": False,
            "original_database_restored": False, "sources": sources,
            "requirements": [{"definition": field(r, "report", {**sources["plan"],
                "pointer": f"/rounds/{r['n'] - 1}"}, "Original test requirement, not historical execution")}
                for r in rounds],
            "global_criteria": field(plan["global_criteria"], "report", {**sources["plan"], "pointer": "/global_criteria"}),
            "state": {
                "scenario_time": field(reminder["delivered_at"], "inferred", {**sources["sent"],
                    "pointer": prefix + "/delivered_at"},
                    "Use the existing observed delivery instant as an independent clock anchor; original round29 time unknown. No date addition or time jump."),
                "facts": facts, "events": historical["events"], "commitment": commitment,
                "preferences": historical["preferences"], "dialogue": [],
                "history_policy": field("No recent dialogue is seeded, so round30 cannot read the answer from it."),
                "unknown": {k: field(None, "unknown", note="Not recovered; never asserted as original history") for k in (
                    "original_round29_32_times", "original_database_ids", "original_write_times",
                    "full_dialogue_history", "profile_confidence_and_evidence", "event_weights_and_times",
                    "original_preference_rows", "original_proactive_chat_preference", "violet_life_state",
                    "homework_submission_before_round29")}},
            "limitations": ["Synthetic, non-continuous; no original32 score.",
                "No original life, message history or control probes are imported.",
                "Naturalness remains unscored until human review; text API is not WeChat delivery."]}


def apply_tail_seed(session, seed):
    from ai.profile_store import Profile
    db, state = session.db, seed["state"]
    profile = Profile(db)
    for raw in state["facts"]:
        row = _plain(raw)
        profile.facts.remember(row["fact_key"], row["value"], source="dashboard", actor="synthetic_seed")
    for raw in state["events"]:
        db.add_event_memory(**_plain(raw), category="event", source_turn_id="synthetic:historical-event")
    for raw in state["preferences"]:
        row = _plain(raw)
        profile.preferences.set_explicit(row["key"], row["value"], evidence=row["evidence"],
                                         source="dashboard", actor="synthetic_seed")
    c = _plain(state["commitment"])
    cid = db.add_commitment(c["content"], c["due_at"], source_turn_id="synthetic:sent-reminder")["commitment"]["id"]
    eid = db.add_proactive_event("commitment_reminder", status="sent", action_type="send_message",
                                decision={"commitment_id": cid}, reason="synthetic: observed sent reminder",
                                sent_messages=[c["message"]])
    db.update_commitment(cid, status=c["status"], delivered_at=c["delivered_at"], proactive_event_id=eid)
    db.add_outbound_message("synthetic:sent-reminder", "proactive", "text", c["message"],
                            proactive_event_id=eid, status="sent")
    tables = {}
    overrides = {"user_profile_facts": state["facts"], "event_memories": state["events"],
        "interaction_preferences": [{"pref_key": p["key"], "value": p["value"], "evidence": p["evidence"]}
                                    for p in state["preferences"]],
        "commitments": [{k: state["commitment"][k] for k in ("content", "due_at", "status", "delivered_at")}],
        "outbound_messages": [{"content": state["commitment"]["message"]}]}
    for table in (*overrides, "proactive_events", "messages", "life_events", "user_situations"):
        rows = db.fetch_all(f"SELECT * FROM {table} ORDER BY id")
        annotated = [{k: field(v, note="New local ID/default/audit value; original unknown") for k, v in r.items()} for r in rows]
        for row, values in zip(annotated, overrides.get(table, [])):
            for key, source in values.items():
                assert row[key]["value"] == source["value"], (table, key)
                row[key] = source
        tables[table] = annotated
    write_new(session.workdir / "seed_materialized.json", {"synthetic": True, "purpose": PURPOSE, "tables": tables})
    [defense] = [e for e in db.fetch_all("SELECT * FROM event_memories") if "答辩" in e["content"]]
    [major] = db.fetch_all("SELECT * FROM user_profile_facts WHERE fact_key='study.major' AND status='active'")
    return {"commitment_id": cid, "event_id": eid, "defense": defense, "old_major": major}


def tail_mock():
    """Only model responses are simulated; all tools, memory writes and retrieval run product code."""
    from tools.harness import MockLLM, _AGENT
    class TailMock(MockLLM):
        @classmethod
        def tools(cls, model, calls):
            result = cls._completion("", model)
            data = result.model_dump()
            data["choices"][0].update(finish_reason="tool_calls", message={"role": "assistant", "content": None,
                "tool_calls": [{"id": f"tail-{i}", "type": "function", "function": {
                    "name": name, "arguments": json.dumps(args, ensure_ascii=False)}} for i, (name, args) in enumerate(calls)]})
            return type(result).model_validate(data)

        def _create(self, **kwargs):
            agent, messages, model = _AGENT.get(), kwargs["messages"], str(kwargs["model"])
            user = next((m.get("content") for m in reversed(messages) if m["role"] == "user"), "")
            used_tool = any(m["role"] == "tool" for m in messages)
            if agent == "IntentAgent" and "答辩你还记得" in user and not used_tool:
                # Exercise the natural-language query allowed by the tool schema;
                # a bare keyword concealed the B.11.6 retrieval failure.
                return self.tools(model, [("query_memory", {"query": user})])
            if agent == "MemoryAgent" and "其实我不是学天文" in user and not used_tool:
                return self.tools(model, [("correct_profile_fact", {"key": "study.major", "new_value": "物理",
                    "old_value": "天文", "evidence": "不是学天文的，是学物理的"}), ("memory_done", {})])
            if agent == "ChatAgent":
                text = ("交完就能轻松一点了。" if "昨天交了作业" in user else
                        "记得，答辩已经顺利结束了。" if "答辩你还记得" in user else
                        "好，是物理，我记住了。" if "其实我不是学天文" in user else
                        "你的专业是物理。" if "我的专业是什么" in user else "嗯。")
                return self._completion(text, model)
            return super()._create(**kwargs)
    return TailMock


def run_tail(session, seed, report, perform, healthy, *, stop, numbers=(29, 30, 31, 32), audit=None):
    from tools.dialogue_eval import _chat_context_of_round
    from ai.chat import extract_tool_memories
    db, rec = session.db, session.recorder
    ids = apply_tail_seed(session, seed)
    if audit is not None:
        audit.target = ids["defense"]
    report["runtime_provenance"] = {"source": "synthetic", "ids": {k: ids[k] for k in ("commitment_id", "event_id")}}
    report["clock_policy"] = seed["state"]["scenario_time"]
    report["global_criteria"] = seed["global_criteria"]["value"]
    report["scoring_notice"] = "通过仅表示本专项自动结构检查通过；人工自然度未执行，不认证原32轮全局指标。"
    report["scenario_clock"] = {"start": session.clock.now().isoformat(), "time_jumps": []}
    plans = [r["definition"]["value"] for r in seed["requirements"] if r["definition"]["value"]["n"] in numbers]
    report["rounds"] = [{"n": r["n"], "user": r["user"], "rubric": r["rubric"], "status": "未执行",
                         "human_review": "未执行：自然度与rubric需人工评分；专项不计入原32轮。"} for r in plans]
    report["initial_state"] = {"facts": db.fetch_all("SELECT * FROM user_profile_facts"),
        "events": db.fetch_all("SELECT * FROM event_memories"), "messages": db.fetch_all("SELECT * FROM messages"),
        "commitments": db.list_commitments(), "preferences": db.fetch_all("SELECT * FROM interaction_preferences")}

    def check(number, values):
        report["checks"].update({f"round{number}.{k}": bool(v) for k, v in values.items()})
        row = next(r for r in report["rounds"] if r["n"] == number)
        row["checks"] = values
        row["status"] = "通过" if all(values.values()) else "失败"
        if not all(values.values()):
            report["execution_state"] = "failed_check"
            raise stop("specialized assertion")

    try:
        for planned, row in zip(plans, report["rounds"]):
            number = planned["n"]
            row["scenario_time"] = session.clock.now().isoformat()
            result = perform(number, f"round{number}.chat", lambda: session.say(planned["user"], number))
            row.update(result)
            row["status"] = "失败"  # incomplete API/background work is never a pass
            healthy()
            complete = result["background_complete"] and not result["execution_errors"] and bool(result["violet"])
            if not complete:
                check(number, {"execution_complete": False})
            text = "".join(result["violet"])
            system, context = _chat_context_of_round(session, result)
            values = {"execution_complete": complete}
            if number == 29:
                before = len(session.wechat.sent)
                probe = perform(29, "round29.dedup_probe", session.proactive_tick)
                row["dedup_probe"] = {"additional_probe": True, **probe}
                healthy()
                values.update(no_recreated_reminder=len(db.list_commitments()) == 1,
                    no_duplicate_send=len(session.wechat.sent) == before,
                    sent_state_preserved=db.get_commitment(ids["commitment_id"])["status"] == "sent",
                    no_duplicate_outbox=len(db.fetch_all("SELECT id FROM outbound_messages WHERE proactive_event_id=?",
                                                      (ids["event_id"],))) == 1,
                    no_duplicate_reminder_event=len(db.fetch_all("SELECT id FROM proactive_events WHERE event_type='commitment_reminder'")) == 1,
                    no_verbal_reminder=not re.search(r"(?:记得|别忘|该|要记得).{0,8}交作业|提醒你.{0,6}交作业", text))
            elif number == 30:
                recalled = extract_tool_memories({"messages": rec.last_request.get("IntentAgent", {}).get("messages", [])})
                target = ids["defense"]
                # Match the actual event line, not the generic '已结束' footer.
                lines = [line for line in system.splitlines() if line.startswith(f"- id={target['id']}; ")]
                row["recall_evidence"] = {"target_id": target["id"], "returned": recalled, "chat_event_lines": lines}
                row["answer_assertion"] = ANSWER_ASSERTION
                values = round30_checks(text, complete=complete, calls=result["calls"], recalled=recalled,
                                        chat_lines=lines, target=target)
            elif number == 31:
                active = db.fetch_all("SELECT * FROM user_profile_facts WHERE fact_key='study.major' AND status='active'")
                old = db.fetch_one("SELECT * FROM user_profile_facts WHERE id=?", (ids["old_major"]["id"],))
                row["correction_evidence"] = {"active": active, "old": old}
                correct = len(active) == 1 and active[0]["value"] == "物理" and active[0]["id"] != old["id"]
                values.update(new_major_persisted=correct,
                    old_major_superseded=correct and old["status"] == "superseded" and old["superseded_by"] == active[0]["id"]
                        and active[0]["supersedes_id"] == old["id"],
                    memory_write_observed=any("correct_profile_fact" in c["tool_calls"] or "remember_profile_fact" in c["tool_calls"]
                                              for c in result["calls"] if c["agent"] == "MemoryAgent"))
                if correct: ids["new_major_id"] = active[0]["id"]
            elif number == 32:
                active = db.fetch_all("SELECT id, value FROM user_profile_facts WHERE fact_key='study.major' AND status='active'")
                row["profile_context_evidence"] = {"fact_ids": (context or {}).get("fact_ids", []),
                    "major_lines": [line for line in system.splitlines() if "专业：" in line], "active": active}
                values.update(correction_dependency=next(r for r in report["rounds"] if r["n"] == 31)["status"] == "通过",
                    current_major_still_persisted=active == [{"id": ids.get("new_major_id"), "value": "物理"}],
                    corrected_fact_in_context=ids.get("new_major_id") in (context or {}).get("fact_ids", []) and "专业：物理" in system,
                    old_fact_not_in_context=ids["old_major"]["id"] not in (context or {}).get("fact_ids", []) and "专业：天文" not in system,
                    reply_uses_new_major="物理" in text and "天文" not in text)
            check(number, values)
        report["execution_state"] = "complete"
    finally:
        report["scenario_clock"]["end"] = session.clock.now().isoformat()
        for row in report["rounds"]:
            if row["status"] == "未执行": row["reason"] = "先前轮次未完整通过，不满足依赖；没有执行或预埋答案。"
