"""
MemoryAgent: 记忆判断 agent。

职责：分析本轮对话（用户消息 + Violet 实际回复），判断是否包含值得记忆的信息，
并通过工具写入记忆系统。第二阶段起在后台执行（ai/memory_jobs.py），不阻塞回复。

规则由代码强制：
  * 字符相似不会自动合并或覆盖记忆；相似记忆只作为候选，必须显式选择
    related_to=<id>（同一件事的新进展）或 new_event=true（另一件事）。
  * 事件进展只由用户明确的新信息改变（update_event_progress）；时间流逝不会变成“已结束”。
  * 只有用户原话明确要求提醒/跟进，才能创建 commitment（create_commitment 会核验原话）。
"""

import contextvars
import json
import re
from typing import Any

from ai.chat import violetdb, format_core_memories, format_recent_messages
from ai.context_engine import ContextEngine
from ai.conversation_state import TopicTracker
from ai.memory_model import mentions_topic, touches_topic
from ai.memory_model import (
    ENDED_PROGRESS, EVENT_PROGRESS, INTENT_LABELS, INTENT_LEVELS, KIND_LABELS, PROGRESS_LABELS, EventTimeMismatch,
    check_explicit_request, commitment_default_time, condition_clause, invented_part_clock, parse_time,
    recall_search_text, resolve_event_time, resolve_time, sounds_conditional,
)
from ai.onboarding import OnboardingPlanner
from ai.profile_store import Profile, evidence_in_message
from ai.tool_schemas import (
    CANCEL_COMMITMENT_SCHEMA,
    CORRECT_PROFILE_FACT_SCHEMA,
    DECLINE_PROFILE_QUESTION_SCHEMA,
    MEMORY_DONE_SCHEMA,
    REMEMBER_PROFILE_FACT_SCHEMA,
    RETRACT_PROFILE_FACT_SCHEMA,
    SET_INTERACTION_PREFERENCE_SCHEMA,
    SET_SITUATION_SCHEMA,
    CORRECT_MEMORY_SCHEMA,
    CREATE_COMMITMENT_SCHEMA,
    QUERY_MEMORY_SCHEMA,
    REINFORCE_CORE_MEMORY_SCHEMA,
    REINFORCE_MEMORY_SCHEMA,
    RETRACT_MEMORY_SCHEMA,
    SAVE_MEMORY_SCHEMA,
    UPDATE_CORE_MEMORY_SCHEMA,
    UPDATE_EVENT_PROGRESS_SCHEMA,
    UPDATE_MEMORY_SCHEMA,
    UPSERT_CORE_MEMORY_SCHEMA,
)
from ai.settings import env_bool

from .base_agent import BaseAgent

# The raw input of the current run (inline mode has no call_context).
_CURRENT_INPUT: contextvars.ContextVar[str] = contextvars.ContextVar("violet_memory_input", default="")

MEMORY_RULES = """【记忆系统规则（代码强制执行，优先于前文中与之冲突的示例）】
1. 记忆分类 kind：
   - fact：用户长期稳定的事实（身份、住处、宠物、长期习惯）。
   - event：具体发生/将要发生的事情，必须给出 progress：planned（还没发生）/ongoing（进行中）/done（用户明确说结束了）/cancelled（用户明确说取消）/unknown（无法判断）。
   - background：普通聊天背景，价值低，很快过期。
   - 用户此刻 / 今天的状态（在哪、在做什么、方不方便、请假或不去某节课）不属于这三类，不用 save_memory：用 set_situation（见用户档案规则 3）。
2. 记忆内容只写事实，不要写“适合鼓励/需要关心/记得追问”之类的行动建议。事件不会自动变成待跟进任务。
3. 时间一律写成绝对时间：用 event_time（ISO），或 event_date（today/tomorrow/YYYY-MM-DD）+ event_clock（HH:MM）。不要在内容里只写“明天”。
4. 用户提到已有事件的新进展或结果（例如“服务器修好了”“答辩结束了”“面试取消了”“考试改到下周了”）：
   - 先判断它是不是“未结束的事件”列表里的某一件。列表里的 id 可以直接用于 update_event_progress，不需要先 query_memory；
   - 列表里没有，再用 query_memory 查找；query_memory 返回空，不代表列表里的事件不存在；
   - 确定是哪一件时，用 update_event_progress(memory_id, progress, summary) 更新，不要新建一条；
   - 用户的话不足以确定是哪一件、或不足以说明结果时，不要强行修改原事件。
   save_memory 若返回 needs_decision，说明存在可能是同一件事的记忆：同一件事就用 update_event_progress 或 related_to=<id>，
   确认是另一件事才传 new_event=true。new_event=true 只对本轮 needs_decision 已经列出的候选有效，提前传入会先返回候选；
   不要为了少调用一次而跳过这个判断。
5. 时间过去不代表事情结束。只有用户明确说了结果，才能把 progress 设为 done/cancelled。
6. 用户纠正你记错的内容：用 correct_memory（或 update_core_memory）；用户说某条记忆根本不对：用 retract_memory。
7. 只有用户原话明确要求“提醒我/到时候问我/跟进”时才调用 create_commitment，evidence 必须逐字引用用户原话；
   普通分享（“我今天去面试了”）绝不创建 commitment。用户说“不用提醒了”时用 cancel_commitment。
8. 用户再次提到同一事实且没有新信息：reinforce_memory；不要因为话题被提起就提高重要性，除非用户给出新的重要信息。
9. 返回 confirmed_by_user 的记忆是用户确认过的（管理后台纠正过或用户亲口纠正过）：内容只能凭用户本轮原话修改，
   在 evidence 里逐字引用那句话；只是有了新进展，用 update_event_progress 只改进展。模糊的一句话不足以改它。
10. 想象、假设、角色扮演、梦、玩笑设定里的情节不是真实发生的事：不要保存成已发生的事件；确实值得记住时写明
   “（想象/角色扮演）”并用 kind=background。想做的事、计划和约定用 progress=planned，不要写成已经做过。
11. 将来的事分三种，save_memory 用 intent 写明：说定了的计划 intent=plan（progress=planned）；带条件的打算
   （“要是周三下午没课，我就去西湖”）intent=conditional，condition 写条件（“周三下午没课”），progress 由系统记为 unknown；
   随口的想法（“好想去西湖玩”）intent=wish。条件只有用户本轮亲口说满足了 / 定下来了，才用 update_event_progress
   改成 planned，evidence 逐字引用那句话；用户说条件不成立、去不了，就改成 cancelled。Violet 的回复、工具查到的结果、
   Violet 这一轮没接上（兜底消息），都不能当作条件已经满足。只说“下午 / 晚上”时只写 event_date，不要编具体钟点。"""


# Phase 7 B.2 (M4a): a background job may end right after its writes -- only when the model said it is
# done (memory_done in the same step) and every write of that step returned one of these actions.
# Anything else (rejected, candidate, needs_decision, kept_disabled, older_statement, duplicate, errors,
# a read such as query_memory, an unknown or denied tool) keeps the normal loop.
DONE_TOOL = "memory_done"
FINAL_WRITE_ACTIONS: dict[str, frozenset[str]] = {
    "save_memory": frozenset({"created", "updated", "updated_by_key", "reinforced", "already_saved"}),
    "update_memory": frozenset({"updated"}),
    "reinforce_memory": frozenset({"reinforced"}),
    "update_event_progress": frozenset({"progress_updated"}),
    "correct_memory": frozenset({"corrected"}),
    "retract_memory": frozenset({"retracted", "forgotten", "already_settled"}),
    "create_commitment": frozenset({"created", "already_saved"}),
    "cancel_commitment": frozenset({"cancelled"}),
    "remember_profile_fact": frozenset({"created", "superseded_previous", "confirmed", "already_known"}),
    "correct_profile_fact": frozenset({"corrected", "created", "confirmed"}),
    "retract_profile_fact": frozenset({"retracted"}),
    "set_situation": frozenset({"created", "already_set"}),
    "set_interaction_preference": frozenset({"set", "already_known"}),
    "decline_profile_question": frozenset({"declined"}),
    "upsert_core_memory": frozenset({"created", "updated"}),
    "update_core_memory": frozenset({"updated"}),
    "reinforce_core_memory": frozenset({"reinforced"}),
}

FINISH_RULE = ("【结束方式】这是回复发出后的后台整理。把这一轮需要的所有写入工具放在同一次回复里调用，并同时调用 memory_done；"
               "写入全部成功时系统直接结束，不再请你总结。需要先 query_memory 查询、核对或看结果再决定时，不要调用 memory_done。"
               "没有需要保存的内容就直接回复，不调用任何工具。")


def write_step_succeeded(name: str, result_text: str) -> bool:
    allowed = FINAL_WRITE_ACTIONS.get(name)
    if not allowed:
        return False
    try:
        payload = json.loads(result_text)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return False
    return payload.get("action") in allowed


PROFILE_RULES = """【用户档案规则（代码强制执行）】
1. 用户亲口说出的关于自己的稳定信息（称呼、上学/工作、学校/专业/年级、平时住处、生活规律、兴趣、边界）用 remember_profile_fact 保存；
   evidence 必须逐字引用用户原话。没有原话依据的推测只会成为“未确认候选”，不会被当作事实。
2. 用户纠正之前的信息用 correct_profile_fact，fact_id 填【已有的用户档案】里那一条的 id；返回 ambiguous 时从候选里选定
   fact_id 再调用，不能确定就不要纠正。说某条信息不对或已经不成立用 retract_profile_fact。
3. 只在此刻成立的状态（今天回家了、正在上课、在开会、准备睡了）用 set_situation 并给出有效期，不要写进档案。常住地不等于此刻的位置。
   一句话里说了几种此刻状态时分别记：在哪用 location；请假、不去某节课、接下来有空没空用 availability（例如“下午实验课请假了，没去”，有效期 today）。
   这只是这一次的安排：不要写进档案、长期记忆（不要再 save_memory 存一份），也不会改动课表。
4. 交流方式的明确要求（回复短一点、别老追问、不想要建议、别主动找我、别再提某件事）用 set_interaction_preference。
5. 用户表示不想回答某个了解类问题时，调用 decline_profile_question，之后不再问。
6. 健康、财务、宗教、政治、联系方式、证件号、精确住址等敏感信息不要保存（系统也会拒绝）。
7. Violet 回复里提到的 Violet 自己的生活（看书、听歌、记笔记等）是她的虚拟生活，不是用户的经历，不要保存成用户记忆或档案（系统也会拒绝）。
8. 网上查到的新闻、文章内容是外部资料，不是用户的经历，不要保存成用户记忆（系统也会拒绝）。"""


MAX_DECISION_PROMPTS = 3        # needs_decision answers per run before a save is refused (no endless loop)

# Phase 9.1.1 / 9.1.2: the user's own words asking Violet to forget something -- one definition, shared with the step
# that settles such a request before the reply (ai/memory_forget.py).
from ai.memory_forget import FORGET_REQUEST_RE  # noqa: E402


class EventDecisionLedger:
    """Similar-memory candidates shown to the model in ONE MemoryAgent run (phase 7 B.6.1).

    new_event=true only counts for candidates listed here with the same fingerprint, shown for the same
    summary: a candidate the model has not seen in this run, one that changed since, or a reworded summary
    is shown again instead of being skipped. A new ledger starts with every run (_new_tool_guard), so no
    other run, turn or retry can decide for it."""

    def __init__(self) -> None:
        self._decided: dict[str, dict[int, tuple]] = {}
        self.asked = 0

    @staticmethod
    def _key(summary: str) -> str:
        from database import VioletDatabase
        return VioletDatabase.normalize_text(summary)

    def decided_for(self, summary: str) -> dict[int, tuple]:
        return dict(self._decided.get(self._key(summary), {}))

    def show(self, summary: str, candidates: list[dict[str, Any]]) -> None:
        from database import VioletDatabase
        self.asked += 1
        shown = self._decided.setdefault(self._key(summary), {})
        for c in candidates:
            shown[c["id"]] = VioletDatabase.event_candidate_fingerprint(c)


def _memory_view(memory: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": memory["id"],
        "content": memory["content"],
        "category": memory.get("category"),
        "kind": memory.get("kind"),
        "progress": memory.get("progress"),
        "event_time": memory.get("event_time"),
        "valid_until": memory.get("valid_until"),
        "status": memory.get("status"),
        "memory_strength": round(float(memory.get("forget_weight") or 0), 2),
        "disabled_by_user": not memory.get("use_in_context", 1),
        **({"intent": memory["intent_level"]} if memory.get("intent_level") else {}),
        **({"condition": memory["intent_condition"]} if memory.get("intent_condition") else {}),
        "similarity": round(float(memory.get("similarity", 0) or 0), 2),
        **({"retrieval": memory["retrieval"]} if memory.get("retrieval") is not None else {}),
    }


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _details_note(memory: dict[str, Any]) -> dict[str, Any]:
    """Phase 9.1.1 (D-P91-1): dates / clock times the rewrite dropped and the system put back."""
    kept = memory.get("details_kept") or []
    return {"details_kept": kept, "content": memory.get("content"),
            "note": "新内容漏掉了原记录里的时间细节，系统已补回；以后改写时请保留它们。"} if kept else {}


def _time_mismatch(exc: Exception) -> str:
    return _dumps({"ok": False, "action": "time_mismatch", "reason": str(exc),
                   "instruction": "没有保存：内容里写的日期 / 时刻和记录的时间对不上。用户说时间改了，用 "
                                  "save_memory(related_to=<id>, event_date, event_clock) 一起改时间；否则按记录的时间改正内容再调用。"})


class MemoryAgent(BaseAgent):
    """
    记忆判断 agent。

    从 .env 读取提示词的环境变量：MEMORY_LLM_PROMPT
    """

    mcp_agent_name = "memory"
    model_env_key = "MEMORY_LLM_MODEL"
    default_model = "gpt-4o"
    system_prompt_env_key = "MEMORY_LLM_PROMPT"
    default_prompt_file = "persona/prompts/memory.txt"
    temperature_env_key = "MEMORY_LLM_MODEL_TEMPERATURE"
    default_system_prompt = (
        "You are a memory evaluation assistant. "
        "Analyze the conversation and determine whether it contains information "
        "worth storing as long-term memory. "
        "If yes, extract and summarize the key information in a structured format."
    )

    def __init__(self):
        super().__init__()
        self.tracker = TopicTracker(violetdb)
        self.profile = Profile(violetdb)
        self.onboarding = OnboardingPlanner(violetdb, self.profile.facts)

    def register_tools(self) -> None:
        """注册记忆相关工具。"""
        self.register_tool(schema=QUERY_MEMORY_SCHEMA, handler=self._tool_query_memory)
        self.register_tool(schema=SAVE_MEMORY_SCHEMA, handler=self._tool_save_memory)
        self.register_tool(schema=UPDATE_MEMORY_SCHEMA, handler=self._tool_update_memory)
        self.register_tool(schema=REINFORCE_MEMORY_SCHEMA, handler=self._tool_reinforce_memory)
        self.register_tool(schema=UPDATE_EVENT_PROGRESS_SCHEMA, handler=self._tool_update_event_progress)
        self.register_tool(schema=CORRECT_MEMORY_SCHEMA, handler=self._tool_correct_memory)
        self.register_tool(schema=RETRACT_MEMORY_SCHEMA, handler=self._tool_retract_memory)
        self.register_tool(schema=CREATE_COMMITMENT_SCHEMA, handler=self._tool_create_commitment)
        self.register_tool(schema=CANCEL_COMMITMENT_SCHEMA, handler=self._tool_cancel_commitment)
        self.register_tool(schema=REMEMBER_PROFILE_FACT_SCHEMA, handler=self._tool_remember_profile_fact)
        self.register_tool(schema=CORRECT_PROFILE_FACT_SCHEMA, handler=self._tool_correct_profile_fact)
        self.register_tool(schema=RETRACT_PROFILE_FACT_SCHEMA, handler=self._tool_retract_profile_fact)
        self.register_tool(schema=SET_SITUATION_SCHEMA, handler=self._tool_set_situation)
        self.register_tool(schema=SET_INTERACTION_PREFERENCE_SCHEMA, handler=self._tool_set_interaction_preference)
        self.register_tool(schema=DECLINE_PROFILE_QUESTION_SCHEMA, handler=self._tool_decline_profile_question)
        self.register_tool(schema=UPSERT_CORE_MEMORY_SCHEMA, handler=self._tool_upsert_core_memory)
        self.register_tool(schema=UPDATE_CORE_MEMORY_SCHEMA, handler=self._tool_update_core_memory)
        self.register_tool(schema=REINFORCE_CORE_MEMORY_SCHEMA, handler=self._tool_reinforce_core_memory)
        self.register_tool(schema=MEMORY_DONE_SCHEMA, handler=self._tool_memory_done)

    # ------------------------------------------------------------------
    # Phase 7 B.2: end a background job right after its successful writes
    # ------------------------------------------------------------------

    def _finish_signal_offered(self) -> bool:
        """Only background jobs (nobody reads the final text) and only while the switch is on."""
        return bool(self._call_context.get("background_job")) and env_bool("VIOLET_MEMORY_FINISH_SIGNAL", True)

    def _filter_tools_for_run(self, tools):
        hidden = set()
        if not self._finish_signal_offered():
            hidden.add(DONE_TOOL)
        if self._nothing_to_search():
            hidden.add("query_memory")       # phase 9.4 (r1): an empty store cannot answer a duplicate check
        return [t for t in tools if t.get("function", {}).get("name") not in hidden]

    def _nothing_to_search(self) -> bool:
        """query_memory searches event memories (disabled ones included): with none at all, a lookup can only come back
        empty and costs a whole model round (r1: 7284 tokens for "memories": []). Same switch idea as Intent's."""
        if not env_bool("VIOLET_MEMORY_HIDE_EMPTY_QUERY_TOOL", True):
            return False
        cached = self._call_context.get("_nothing_to_search")
        if cached is None:
            cached = self._call_context["_nothing_to_search"] = not violetdb.has_searchable_event_memories(
                include_disabled=True)
        return cached

    def _record_writes(self, step: list[dict[str, Any]]) -> None:
        """Phase 9.4 (r1): every write that really succeeded, for the memory job's progress (MemoryWorker passes the
        list in call_context["write_log"]); a run that stops later still leaves an exact account of what it did."""
        log = self._call_context.get("write_log")
        if not isinstance(log, list):
            return
        for s in step:
            if s["denied"] or s["name"] == DONE_TOOL or not write_step_succeeded(s["name"], s["result"]):
                continue
            payload = json.loads(s["result"])
            ref = {k: v for k, v in payload.items() if k.endswith("_id") and v is not None}
            log.append({"tool": s["name"], "action": payload.get("action"), **ref})

    def _should_finish_after_tools(self, step: list[dict[str, Any]]) -> bool:
        self._record_writes(step)
        if not self._finish_signal_offered() or not step:
            return False
        if not any(s["name"] == DONE_TOOL and not s["denied"] for s in step):
            return False                    # the model did not say it is done
        for s in step:
            if s["denied"]:
                return False
            if s["name"] == DONE_TOOL:
                continue
            if not write_step_succeeded(s["name"], s["result"]):
                return False                # a read, a failure, a rejection or something to decide
        return True

    def _new_tool_guard(self, user_message: str):
        # every run starts with nothing shown: decisions never carry over from another run, turn or retry
        self._call_context["event_decisions"] = EventDecisionLedger()
        return None

    def _decision_ledger(self) -> EventDecisionLedger:
        ledger = self._call_context.get("event_decisions")
        if not isinstance(ledger, EventDecisionLedger):
            ledger = self._call_context["event_decisions"] = EventDecisionLedger()
        return ledger

    def _previous_writes_note(self) -> str:
        """Phase 9.4 (r1): a retried job is told what its earlier attempt already wrote."""
        items = "；".join(f"{w.get('tool')}（{w.get('action')}"
                         + "".join(f"，{k}={v}" for k, v in w.items() if k.endswith("_id")) + "）"
                         for w in self._call_context.get("previous_writes") or [])
        return ("【这一轮之前的尝试已经成功写入】" + items + "。不要再重复这些写入；只处理这一轮还没处理的部分"
                "（比如还没记下的另一种此刻状态）。全部都已处理好，就直接调用 memory_done 或直接回复。")

    def _tool_memory_done(self, args: dict) -> str:
        return _dumps({"ok": True, "action": "done_requested",
                       "note": "同一次回复里如果有写入失败、被拒绝、需要选择或待确认，系统会继续，请根据那些结果处理。"})

    # ------------------------------------------------------------------
    # Context of the current run
    # ------------------------------------------------------------------

    def _turn_id(self) -> str | None:
        return self._call_context.get("turn_id")

    def _user_text(self) -> str:
        text = self._call_context.get("user_message")
        if text:
            return text
        raw = _CURRENT_INPUT.get()
        match = re.match(r"用户说：(.*?)(?:\nViolet 实际回复：.*)?$", raw, flags=re.S)
        return match.group(1) if match else raw

    def _resolve_event_time(self, args: dict) -> str | None:
        # phase 9.1.1 (D-P91-1): a day without a clock stays "YYYY-MM-DD" -- no 23:59 is made up
        value = resolve_event_time(args.get("event_time"), args.get("event_date"), args.get("event_clock"))
        if invented_part_clock(value, self._user_text(), args.get("summary")):
            # phase 9.2.1: the user said "下午", the model wrote 15:00 -- keep the day, the words stay in the text
            parsed = parse_time(value)
            return parsed.date().isoformat() if parsed else value
        return value

    def _settle_guard(self, memory_id: int, progress: str | None, evidence: str | None) -> str | None:
        """Phase 9.2.1: a conditional / wished event becomes a plan (planned / ongoing / done) only on the user's own
        words of this turn -- not on Violet's reply, a tool result, or a turn where Violet's reply failed."""
        if progress not in ("planned", "ongoing", "done"):
            return None
        memory = violetdb.get_event_memory(memory_id) or {}
        level = memory.get("intent_level")
        if level not in ("conditional", "wish") or self._users_words(evidence):
            return None
        return _dumps({"ok": False, "action": "rejected", "reason": "condition_not_confirmed", "memory_id": memory_id,
                       "intent": level, "condition": memory.get("intent_condition"),
                       "note": "这是" + INTENT_LABELS[level] + "。只有用户本轮亲口说条件满足了 / 定下来了才能改成计划，"
                               "evidence 逐字引用那句话；Violet 的回复、工具结果、Violet 没接上的兜底消息都不算。"
                               "用户说去不了 / 条件不成立，就改成 cancelled。"})

    def _user_message_id(self) -> int | None:
        value = self._call_context.get("user_message_id")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _users_words(self, evidence: str | None) -> bool:
        """Phase 9.1 (A7): evidence is a real quote (4+ characters) from the user's message of this turn."""
        return len(re.sub(r"\s+", "", evidence or "")) >= 4 and evidence_in_message(evidence, self._user_text())

    def _confirmed_guard(self, table: str, memory_id: int, evidence: str | None) -> str | None:
        """None when the content change may go ahead; else the rejection (phase 9.1, A7).

        A memory the user confirmed -- corrected in the dashboard, or corrected in chat with their own words -- is not
        rewritten from a vague turn: its content only changes with the user's words of this turn, quoted verbatim."""
        if not violetdb.confirmed_by_user(table, memory_id) or self._users_words(evidence):
            return None
        return _dumps({"ok": False, "action": "rejected", "reason": "confirmed_by_user", "memory_id": memory_id,
                       "note": "这条记忆是用户确认过的（管理后台纠正过，或用户亲口纠正过），内容只能凭用户本轮原话修改："
                               "在 evidence 里逐字引用那句话。只是事件有了新进展，就用 update_event_progress 只改进展。"})

    # ------------------------------------------------------------------
    # 工具处理函数
    # ------------------------------------------------------------------

    def _tool_query_memory(self, args: dict) -> dict:
        """查询已有记忆。检索只做记录，不改变记忆权重。"""
        query = recall_search_text(args.get("query", ""))
        if not query:
            return {"memories": [], "note": "query 为空"}
        violetdb.refresh_event_states()
        memories = violetdb.search_event_memories(
            query=query,
            category=args.get("category"),
            limit=int(args.get("limit", 8)),
            min_similarity=float(args.get("min_similarity", 0.25)),
            include_disabled=True,
            recall=True,
        )
        return {
            "note": "similarity 越大表示越相关；progress 是事件进展；status=expired 表示已过有效期；"
                    "disabled_by_user=true 表示用户禁止使用，不要再保存或引用。",
            "memories": [_memory_view(m) for m in memories],
        }

    def _tool_save_memory(self, args: dict) -> str:
        summary = (args.get("summary") or "").strip()
        if not summary:
            return _dumps({"ok": False, "error": "summary 为空"})
        if self._is_eves_life(summary):
            return _dumps({"ok": False, "action": "rejected", "reason": "violet_virtual_life",
                           "note": "这是 Violet 自己的虚拟生活，不是用户的经历，不能保存为用户记忆。"})
        if self._is_external_info(summary):
            return _dumps({"ok": False, "action": "rejected", "reason": "external_info",
                           "note": "这是网页上的外部资料，不是用户的经历，不能保存为用户记忆。"})
        if self._is_timetable_info(summary):
            return _dumps({"ok": False, "action": "rejected", "reason": "timetable",
                           "note": "这是用户课表里已有的课程 / 考试安排（课表是唯一来源），不要再存成记忆；"
                                   "用户自己说的新情况（比如说某节课停了）照常保存。"})
        kind = args.get("kind") or "event"
        if kind not in KIND_LABELS or kind == "legacy":
            kind = "event"
        progress = args.get("progress")
        if progress is not None and progress not in EVENT_PROGRESS:
            return _dumps({"ok": False, "error": f"invalid progress {progress!r}", "allowed": sorted(EVENT_PROGRESS)})
        if kind == "event" and progress == "na":
            progress = None
        # phase 9.2.1: plan / conditional / wish. A planned event the user only stated with a condition ("要是没课就去")
        # is conditional even when the model forgot to say so: both the user's words and its own summary carry it.
        level = args.get("intent") if args.get("intent") in INTENT_LEVELS else None
        condition = (args.get("condition") or "").strip()[:60] or None
        inferred = False
        if level is None and kind == "event" and progress == "planned" and sounds_conditional(self._user_text(), summary):
            level, inferred = "conditional", True
        if level == "conditional":
            condition = condition or condition_clause(summary, self._user_text())
        if level in ("conditional", "wish") and progress in (None, "planned"):
            progress = "unknown"
        # phase 9.1.1: no progress given -> None. A new event still starts as "unknown" (save_event_memory), but an
        # update of an existing one (related_to / event_key) keeps its progress instead of being reset to "unknown".
        if args.get("related_to") is not None:
            try:
                rejected = (self._confirmed_guard("event_memories", int(args["related_to"]), args.get("evidence"))
                            or self._settle_guard(int(args["related_to"]), "planned" if level == "plan" else progress,
                                                  args.get("evidence")))
            except (TypeError, ValueError):
                return _dumps({"ok": False, "error": "related_to 无效"})
            if rejected:
                return rejected
        ledger = self._decision_ledger()
        try:
            result = violetdb.save_event_memory(
                content=summary,
                category=args.get("category", "general"),
                kind=kind,
                progress=progress,
                happened_at=args.get("happened_at"),
                event_time=self._resolve_event_time(args),
                valid_until=args.get("valid_until"),
                importance=float(args.get("importance", 1.0)),
                emotional_weight=float(args.get("emotional_weight", 1.0)),
                event_key=args.get("event_key"),
                related_to=args.get("related_to"),
                new_event=bool(args.get("new_event", False)),
                source_turn_id=self._turn_id(),
                decided=ledger.decided_for(summary),
                source_message_id=self._user_message_id(),
                preserve_details=self._user_text(),
            )
        except EventTimeMismatch as exc:
            return _time_mismatch(exc)
        except ValueError as exc:
            return _dumps({"ok": False, "error": str(exc)})
        action = result["action"]
        if action == "needs_decision":
            if ledger.asked >= MAX_DECISION_PROMPTS:
                # the candidates keep changing under this run: do not write, do not loop
                return _dumps({"ok": False, "action": "rejected", "reason": "decision_not_settled",
                               "note": "相似记忆在本轮里一直在变化，这条先不保存，本轮不要再重试。"})
            ledger.show(summary, result["candidates"])
            return _dumps({
                "ok": False,
                "action": "needs_decision",
                "candidates": result["candidates"],
                "undecided_ids": result.get("undecided_ids", []),
                "instruction": "已有可能是同一件事的记忆。若这是其中某件事的新进展/结果（结束、取消、改期等），"
                               "用 update_event_progress(memory_id=<id>) 或 related_to=<id> 重新调用 save_memory；"
                               "确认是另一件事，才传 new_event=true。new_event 只对这里列出的候选有效。不会自动合并。",
            })
        memory = result["memory"] or {}
        if level and memory.get("id") and action in ("created", "updated", "updated_by_key"):
            violetdb.set_event_intent(memory["id"], level, condition if level == "conditional" else None)
        if action in ("updated", "updated_by_key", "reinforced") and memory.get("id"):
            self.tracker.record_user_topic(memory_id=memory["id"])
        return _dumps({
            "ok": True,
            "action": action,
            "memory_id": memory.get("id"),
            "kind": memory.get("kind"),
            "progress": memory.get("progress"),
            "event_time": memory.get("event_time"),
            **({"intent": level, "condition": condition} if level else {}),
            **({"note": "用户的话带条件，已记为有条件的打算（progress=unknown）；条件满足要等用户亲口确认。"} if inferred else {}),
            **_details_note(memory),
        })

    def _is_eves_life(self, text: str) -> bool:
        """Text about Violet's recorded virtual life that the user did not say themselves."""
        if mentions_topic(self._user_text(), text):
            return False
        events = violetdb.fetch_all("SELECT content FROM life_events WHERE status = 'happened'")
        return any(mentions_topic(text, e["content"]) for e in events)

    def _is_external_info(self, text: str) -> bool:
        """Text that comes from a web finding and that the user did not say themselves (phase 5B)."""
        if mentions_topic(self._user_text(), text):
            return False
        rows = violetdb.fetch_all("SELECT title, summary FROM web_findings WHERE status = 'active' ORDER BY id DESC LIMIT 50")
        return any(touches_topic(text, r["title"]) or (r["summary"] and touches_topic(text, r["summary"])) for r in rows)

    def _is_timetable_info(self, text: str) -> bool:
        """Phase 9.4.2: text about a course / exam of the user's timetable that only the timetable (through Violet's
        reply) said -- not the user. The timetable is the one source; it is never copied into event memories."""
        from ai.settings import env_bool
        if not env_bool("VIOLET_SCHEDULE_ENABLED", False):
            return False
        # only whether the USER named the course / exam counts: "明天上午有课吗" shares 明天 / 上午 with "用户明天上午有
        # 高等数学课" but the course came from the timetable (an abbreviation like 高数 counts as naming it)
        user = self._user_text()
        try:
            from ai.schedule.access import mentions_course
            from ai.schedule.store import ScheduleStore
            names = ScheduleStore(violetdb).known_names()
        except Exception:
            import logging
            logging.getLogger("violet.memory").exception("timetable names unreadable; saved without the timetable check")
            return False
        return any(mentions_course(text, n) and not mentions_course(user, n) for n in names)

    def _tool_update_memory(self, args: dict) -> str:
        """补充已有记忆的内容（不会仅因更新而提高权重）。"""
        try:
            memory_id = int(args.get("memory_id"))
        except (TypeError, ValueError):
            return _dumps({"ok": False, "error": "memory_id 无效"})
        summary = args.get("summary", "")
        if not summary:
            return _dumps({"ok": False, "error": "summary 为空"})
        rejected = self._confirmed_guard("event_memories", memory_id, args.get("evidence"))
        if rejected:
            return rejected
        try:
            memory = violetdb.apply_event_update(
                memory_id, content=summary,
                importance=float(args["importance"]) if args.get("importance") is not None else None,
                reason=args.get("reason"), source="user", source_turn_id=self._turn_id(), record_user_mention=True,
                source_message_id=self._user_message_id(), preserve_details=self._user_text(),
            )
        except EventTimeMismatch as exc:
            return _time_mismatch(exc)
        except ValueError as exc:
            return _dumps({"ok": False, "error": str(exc)})
        if args.get("category"):
            violetdb.update_event_memory(memory_id, category=args["category"])
        self.tracker.record_user_topic(memory_id=memory_id)
        return _dumps({"ok": True, "action": "updated", "memory_id": memory_id, "mention_count": memory.get("mention_count"),
                       **_details_note(memory)})

    def _tool_reinforce_memory(self, args: dict) -> str:
        """用户再次提到已有记忆且无新信息：记录被提及，不额外加权。"""
        try:
            memory_id = int(args.get("memory_id"))
            memory = violetdb.record_user_mention(memory_id)
        except (TypeError, ValueError) as exc:
            return _dumps({"ok": False, "error": str(exc)})
        self.tracker.record_user_topic(memory_id=memory_id)
        return _dumps({"ok": True, "action": "reinforced", "memory_id": memory_id, "mention_count": memory.get("mention_count")})

    def _tool_update_event_progress(self, args: dict) -> str:
        try:
            memory_id = int(args.get("memory_id"))
        except (TypeError, ValueError):
            return _dumps({"ok": False, "error": "memory_id 无效"})
        progress = args.get("progress")
        if progress not in EVENT_PROGRESS or progress == "na":
            return _dumps({"ok": False, "error": f"invalid progress {progress!r}"})
        content = args.get("summary")
        unsettled = self._settle_guard(memory_id, progress, args.get("evidence"))
        if unsettled:
            return unsettled
        settles = (violetdb.get_event_memory(memory_id) or {}).get("intent_level") in ("conditional", "wish") \
            and progress in ("planned", "ongoing", "done")
        # phase 9.1 (A7): progress of a user-confirmed memory may change; its wording only with the user's words
        content_kept = bool(content) and self._confirmed_guard("event_memories", memory_id, args.get("evidence")) is not None
        try:
            memory = violetdb.update_event_progress(
                memory_id, progress, content=None if content_kept else content, reason=args.get("reason"),
                source="user", source_turn_id=self._turn_id(), source_message_id=self._user_message_id(),
                preserve_details=self._user_text(),
            )
        except EventTimeMismatch as exc:
            return _time_mismatch(exc)
        except ValueError as exc:
            return _dumps({"ok": False, "error": str(exc)})
        if settles:
            violetdb.set_event_intent(memory_id, "plan", None)      # the user settled it in their own words
        self.tracker.record_user_topic(memory_id=memory_id)
        return _dumps({"ok": True, "action": "progress_updated", "memory_id": memory_id, "progress": memory["progress"],
                       "content": memory["content"], **_details_note(memory),
                       **({"content_kept": "用户确认过的内容没有改，只更新了进展"} if content_kept else {})})

    def _tool_correct_memory(self, args: dict) -> str:
        try:
            memory_id = int(args.get("memory_id"))
        except (TypeError, ValueError):
            return _dumps({"ok": False, "error": "memory_id 无效"})
        corrected = (args.get("corrected_summary") or "").strip()
        if not corrected:
            return _dumps({"ok": False, "error": "corrected_summary 为空"})
        progress = args.get("progress")
        if progress is not None and progress not in EVENT_PROGRESS:
            return _dumps({"ok": False, "error": f"invalid progress {progress!r}"})
        rejected = (self._confirmed_guard("event_memories", memory_id, args.get("evidence"))
                    or self._settle_guard(memory_id, progress, args.get("evidence")))
        if rejected:
            return rejected
        # the user's own words of this turn make the corrected content confirmed (phase 9.1)
        source = "user_confirmed" if self._users_words(args.get("evidence")) else "user_correction"
        before = violetdb.get_event_memory(memory_id)
        try:
            memory = violetdb.correct_event_memory(memory_id, corrected, reason=args.get("reason"),
                                                progress=progress, source_turn_id=self._turn_id(), source=source,
                                                source_message_id=self._user_message_id())
        except ValueError as exc:
            return _dumps({"ok": False, "error": str(exc)})
        self.tracker.record_user_topic(memory_id=memory_id)
        payload = {"ok": True, "action": "corrected", "memory_id": memory_id, "content": memory["content"]}
        if before and before.get("kind") == "fact":
            # phase 9.1.2 (D-P911-R1): the same fact stored as a profile fact / another memory follows the correction
            from ai.memory_consistency import propagate_correction
            report = propagate_correction(violetdb, before["content"], memory["content"], source=source,
                                          origin=f"记忆 {memory_id}", turn_id=self._turn_id(), exclude_memory_id=memory_id,
                                          profile=self.profile.facts, evidence=args.get("evidence"),
                                          user_message=self._user_text())
            payload.update(also_corrected_memory_ids=[u["id"] for u in report["updated"]],
                           conflicts_recorded=report["conflicts"], profile_corrections=report["profile"])
        return _dumps(payload)

    def _tool_retract_memory(self, args: dict) -> str:
        try:
            memory_id = int(args.get("memory_id"))
            if args.get("forget") is True:
                return self._forget(memory_id, args.get("evidence"))
            rejected = self._confirmed_guard("event_memories", memory_id, args.get("evidence"))
            if rejected:
                return rejected
            violetdb.retract_event_memory(memory_id, reason=args.get("reason"), source_turn_id=self._turn_id())
        except (TypeError, ValueError) as exc:
            return _dumps({"ok": False, "error": str(exc)})
        return _dumps({"ok": True, "action": "retracted", "memory_id": memory_id})

    def _forget(self, memory_id: int, evidence: str | None) -> str:
        """Phase 9.1.1: forget (the memory and the user's words about it in its source turn) only on the user's own,
        explicit request in this turn -- quoted verbatim. "你记错了" is a correction, not a request to forget.
        Phase 9.1.2: when the request was already settled before the reply, that outcome stands (the user has been told
        it): nothing more is deleted behind the reply's back, and nothing is undone."""
        from ai.memory_forget import settled_forget
        settled = settled_forget(violetdb, self._turn_id())
        if settled is not None:
            return _dumps({"ok": True, "action": "already_settled", "status": settled["status"],
                           "note": "这一轮的遗忘请求在回复前已经处理过，结果已经告诉用户；不要再删除或改动其他记忆。"})
        if not (self._users_words(evidence) and FORGET_REQUEST_RE.search(evidence or "")):
            return _dumps({"ok": False, "action": "rejected", "reason": "forget_needs_user_request",
                           "note": "只有用户原话明确要求忘掉 / 别记 / 删掉时才能 forget=true，evidence 逐字引用那句话；"
                                   "只是说记错了或不对，用 correct_memory / retract_memory（不带 forget）。"})
        if not violetdb.get_event_memory(memory_id):
            return _dumps({"ok": False, "error": f"memory {memory_id} not found"})
        from ai.memory_forget import forget_event_memory
        report = forget_event_memory(violetdb, memory_id, actor="memory_agent", turn_id=self._turn_id())
        return _dumps({"ok": True, "action": "forgotten", "memory_id": memory_id,
                       "messages_changed": len(report["messages_changed"])})

    def _settled_before_reply(self) -> str | None:
        """Phase 7 B.10.1: a "提醒我…" request is settled before the reply (ai.chat.settle_reminder_request) and
        the reply told the user the result. This run then never adds a second reminder (the one saved for this
        turn, or the same pending one it found) and never sets one the user was told could not be set (the
        reasons come from the words alone, so they are recomputed here). A readable request with nothing saved
        for this turn (the step before the reply did not run) falls through to the usual rules."""
        from ai.memory_model import parse_reminder_request, reminder_before_reply_enabled
        if not reminder_before_reply_enabled():
            return None
        parsed = parse_reminder_request(self._user_text())
        if not parsed["requested"]:
            return None
        turn_id = self._turn_id()
        row = (violetdb.fetch_one("SELECT id, due_at FROM commitments WHERE source_turn_id = ? ORDER BY id LIMIT 1",
                               (turn_id,)) if turn_id else None)
        if row is None and parsed["ok"]:
            row = violetdb.fetch_one("SELECT id, due_at FROM commitments WHERE status IN ('pending', 'sending') "
                                  "AND content = ? AND due_at = ? ORDER BY id LIMIT 1", (parsed["content"], parsed["due_at"]))
        if row:
            return _dumps({"ok": True, "action": "already_saved", "commitment_id": row["id"], "due_at": row["due_at"],
                           "note": "这个提醒在回复前已经保存好了，不要再创建。"})
        if not parsed["ok"]:
            return _dumps({"ok": False, "action": "rejected", "reason": "settled_before_reply",
                           "note": "这一轮的提醒在回复前已经处理过（没能设置，用户已被告知），不要再创建。"})
        return None

    def _tool_create_commitment(self, args: dict) -> str:
        settled = self._settled_before_reply()
        if settled is not None:
            return settled
        ok, reason = check_explicit_request(args.get("evidence"), self._user_text())
        if not ok:
            return _dumps({"ok": False, "action": "rejected", "reason": reason,
                           "note": "只有用户原话明确要求提醒/跟进时才能创建；普通事件只需要 save_memory。"})
        content = (args.get("content") or "").strip()
        if not content:
            return _dumps({"ok": False, "error": "content 为空"})
        due_at = resolve_time(args.get("due_time"), args.get("due_date"), args.get("due_clock"),
                              default_time=commitment_default_time())
        if not due_at:
            return _dumps({"ok": False, "error": "需要提醒时间：due_time（ISO）或 due_date + due_clock"})
        if parse_time(due_at) < parse_time(violetdb.now()):
            return _dumps({"ok": False, "error": f"提醒时间 {due_at} 已经过去"})
        kind = args.get("kind") if args.get("kind") in ("reminder", "follow_up") else "reminder"
        result = violetdb.add_commitment(content, due_at, kind=kind, evidence=args.get("evidence"),
                                      source_turn_id=self._turn_id())
        commitment = result["commitment"]
        return _dumps({"ok": True, "action": result["action"], "commitment_id": commitment["id"], "due_at": commitment["due_at"]})

    def _tool_cancel_commitment(self, args: dict) -> str:
        evidence = re.sub(r"\s+", "", args.get("evidence") or "")
        if not evidence or evidence not in re.sub(r"\s+", "", self._user_text()):
            return _dumps({"ok": False, "action": "rejected", "reason": "evidence_not_in_user_message"})
        try:
            commitment_id = int(args.get("commitment_id"))
        except (TypeError, ValueError):
            return _dumps({"ok": False, "error": "commitment_id 无效"})
        commitment = violetdb.get_commitment(commitment_id)
        if not commitment or commitment["status"] != "pending":
            return _dumps({"ok": False, "error": "commitment 不存在或已不是待提醒状态"})
        violetdb.update_commitment(commitment_id, status="cancelled", last_error="cancelled by user")
        return _dumps({"ok": True, "action": "cancelled", "commitment_id": commitment_id})

    # -- phase 3: profile / situation / preferences -------------------------------
    @staticmethod
    def _fact_result(result: dict[str, Any]) -> str:
        fact = result.get("fact") or {}
        return _dumps({
            "ok": result["action"] not in ("rejected", "not_stored"),
            "action": result["action"],
            "reason": result.get("reason"),
            "fact_id": fact.get("id"),
            "status": fact.get("status"),
        })

    def _tool_remember_profile_fact(self, args: dict) -> str:
        if self._is_eves_life(args.get("value", "")):
            return _dumps({"ok": False, "action": "rejected", "reason": "violet_virtual_life"})
        if self._is_external_info(args.get("value", "")):
            return _dumps({"ok": False, "action": "rejected", "reason": "external_info"})
        return self._fact_result(self.profile.facts.remember(
            args.get("key", ""), args.get("value", ""), evidence=args.get("evidence"), user_message=self._user_text(),
            source="user_chat", source_turn_id=self._turn_id(), source_message_id=self._call_context.get("user_message_id"),
            valid_until=args.get("valid_until"),
        ))

    def _tool_correct_profile_fact(self, args: dict) -> str:
        """Phase 9.1.1 (D-P91-2): the old fact is named by fact_id (or found from a part of old_value); when it is not
        certain which one, nothing is written and the candidates come back -- never a silent second active fact."""
        try:
            fact_id = int(args["fact_id"]) if args.get("fact_id") not in (None, "") else None
        except (TypeError, ValueError):
            return _dumps({"ok": False, "action": "failed", "reason": "fact_id_invalid"})
        result = self.profile.facts.correct(
            args.get("key", ""), args.get("new_value", ""), fact_id=fact_id, old_value=args.get("old_value"),
            evidence=args.get("evidence"), user_message=self._user_text(), source="user_chat",
            source_turn_id=self._turn_id(), source_message_id=self._call_context.get("user_message_id"),
        )
        fact = result.get("fact") or {}
        payload: dict[str, Any] = {
            "ok": result["action"] in ("corrected", "created", "confirmed"),
            "action": result["action"],
            "reason": result.get("reason"),
            "fact_id": fact.get("id"),
            "status": fact.get("status"),
            "superseded_ids": result.get("superseded") or [],
        }
        propagated = result.get("propagated") or {}
        if propagated.get("updated") or propagated.get("conflicts"):
            # phase 9.1.2: the same fact in other memories was corrected too (or flagged when it is an event record)
            payload["also_corrected_memory_ids"] = [u["id"] for u in propagated["updated"]]
            payload["conflicts_recorded"] = propagated["conflicts"]
            payload["note"] = "其他记忆里同一事实的旧说法已一并改正；事件记录只标记了冲突，没有改写，不要再手动修改它们。"
        if result["action"] == "ambiguous":
            payload["candidates"] = result.get("candidates") or []
            payload["instruction"] = ("没有纠正任何档案：不能确定要改哪一条。从 candidates 里选出用户说的那一条，"
                                      "用它的 id 作为 fact_id 再调用一次；都不是就不要纠正。")
        elif result["action"] == "failed":
            payload["instruction"] = "没有纠正任何档案。"
        return _dumps(payload)

    def _tool_retract_profile_fact(self, args: dict) -> str:
        return _dumps(self.profile.facts.retract(args.get("key", ""), evidence=args.get("evidence"),
                                                 user_message=self._user_text(), value=args.get("value")))

    def _tool_set_situation(self, args: dict) -> str:
        result = self.profile.situations.observe(
            args.get("kind", ""), args.get("value", ""), "user_stated", evidence=args.get("evidence"),
            user_message=self._user_text(), until=args.get("until"), turn_id=self._turn_id(),
        )
        situation = result.get("situation") or {}
        return _dumps({"ok": result["action"] in ("created", "already_set"), "action": result["action"],
                       "reason": result.get("reason"), "situation_id": situation.get("id"),
                       "expires_at": situation.get("expires_at")})

    def _tool_set_interaction_preference(self, args: dict) -> str:
        return _dumps(self.profile.preferences.set_explicit(
            args.get("key", ""), args.get("value", ""), evidence=args.get("evidence"), user_message=self._user_text(),
            turn_id=self._turn_id(),
        ))

    def _tool_decline_profile_question(self, args: dict) -> str:
        if not evidence_in_message(args.get("evidence"), self._user_text()):
            return _dumps({"ok": False, "action": "rejected", "reason": "evidence_not_in_user_message"})
        self.onboarding.decline(args.get("key", ""))
        return _dumps({"ok": True, "action": "declined", "key": args.get("key")})

    def _tool_upsert_core_memory(self, args: dict) -> str:
        key = args.get("memory_key", "general")
        before = violetdb.get_core_memory_by_key(key)
        if before and (args.get("content") or "") != before["content"]:
            rejected = self._confirmed_guard("core_memories", before["id"], args.get("evidence"))
            if rejected:
                return rejected
        result = violetdb.upsert_core_memory(
            memory_key=key,
            category=args.get("category", "general"),
            content=args.get("content", ""),
            priority=int(args.get("priority", 3)),
            confidence=float(args.get("confidence", 1.0)),
        )
        memory = result.get("memory") or {}
        if before and memory.get("content") != before["content"]:
            # phase 9.1 (A7): an overwrite by key used to leave no trace of the old text
            violetdb.add_memory_revision("core_memories", before["id"], "update", old_content=before["content"],
                                      new_content=memory.get("content"), reason=args.get("reason"),
                                      source="memory_agent", source_turn_id=self._turn_id())
        return _dumps({"ok": True, "action": result.get("action"), "memory_id": memory.get("id")})

    def _tool_update_core_memory(self, args: dict) -> str:
        memory_id = int(args.get("memory_id"))
        before = violetdb.get_core_memory(memory_id)
        if before and args.get("content") and args["content"] != before["content"]:
            rejected = self._confirmed_guard("core_memories", memory_id, args.get("evidence"))
            if rejected:
                return rejected
        memory = violetdb.update_core_memory(
            memory_id=memory_id,
            content=args.get("content"),
            category=args.get("category"),
            memory_key=args.get("memory_key"),
            priority=args.get("priority"),
            confidence=args.get("confidence"),
        )
        violetdb.add_memory_revision("core_memories", memory_id, "update",
                                  old_content=(before or {}).get("content"), new_content=memory.get("content"),
                                  reason=args.get("reason"), source="memory_agent", source_turn_id=self._turn_id())
        return _dumps({"ok": True, "action": "updated", "memory_id": memory.get("id")})

    def _tool_reinforce_core_memory(self, args: dict) -> str:
        memory = violetdb.reinforce_core_memory(int(args.get("memory_id")))
        return _dumps({"ok": True, "action": "reinforced", "memory_id": memory.get("id"),
                       "mention_count": memory.get("mention_count")})

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    @staticmethod
    def _format_open_events(limit: int = 15) -> str:
        events = violetdb.list_recent_event_memories(kinds=["event", "legacy"], include_ended=False, limit=limit)
        if not events:
            return "未结束的事件：无"
        lines = ["未结束的事件（用户提到它们的新进展或结果时，直接用这里的 id 调用 update_event_progress 更新，"
                 "不必先 query_memory；不要新建）："]
        for e in events:
            intent = INTENT_LABELS.get(e.get("intent_level") or "")
            lines.append(
                f"- id={e['id']}; {PROGRESS_LABELS.get(e.get('progress'), e.get('progress'))}; "
                + (f"{intent}（条件：{e.get('intent_condition') or '见内容'}）; " if e.get("intent_level") == "conditional"
                   else f"{intent}; " if intent else "")
                + f"时间={e.get('event_time') or '未知'}; 内容={e['content']}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_pending_commitments() -> str:
        rows = violetdb.list_commitments(["pending"], limit=10)
        if not rows:
            return "待执行的提醒：无"
        return "待执行的提醒（用户取消时用 cancel_commitment）：\n" + "\n".join(
            f"- id={r['id']}; 时间={r['due_at']}; 内容={r['content']}" for r in rows
        )

    def build_context_messages(
        self,
        user_message: str,
        recent_limit: int = 10,
    ) -> list[dict[str, Any]]:
        """不自动注入相关记忆，由 MemoryAgent 自己按关键词调用 query_memory。"""
        _CURRENT_INPUT.set(user_message)
        violetdb.refresh_event_states()
        self._call_context.pop("_nothing_to_search", None)     # decided afresh for every run, then kept for its tools
        recent_messages = format_recent_messages(10)
        profile_sections = ContextEngine(violetdb).memory_agent_sections()
        return [
            {"role": "system", "content": self._config.reply_prompt},
            {"role": "system", "content": MEMORY_RULES},
            {"role": "system", "content": PROFILE_RULES},
            {"role": "system", "content": f"当前时间：{violetdb.now()}"},
            {"role": "system", "content": f'已有的核心记忆，切勿重复存储：{format_core_memories()}'},
            *({"role": "system", "content": section} for section in profile_sections),
            {"role": "system", "content": self._format_open_events()},
            {"role": "system", "content": self._format_pending_commitments()},
            {
                "role": "system",
                "content": ("记忆库里还没有任何事件记忆，不会有重复，不需要查询：直接按这一轮的内容决定要保存什么。"
                            if self._nothing_to_search() else
                            "如需判断是否与已有记忆重复或应合并，请先用 query_memory 主动查询关键词、同义词、上位词和相关概念。"
                            "例如：小狗/狗/犬类/宠物。"),
            },
            *([{"role": "system", "content": FINISH_RULE}] if self._finish_signal_offered() else []),
            *([{"role": "system", "content": self._previous_writes_note()}] if self._call_context.get("previous_writes")
              else []),
            # Phase 3: history is reference data for this analysis, not a system instruction.
            {"role": "user", "content": f"【参考资料：最近的聊天记录，只用于理解本轮，不要重复处理旧内容】\n{recent_messages}"},
            {"role": "user", "content": user_message},
        ]
