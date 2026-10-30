import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from database import VioletDatabase
from dotenv import load_dotenv
from ai.debug_visualizer import visualize_pipeline_start, visualize_pipeline_end, visualize_step
import ai.pipeline_state as _ps
from ai.settings import ENV_PATH, env_bool, env_str
from ai.chat_output import NO_REPLY_TOKEN, normalize_chat_output, strip_images
from ai.conversation_state import TopicTracker, normalize_topic_key
from ai.memory_model import EVENT_KINDS, KIND_LABELS, PROGRESS_LABELS, event_time_label, is_date_only, touches_topic

violetdb = VioletDatabase()
logger = logging.getLogger("violet.pipeline")

# Load .env from project root (same directory as main.py); VIOLET_ENV_FILE overrides.
_ENV_PATH = ENV_PATH
load_dotenv(_ENV_PATH)


# ---------------------------------------------------------------------------
# LLM configuration
# ---------------------------------------------------------------------------

def get_llm_api_key() -> str:
    key = os.getenv("LLM_API_KEY", "")
    if not key:
        raise ValueError("LLM_API_KEY is not set in .env")
    return key


def get_llm_api_url() -> str:
    return os.getenv("LLM_API_URL", "https://api.openai.com/v1")


def get_mcp_servers(apply_settings: bool = True) -> list[dict[str, str]]:
    """Every MCP server Violet knows: MCP_SERVERS, the servers added in the Control Center, the built-in ones -- with the
    Control Center's settings applied (phase 9.2: enabled, agents, timeouts, switched-off tools, replaced headers).
    apply_settings=False: as configured (agents keep that and apply the settings at the start of every run, so a
    switch in the Control Center acts on the next call without reloading anything)."""
    from ai.mcp import builtin_servers
    raw = os.getenv("MCP_SERVERS", "").strip()
    servers = []
    if raw:
        try:
            servers = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("MCP_SERVERS must be valid JSON") from exc
        if not isinstance(servers, list):
            raise ValueError("MCP_SERVERS must be a JSON list")
    # phase 10.5: servers added in the Control Center (a name already used in .env keeps the .env one)
    try:
        from ai.tool_manager import tool_store
        taken = {s.get("name") for s in servers if isinstance(s, dict)}
        servers = servers + [dict(s) for s in tool_store().custom_servers()
                             if isinstance(s, dict) and s.get("name") not in taken]
    except Exception:
        logger.exception("servers added in the Control Center could not be read")
    # built-in servers (phase 5B: violet-web) cannot be shadowed by a configured server of the same name
    builtin = builtin_servers()
    reserved = {b["name"] for b in builtin}
    merged = [s for s in servers if not (isinstance(s, dict) and s.get("name") in reserved)] + builtin
    if not apply_settings:
        return merged
    from ai.tool_manager import apply_tool_settings
    return apply_tool_settings(merged)


def llm_configured() -> bool:
    """Phase 10: whether the model service is set up (API address and key). Violet starts without it -- the Control
    Center's first-run wizard asks for both -- and only the model calls fail until then."""
    return bool(os.getenv("LLM_API_KEY", "").strip() and os.getenv("LLM_API_URL", "").strip())


class LLMConfig:
    """Holds all LLM configuration loaded from environment variables."""

    def __init__(self):
        # phase 10: a missing key no longer stops Violet from starting (agents were built at import time, so a fresh
        # install without .env crashed before the dashboard came up); a model call without a key fails clearly
        self.api_key = os.getenv("LLM_API_KEY", "").strip()
        self.api_url = get_llm_api_url()
        self.reply_model: str = ""         # overridden per-agent
        self.reply_prompt: str = ""        # overridden per-agent
        self.reply_temperature: float = 0.2  # overridden per-agent
        self.fallback_model: str = ""      # optional <AGENT>_LLM_FALLBACK_MODEL
        self.mcp_servers = get_mcp_servers(apply_settings=False)   # settings applied per run (BaseAgent)

    def __repr__(self) -> str:
        return f"LLMConfig(model={self.reply_model}, api_url={self.api_url})"


# ---------------------------------------------------------------------------
# Context helpers (shared across agents)
# ---------------------------------------------------------------------------

def format_recent_messages(limit: int = 30) -> str:
    """Build a compact recent-message context block from SQLite."""
    rows = violetdb.get_recent_messages(limit=limit)
    if not rows:
        return "最近聊天记录：无"

    lines = ["最近聊天记录："]
    for row in rows:
        speaker = "用户" if row["role"] == "user" else "assistant"
        message_type = row["message_type"]
        content = row.get("content") or ""
        body = content if message_type == "text" else f"[{message_type}] {content}".strip()
        lines.append(f"- {row['created_at']} {speaker}: {body}")

    return "\n".join(lines)


def format_base_info() -> str:
    lines = ["基础信息：", f"当前时间: {violetdb.now()}"]
    return "\n".join(lines) + "\n"


def format_active_tasks(limit: int = 10) -> str:
    """Build a compact active-task context block from SQLite."""
    tasks = violetdb.list_active_task_sessions(limit=limit)
    if not tasks:
        return "当前 active_tasks：无"

    lines = ["当前 active_tasks："]
    for task in tasks:
        lines.append(
            "- "
            f"id={task['id']}; "
            f"status={task['status']}; "
            f"name={task['task_name']}; "
            f"description={task['task_description']}; "
            f"task_info={task.get('task_info') or {}}; "
            f"last_question={task.get('last_question') or ''}"
        )
    return "\n".join(lines)


def extract_tool_memories(agent_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract query_memory tool results from an agent run result."""
    recalled: list[dict[str, Any]] = []
    for message in agent_result.get("messages", []):
        if message.get("role") != "tool":
            continue
        content = message.get("content") or ""
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            continue
        for memory in payload.get("memories", []):
            if isinstance(memory, dict) and memory not in recalled:
                recalled.append(memory)
    return recalled


def format_recalled_memories(memories: list[dict[str, Any]]) -> str:
    memories = [m for m in memories if m.get("status") != "deleted"]
    if not memories:
        return "相关记忆：无"
    status_labels = {"expired": "已过有效期", "archived": "很久以前", "forgotten": "印象很模糊"}
    lines = ["相关记忆（来自记忆库，可能与本轮有关；只在自然相关时使用，不要逐条复述）："]
    for memory in memories:
        kind = memory.get("kind") or "legacy"
        tags = [KIND_LABELS.get(kind, kind)]
        progress = memory.get("progress")
        if kind in EVENT_KINDS and progress:
            tags.append(PROGRESS_LABELS.get(progress, progress))
        intent = memory.get("intent_level") or memory.get("intent")        # a database row / a query_memory view
        if intent == "conditional":
            # phase 9.2.1: "要是周三下午没课就去西湖" is not a plan until the user says so
            condition = memory.get("intent_condition") or memory.get("condition")
            tags.append(f"有条件的打算，还没定（条件：{condition or '见内容'}）")
        elif intent == "wish":
            tags.append("只是随口的想法，不是计划")
        if memory.get("event_time"):
            # phase 9.1.1: a day without a clock says so (it used to be shown as 23:59)
            when = event_time_label(memory["event_time"]) if is_date_only(memory["event_time"]) else memory["event_time"]
            tags.append(f"时间 {when}")
        if memory.get("status") in status_labels:
            tags.append(status_labels[memory["status"]])
        if (memory.get("retrieval") or {}).get("reason") == "user_words":
            tags.append("按这句话里的词找到，不一定相关")       # phase 9.1: found by code, not by a search
        if memory.get("id") is not None:
            # phase 9.1.2: an event record that still uses a value the user later corrected (not rewritten, flagged)
            from ai.memory_consistency import conflict_tag, pending_conflicts
            conflicts = pending_conflicts(violetdb, "event_memories", int(memory["id"]), memory.get("content"))
            if conflicts:
                tags.append(conflict_tag(conflicts))
        lines.append(f"- id={memory.get('id')}; [{' / '.join(tags)}] {memory.get('content')}")
    lines.append(
        "使用规则：进展未知或很久以前的事，不要假定它已经结束或仍在进行，也不要为了它追问；"
        "已结束的事不要当作正在发生。"
    )
    return "\n".join(lines)


CONSISTENCY_HINTS = {
    "unrecorded_experience": "没有记录能说明这件事真的发生过",
    "plan_described_as_done": "这还只是计划，没有发生",
    "user_experience_as_own": "这是用户的经历，不是你的",
    "forbidden_backstory": "人设里没有这样的背景",
    "identity_conflict": "与虚拟身份设定不符",
    "external_as_experience": "这是网上看到的外部资料，不是你的亲身经历，可以说“看到一篇文章说…”",
    "camera_claim": "图片是 AI 画的插画，不是照片，不能说是拍的、自拍或摄像头拍的",
    # phase 8.9.4
    "stale_weather": "对方这次没提天气，你也不知道现在的天气；以前聊到的天气是那时候的，别说成现在",
    # phase 8.9.5
    "leaked_draft": "这一行混进了英文的草稿和分析，那不是给对方的话；只写要发给对方的消息",
}


def consistency_note(violations: list[Any]) -> str:
    """Correction note for one rewrite: name the lines, ask for a natural rephrase, keep the rest."""
    from ai.semantic_guard import is_uncertain
    parts = [f"“{v.line}”（{CONSISTENCY_HINTS.get(v.check, v.detail)}）" for v in violations if v.line]
    note = "；".join(dict.fromkeys(parts))
    if any(is_uncertain(v) for v in violations):
        note += "。这些说法无法确认，请自然地改成想法、打算或感受，不要描述没有记录的具体经历"
    return note + "。其余内容和语气尽量保持不变"


def _review(guard, semantic, text: str) -> list[Any]:
    violations = guard.check_reply(text)
    if violations and semantic is not None:
        try:
            violations = semantic.review(violations)
        except Exception:
            logger.exception("semantic review failed; rule findings kept")
    return violations


def guard_reply(content: str, regenerate=None, entity: str = "reply", turn_used_web: bool = False,
                turn_used_schedule: bool = False) -> tuple[str, list[Any]]:
    """Keep Violet's text consistent with her recorded life and persona (phase 4, 5A).

    1. rules (ai/consistency.py);
    2. optional semantic review of UNCERTAIN findings only (ai/semantic_guard.py):
       may clear a finding with evidence, never changes recorded history;
    3. enforce: one natural rephrase with a correction note; only lines that still
       fail are dropped -- never the whole reply because of an uncertain line.
    flag: record only; off: nothing. Returns (text, violations_found).
    """
    from ai.consistency import ConsistencyGuard, guard_mode
    from ai.semantic_guard import SemanticGuard
    mode = guard_mode()
    if mode == "off" or not content:
        return content, []
    try:
        guard = ConsistencyGuard(violetdb)
        guard.check_weather = entity == "reply"          # phase 8.9.4: a proactive weather task has real data
        guard.turn_used_web = turn_used_web               # phase 9.3: "刚查了一下" is backed by this turn's search
        guard.turn_used_schedule = turn_used_schedule     # phase 9.4.2: "看了下你的课表" is this turn's lookup
        semantic = SemanticGuard(violetdb)
        violations = _review(guard, semantic, content)
    except Exception:
        logger.exception("consistency guard failed; reply not checked")
        return content, []
    if not violations:
        return content, []
    from ai.life_store import LifeStore
    store = LifeStore(violetdb)
    for v in violations:
        semantic_note = f" | semantic: {v.semantic}" if v.semantic else ""
        store.flag(entity, None, v.check, f"{v.detail} | {v.line or ''}{semantic_note}")
    if mode != "enforce":
        return content, violations
    if regenerate is not None:
        try:
            retry = regenerate(consistency_note(violations))
            remaining = _review(guard, semantic, retry)
            if not remaining:
                return retry, violations
            return guard.drop_lines(retry, remaining), violations
        except Exception:
            logger.exception("regeneration after consistency violation failed")
    return guard.drop_lines(content, violations), violations


def enforce_follow_up_preference(user_message: str, content: str, regenerate=None) -> tuple[str, dict[str, Any] | None]:
    """Phase 8.8: the user said "别老问我问题" and Violet still ended a reply to a plain share with a question.

    One rewrite with a note; the rewrite is used only if it no longer ends with a question and passes the
    consistency guard. Otherwise the original stays: no question mark is ever cut out, and a question the user
    invited, a question of the user's own, or a turn without the preference is never touched.
    Returns (text, check) -- check is None when nothing applied."""
    from ai.context_engine import context_style
    from ai.interaction import FOLLOW_UP_NOTE, follow_up_preference, follow_up_violation
    from ai.profile_store import Profile
    from ai.settings import env_bool
    if not content or regenerate is None or not env_bool("VIOLET_FOLLOW_UP_CHECK", True) or context_style() != "natural":
        return content, None
    try:
        pref = follow_up_preference(Profile(violetdb).preferences.effective())
        # phase 8.9: the answer to a question the user invited may get one interested follow-up
        from ai.context_engine import ContextEngine
        session = ContextEngine(violetdb)._session_messages() if pref else []
        previous_user = next((m["content"] for m in reversed(session) if m["role"] == "user"), None)
    except Exception:
        logger.exception("follow-up preference unavailable; reply kept")
        return content, None
    if not follow_up_violation(content, user_message, pref, previous_user):
        return content, None
    try:
        retry = (regenerate(FOLLOW_UP_NOTE) or "").strip()
    except Exception:
        logger.exception("rewrite for the follow-up preference failed; reply kept")
        return content, {"rewritten": False, "reason": "rewrite_failed"}
    if not retry:
        return content, {"rewritten": False, "reason": "empty_rewrite"}
    if follow_up_violation(retry, user_message, pref, previous_user):
        return content, {"rewritten": False, "reason": "rewrite_still_asks"}
    checked, violations = guard_reply(retry)
    if violations or not checked.strip():
        return content, {"rewritten": False, "reason": "rewrite_failed_consistency"}
    return checked, {"rewritten": True, "reason": None}


def note_life_mentions(reply_text: str, user_message: str = "", skip_event_ids: list[int] | None = None) -> list[int]:
    """Life events Violet talked about in a normal reply: mark them shared so they are not offered again."""
    from ai.conversation_state import LIFE_TOPIC, life_topic_key
    from ai.life_store import LifeStore
    from ai.memory_model import mentions_topic
    from ai.daily_life import mentions_life
    if not reply_text:
        return []
    store = LifeStore(violetdb)
    mentioned = []
    skip = {int(i) for i in skip_event_ids or []}          # already counted by a picture in this reply
    for event in store.recent_happened(days=30):
        if event["id"] in skip:
            continue
        if mentions_topic(reply_text, event["content"]) or mentions_life(reply_text, event["content"]):
            store.mark_shared(event["id"])
            violetdb.add_topic_mention(life_topic_key(event["id"]), "violet_reply")
            mentioned.append(event["id"])
    if mentioned:
        from ai.conversation_state import ASK_ABOUT_VIOLET_RE
        # the user asked about Violet's life: that is engagement, not Violet pushing her own topic
        violetdb.add_topic_mention(LIFE_TOPIC, "user" if ASK_ABOUT_VIOLET_RE.search(user_message or "") else "violet_reply",
                                user_engaged=1 if ASK_ABOUT_VIOLET_RE.search(user_message or "") else None)
    return mentioned


def cooling_topics_for_turn(recalled_memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Topics Violet should not raise herself right now, minus what this turn is about."""
    try:
        cooling = TopicTracker(violetdb).cooling_topics()
    except Exception:
        logger.exception("could not compute cooling topics")
        return []
    recalled_ids = {m.get("id") for m in recalled_memories or []}
    return [c for c in cooling if c.get("content") and c.get("memory_id") not in recalled_ids]


def format_core_memories(limit: int = 20) -> str:
    memories = violetdb.list_core_memories(limit=limit)
    if not memories:
        return "核心长期记忆：无"
    lines = ["核心长期记忆（长期用户画像和回复偏好）："]
    for memory in memories:
        lines.append(
            f"- id={memory['id']}; key={memory['memory_key']}; "
            f"category={memory['category']}; priority={memory['priority']}; "
            f"content={memory['content']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Robust JSON handling for agent outputs
# ---------------------------------------------------------------------------

def parse_json_object(raw: str | None) -> dict[str, Any] | None:
    """Parse the first JSON object in an LLM reply.

    Tolerates ```json fences and leading/trailing prose. Returns None when no
    JSON object can be recovered (callers decide the fallback).
    """
    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(text, start)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        start = text.find("{", start + 1)
    return None


DEFAULT_INTENT: dict[str, Any] = {
    "route": "chat",
    "should_reply": True,
    "user_intent": "",
    "emotion": "neutral",
    "silent_reason": None,
    "active_task_id": None,
    "task": {"exists": False, "name": None, "description": None},
}


def normalize_intent(raw: dict[str, Any] | None) -> tuple[dict[str, Any], str | None]:
    """Validate IntentAgent output. Returns (intent, problem-or-None).

    Anything unusable degrades to a plain chat turn instead of failing the turn.
    """
    if not isinstance(raw, dict):
        return {**DEFAULT_INTENT, "fallback": True}, "intent output is not a JSON object"

    intent = dict(raw)
    problem = None
    route = intent.get("route")
    if route not in ("chat", "task", "silent"):
        problem = f"unknown route {route!r}"
        route = "chat"

    if route == "task":
        task = intent.get("task") if isinstance(intent.get("task"), dict) else {}
        active_task_id = intent.get("active_task_id")
        if active_task_id is not None:
            try:
                active_task_id = int(active_task_id)
            except (TypeError, ValueError):
                problem = f"invalid active_task_id {active_task_id!r}"
                active_task_id = None
        if active_task_id is not None and violetdb.get_task_session(active_task_id) is None:
            problem = f"active_task_id {active_task_id} does not exist"
            active_task_id = None
        name = (task.get("name") or "").strip() if isinstance(task.get("name"), str) else ""
        if active_task_id is None and not name:
            problem = problem or "task route without task name"
            route = "chat"
        intent["active_task_id"] = active_task_id
        intent["task"] = {
            "exists": route == "task",
            "name": name or task.get("name"),
            "description": task.get("description") or name or None,
        }
    intent["route"] = route
    return intent, problem


# ---------------------------------------------------------------------------
# Multi-agent pipeline
# ---------------------------------------------------------------------------

def _failed_performer_result(active_task_id: int | None, exc: Exception) -> dict[str, Any]:
    return {
        "content": json.dumps(
            {
                "task_status": "failed",
                "task_id": active_task_id,
                "actions_taken": [],
                "result": {"summary": "任务执行过程中出错，这次没有完成", "data": {}},
                "user_visible_facts": ["这件事这次没办成"],
                "internal_notes": f"performer error: {exc.__class__.__name__}",
            },
            ensure_ascii=False,
        ),
        "messages": [],
        "raw": {},
        "error": f"{exc.__class__.__name__}: {exc}",
    }


def settle_reminder_request(user_message: str, turn_id: str | None) -> dict[str, Any] | None:
    """Phase 7 B.10.1: an explicit "提醒我…" request is saved BEFORE the reply, so the reply can say what
    really happened. None when the message is not such a request (or the feature is off).

    status: "set"            -- a pending commitment was saved (or already saved for this very turn)
            "already_pending" -- the same reminder was already pending from an earlier turn: nothing new
            "not_set"         -- nothing was saved (reason says why); the reply must not claim otherwise
    A saved reminder is only a pending row: it says nothing about the later delivery."""
    from ai.memory_model import parse_reminder_request, reminder_before_reply_enabled
    if not reminder_before_reply_enabled():
        return None
    parsed = parse_reminder_request(user_message)
    if not parsed["requested"]:
        return None
    outcome = {"status": "not_set", "reason": parsed["reason"], "content": parsed["content"],
               "due_at": parsed["due_at"], "evidence": parsed["evidence"], "commitment_id": None}
    if not parsed["ok"]:
        return outcome
    try:
        result = violetdb.add_commitment(parsed["content"], parsed["due_at"], kind="reminder", evidence=parsed["evidence"],
                                      source_turn_id=turn_id, created_by="user")
        row = violetdb.get_commitment(int(result["commitment"]["id"]))      # what is really stored, not what was asked
    except Exception:
        logger.exception("could not save the reminder of turn %s", turn_id)
        return {**outcome, "reason": "storage_error"}
    if not row or row["status"] != "pending":
        return {**outcome, "reason": "storage_error"}
    status = "already_pending" if result["action"] == "duplicate" else "set"
    return {**outcome, "status": status, "reason": result["action"], "commitment_id": row["id"],
            "content": row["content"], "due_at": row["due_at"]}


def reminder_note(outcome: dict[str, Any]) -> str:
    """What the reply must know about this turn's reminder (facts only, no promise about delivery)."""
    from ai.memory_model import parse_time, reminder_failure_text
    if outcome["status"] in ("set", "already_pending"):
        when = parse_time(outcome["due_at"])
        label = f"{when.month}月{when.day}日 {when:%H:%M}" if when else outcome["due_at"]
        if outcome["status"] == "set":
            return (f"【提醒】用户要你设的提醒已经保存好了（系统里的真实记录）：{label} 提醒“{outcome['content']}”。"
                    "回复里简短确认时间和事项就行，不要说设不了，也不要让用户自己另设。")
        return (f"【提醒】同样的提醒之前已经设好了：{label} 提醒“{outcome['content']}”，这次没有重复设置。"
                "简短告诉用户已经有这个提醒了。")
    return (f"【提醒】用户像是要你设提醒，但这次没有设置成功：{reminder_failure_text(outcome['reason'])}。"
            "不要说已经设好，也不要说到时会提醒；如果用户确实想设提醒，请他说清楚哪天几点、提醒什么。"
            "如果用户其实不是在请你设提醒，就当没有这条。")


def run_pipeline(
    user_message: str,
    intent_agent,
    chat_agent,
    performer_agent,
    memory_agent,
    defer_memory: bool = False,
    turn_id: str | None = None,
    turn_key: str | None = None,
) -> dict[str, Any]:
    """One user turn (see _run_pipeline). Phase 9.2: the turn is one tool scope -- an MCP server that fails in it is
    not tried again by a later agent of the same turn (ai.mcp.tool_turn)."""
    from ai.diagnostics import turn_scope
    from ai.mcp import tool_turn
    from ai.schedule.access import turn_message
    # phase 9.4.2: the user's own words of this turn decide which personal timetable fields a lookup may return
    # phase 10.6: the turn id is the correlation id of every log line and diagnosis event of this turn
    with turn_scope(turn_id), tool_turn(), turn_message(user_message):
        result = _run_pipeline(user_message, intent_agent, chat_agent, performer_agent, memory_agent,
                               defer_memory=defer_memory, turn_id=turn_id, turn_key=turn_key)
        _record_tool_plan(result)
        return result


def _record_tool_plan(result: dict[str, Any]) -> None:
    """Phase 10.6: what this turn did with tools, for the diagnosis (names and states only)."""
    try:
        plan = result.get("tool_plan") or {}
        called = [n for n in ((result.get("chat") or {}).get("tools_called") or []) if "__" in n]
        if not plan.get("needed") and not called and plan.get("mode") != "blocked":
            return
        from ai.diagnostics import record
        unknown = list(plan.get("tools") or []) if plan.get("mode") == "blocked" and \
            plan.get("status") == "not_configured" else []
        record("tool_plan", plan.get("mode") != "blocked", mode=plan.get("mode"), status=plan.get("status"),
               tools=list(plan.get("tools") or []), called=called, unknown_tools=unknown)
    except Exception:
        logger.exception("could not record the tool plan of this turn")


def _run_pipeline(
    user_message: str,
    intent_agent,
    chat_agent,
    performer_agent,
    memory_agent,
    defer_memory: bool = False,
    turn_id: str | None = None,
    turn_key: str | None = None,
) -> dict[str, Any]:
    """
    串行执行 intent -> chat -> memory 三阶段 pipeline。

    defer_memory=True 时不在这里运行 MemoryAgent（由 ai/memory_jobs.py 在回复发送后后台处理）。

    任何单个阶段失败都不会让整轮抛出异常：
      - Intent 失败或输出非法 JSON -> 按普通聊天处理；
      - Performer 失败 -> 任务标记 failed，Chat 以失败结果自然回复；
      - Chat 失败 -> chat["failed"]=True，由调用方决定降级回复；
      - Memory 失败 -> 跳过记忆更新。

    Returns:
        {
            "intent": dict,   # IntentAgent 原始输出
            "chat":   dict,   # ChatAgent   原始输出（含 content / messages / raw）
            "memory": dict,   # MemoryAgent 原始输出
            "errors": dict,   # 各阶段错误摘要（无错误时为空）
            "chat_failed": bool,
        }
    """
    visualize_pipeline_start(user_message)
    _ps.pipeline_start("chat", user_message[:60])
    errors: dict[str, str] = {}

    # Step 0 (phase 9.2): the external tools that are really available now -- Intent decides with this list
    from ai.tool_manager import (Catalog, chat_note, direct_note, direct_tools, discover, intent_note, plan_for_turn,
                                 tool_results_from)
    try:
        catalog = discover()
    except Exception:
        logger.exception("tool discovery failed; this turn runs without external tools")
        catalog = Catalog()
    from ai import trace
    trace.step("可用工具", 工具=sorted(catalog.tools) or None, 连不上=[u.get("server") for u in catalog.unavailable] or None,
               已停用=catalog.disabled or None, 未授权=catalog.refused or None)

    # Step 1: 意图识别
    visualize_step("Intent Recognition", "识别用户意图：对话 or 任务")
    _ps.node_start("Intent", input_data={"user_message": user_message})
    try:
        intent_result = intent_agent.run(user_message, call_context={"tool_catalog_note": intent_note(catalog)})
        parsed = parse_json_object(intent_result.get("content"))
    except Exception as exc:
        logger.exception("IntentAgent failed; falling back to plain chat route")
        errors["intent"] = f"{exc.__class__.__name__}: {exc}"
        intent_result = {"content": "", "messages": [], "raw": {}, "error": errors["intent"]}
        parsed = None
    intent_json, problem = normalize_intent(parsed)
    if problem:
        logger.warning("Intent output unusable (%s); continuing as %s", problem, intent_json["route"])
        errors.setdefault("intent", problem)
    trace.step("意图识别", 路线=intent_json.get("route"), 要回复=intent_json.get("should_reply"),
               工具请求=intent_json.get("tool_query") or "没有", 问题=problem, 原始输出=trace.text(intent_result.get("content")))
    recalled_memories = extract_tool_memories(intent_result)
    from ai import test_mode
    testing = test_mode.active()          # 2026-10-30: no memories, no topic bookkeeping, no persona checks
    try:
        # phase 9.1 (A2): memories the user's own words are about, whether or not Intent searched
        from ai.recall_search import user_word_recall
        if not testing:
            recalled_memories += user_word_recall(violetdb, user_message,
                                                  exclude_ids=[m.get("id") for m in recalled_memories])
    except Exception:
        logger.exception("word recall failed; continuing with Intent's results only")
    try:
        user_raised = [] if testing else TopicTracker(violetdb).note_user_mentions(user_message, recalled_memories)
    except Exception:
        logger.exception("could not record user-raised topics")
        user_raised = []

    if intent_json.get("route") == "silent" or intent_json.get("should_reply") is False:
        _ps.node_done("Intent", output_data={"messages": intent_result.get("messages", []), "content": intent_json})
        _ps.node_skip("Performer")
        _ps.node_skip("Chat")
        _ps.node_skip("Memory")
        _ps.pipeline_end()
        visualize_pipeline_end()
        return {
            "intent": intent_result,
            "chat": {"content": "", "messages": [], "raw": {}, "skipped": True},
            "memory": {"content": "", "messages": [], "raw": {}, "skipped": True},
            "recalled_memories": recalled_memories,
            "should_reply": False,
            "errors": errors,
            "chat_failed": False,
        }

    try:
        reminder = settle_reminder_request(user_message, turn_id)
    except Exception:
        logger.exception("reminder request check failed for turn %s", turn_id)
        reminder = None
    if reminder is not None and intent_json["route"] == "task" and intent_json.get("active_task_id") is None:
        # the reminder is handled above with the product's commitment store; the Performer has no reminder
        # tool and would only mark a new task as failed (phase 7 B.10 round 26)
        intent_json = {**intent_json, "route": "chat", "route_override": "reminder_before_reply"}
    try:
        # phase 9.1.2 (D-P911-R2): "那件事你忘掉吧" is carried out here, before the reply, by the memory store itself;
        # the Performer has no memory tools (in p911-real-1 it made a failed task and the reply said "抹不掉")
        from ai.memory_forget import forget_note, settle_forget_request
        forget = settle_forget_request(violetdb, user_message, turn_id)
    except Exception:
        logger.exception("forget request check failed for turn %s", turn_id)
        forget = None
    if forget is not None and intent_json["route"] == "task" and intent_json.get("active_task_id") is None:
        intent_json = {**intent_json, "route": "chat", "route_override": "forget_before_reply"}
    if forget is not None and forget["status"] == "forgotten":
        # its own memory was just deleted: what Intent / word recall found for it must not reach Chat
        recalled_memories = [m for m in recalled_memories if m.get("id") != forget["memory_id"]]

    # phase 9.2: what this turn does with tools. A read-only query is answered in Chat's own run (no task); an
    # execution tool goes to the Performer; a tool that is missing / off / down / not permitted never becomes a task.
    plan = {"needed": False, "mode": "none", "tools": []}
    if reminder is None and forget is None:
        try:
            plan = plan_for_turn(intent_json, catalog)
        except Exception:
            logger.exception("tool plan failed; this turn runs without external tools")
    trace.step("工具计划", 方式=plan.get("mode"), 工具=plan.get("tools") or None, 状态=plan.get("status"),
               原因=plan.get("reason"), 缺少=plan.get("missing"),
               说明="提醒 / 遗忘请求这一轮不用工具" if reminder is not None or forget is not None else None)
    new_task = intent_json["route"] == "task" and intent_json.get("active_task_id") is None
    if plan["mode"] in ("chat", "blocked") and new_task:
        intent_json = {**intent_json, "route": "chat", "route_override": f"tool_{plan['mode']}"}
    elif plan["mode"] == "performer" and intent_json["route"] != "task":
        intent_json = {**intent_json, "route": "task", "route_override": "tool_performer", "active_task_id": None,
                       "task": {"exists": True, "name": (plan.get("reason") or "工具任务")[:60],
                                "description": plan.get("reason") or user_message[:120]}}

    performer_result = None
    if intent_json["route"] == "task":
        _ps.node_done("Intent", "Performer", output_data={"messages": intent_result.get("messages", []), "content": intent_json})
        visualize_step("Task Performer", "执行任务相关操作")
        active_task_id = intent_json.get("active_task_id")
        try:
            if active_task_id is None:
                task = intent_json["task"]
                active_task_id = violetdb.create_task_session(
                    task_name=task["name"],
                    task_description=task["description"] or task["name"],
                    task_info={},
                    status="pending_info",
                )
            performer_input = {
                "user_message": user_message,
                "intent": intent_json,
                "active_task_id": active_task_id,
                "recalled_memories": recalled_memories,
            }
            _ps.node_start("Performer", input_data=performer_input)
            performer_result = performer_agent.run(
                json.dumps(performer_input, ensure_ascii=False)
            )
        except Exception as exc:
            logger.exception("PerformerAgent failed for task %s", active_task_id)
            errors["performer"] = f"{exc.__class__.__name__}: {exc}"
            if active_task_id is not None:
                try:
                    violetdb.update_task_session(
                        task_id=active_task_id,
                        status="failed",
                        error_message=f"performer error: {exc.__class__.__name__}"[:300],
                    )
                except Exception:
                    logger.exception("could not mark task %s as failed", active_task_id)
            performer_result = _failed_performer_result(active_task_id, exc)
        _ps.node_done("Performer", "Chat", output_data={"messages": performer_result.get("messages", []), "content": performer_result.get("content")})
    else:
        _ps.node_done("Intent", "Chat", output_data={"messages": intent_result.get("messages", []), "content": intent_json})
        _ps.node_skip("Performer")

    direct = direct_tools(catalog) if reminder is None and forget is None else []
    # Step 2: 对话生成
    visualize_step("Chat Generation", "生成回复消息")
    chat_input = {"user_message": user_message, "performer_result": bool(performer_result), "recalled_memories_count": len(recalled_memories)}
    _ps.node_start("Chat", input_data=chat_input)
    chat_context = {
        "performer_result": performer_result,
        "recalled_memories": recalled_memories,
        "restricted_topics": [] if testing else cooling_topics_for_turn(recalled_memories),
        "route": intent_json.get("route"),
        # phase 8.9.8: IntentAgent's reading of this message (8.9.7: it read "调侃" / "带撒娇" well, Chat never saw it)
        "intent_reading": None if intent_json.get("fallback") else
        {"user_intent": intent_json.get("user_intent"), "emotion": intent_json.get("emotion")},
        "turn_id": turn_id,
        "turn_key": turn_key or turn_id,       # stable across crash recovery (inbound ids): idempotent images
        "reminder_note": reminder_note(reminder) if reminder is not None else None,
        "forget_note": forget_note(forget) if forget is not None else None,
        # phase 9.2: the turn's tool plan (ChatAgent offers / forces the tools from it), what Chat must know about
        # it, and the external data the Performer's tools returned (to answer from, not only a task log)
        "tool_plan": plan,
        "tool_note": chat_note(plan, catalog) or direct_note(direct),
        # 2026-10-30 (rework step 2, "他不会用工具"): the read-only tools go to Chat on every turn, it decides itself;
        # Intent's tool_query still makes the named one come first. Off: VIOLET_CHAT_TOOLS_DIRECT=0 (only when planned)
        "direct_tools": direct,
        "tool_results": tool_results_from((performer_result or {}).get("messages") or []),
    }
    try:
        chat_result = chat_agent.run(user_message, call_context=chat_context)
        # phase 9.3: a link in the reply must come from this turn's tools, the user's words or the recent conversation
        from ai.web.citations import checked_reply
        recent = [r.get("content") for r in violetdb.get_recent_messages(limit=30)]
        chat_result = checked_reply(chat_result, user_message, [*chat_context["tool_results"], *recent])
        def used(server: str) -> bool:
            return any(isinstance(m, dict) and m.get("role") == "tool"
                       and str(m.get("content") or "").startswith(f"【外部工具数据｜来源 {server}/")
                       for m in chat_result.get("messages") or [])
        guard_flags = {"turn_used_web": used("violet-web")}
        if used("violet-schedule"):
            guard_flags["turn_used_schedule"] = True        # phase 9.4.2 (only passed when true: older wrappers)
        guarded, violations = (chat_result.get("content", ""), []) if testing else guard_reply(
            chat_result.get("content", ""),
            regenerate=lambda note: chat_agent.run(user_message, call_context={**chat_context, "consistency_note": note})
            .get("content", ""),
            **guard_flags,
        )
        if violations:
            chat_result = {**chat_result, "content": guarded, "consistency_violations": [v.check for v in violations]}
            if not guarded.strip():
                errors["chat"] = "consistency: every line contradicted recorded facts"
                chat_result["failed"] = True
        if not chat_result.get("failed") and not testing:
            content, follow_up = enforce_follow_up_preference(
                user_message, chat_result.get("content", ""),
                regenerate=lambda note: chat_agent.run(user_message, call_context={**chat_context, "follow_up_note": note})
                .get("content", ""))
            if follow_up is not None:
                chat_result = {**chat_result, "content": content, "follow_up_check": follow_up}
    except Exception as exc:
        logger.exception("ChatAgent failed")
        errors["chat"] = f"{exc.__class__.__name__}: {exc}"
        chat_result = {"content": "", "messages": [], "raw": {}, "error": errors["chat"], "failed": True}
    _ps.node_done("Chat", "Memory", output_data={"messages": chat_result.get("messages", []), "content": chat_result.get("content")})

    # Step 3: 记忆判断（以本轮对话为输入）；测试模式不记
    if defer_memory or testing:
        _ps.node_skip("Memory")
        memory_result = {"content": "", "messages": [], "raw": {}, "skipped": True, "deferred": True}
        visualize_pipeline_end()
        _ps.pipeline_end(error="; ".join(f"{k}: {v}" for k, v in errors.items())[:500] if errors else None)
        return {
            "intent": intent_result,
            "chat": chat_result,
            "memory": memory_result,
            "recalled_memories": recalled_memories,
            "user_raised_memory_ids": user_raised,
            "reminder": reminder,
            "tool_plan": plan,
            "errors": errors,
            "chat_failed": bool(chat_result.get("failed")),
        }

    visualize_step("Memory Processing", "处理记忆存储")
    memory_input = f"用户说：{user_message}"
    _ps.node_start("Memory", input_data={"memory_input": memory_input})
    try:
        memory_result = memory_agent.run(memory_input)
    except Exception as exc:
        logger.warning("MemoryAgent failed, continue without memory update: %s", exc)
        errors["memory"] = f"{exc.__class__.__name__}: {exc}"
        memory_result = {
            "content": json.dumps(
                {"should_save": False, "reason": f"MemoryAgent failed: {exc}"},
                ensure_ascii=False,
            ),
            "messages": [],
            "raw": {},
            "error": str(exc),
        }
    _ps.node_done("Memory", output_data={"messages": memory_result.get("messages", []), "content": memory_result.get("content")})

    visualize_pipeline_end()
    _ps.pipeline_end(error="; ".join(f"{k}: {v}" for k, v in errors.items())[:500] if errors else None)

    return {
        "intent": intent_result,
        "chat": chat_result,
        "memory": memory_result,
        "recalled_memories": recalled_memories,
        "user_raised_memory_ids": user_raised,
        "reminder": reminder,
        "tool_plan": plan,
        "errors": errors,
        "chat_failed": bool(chat_result.get("failed")),
    }


def _safe_json_loads(raw: str, default: Any) -> Any:
    parsed = parse_json_object(raw)
    return parsed if parsed is not None else default


def is_proactive_task_allowed(task: dict[str, Any] | None, event_context: dict[str, Any]) -> tuple[bool, str]:
    if not task or not isinstance(task, dict):
        return False, "missing task"

    task_name = task.get("name")
    allowed_tasks = event_context.get("allowed_tasks") or {}
    task_config = allowed_tasks.get(task_name)
    if not task_config or not task_config.get("enabled", False):
        return False, f"proactive task is not enabled: {task_name}"

    constraints = task.get("constraints") or {}
    configured_constraints = task_config.get("constraints") or {}
    budget_max = configured_constraints.get("budget_max", task_config.get("budget_max"))
    requested_budget = constraints.get("budget_max") or (task.get("task_info") or {}).get("budget_max")
    try:
        if budget_max is not None and requested_budget is not None and float(requested_budget) > float(budget_max):
            return False, f"requested budget exceeds configured budget_max: {requested_budget} > {budget_max}"
    except (TypeError, ValueError):
        return False, f"invalid budget value: {requested_budget!r}"

    return True, "allowed"


def get_proactive_task_config(task: dict[str, Any] | None, event_context: dict[str, Any]) -> dict[str, Any]:
    if not task:
        return {}
    return (event_context.get("allowed_tasks") or {}).get(task.get("name"), {}) or {}


def run_proactive_pipeline(
    event_context: dict[str, Any],
    proactive_agent,
    chat_agent,
    performer_agent,
) -> dict[str, Any]:
    """
    串行执行 proactive -> performer? -> chat 主动触达 pipeline。

    ProactiveAgent 只做决策；是否真的执行任务由 Python 层根据 event_context
    和配置硬校验。主动链路不调用 MemoryAgent。

    任何阶段抛出的异常都会被记录为 proactive_events.status = failed，
    不会再让事件停留在 planned / task_started。
    """
    state: dict[str, Any] = {"event_id": None, "task_id": None}
    try:
        return _run_proactive_pipeline(event_context, proactive_agent, chat_agent, performer_agent, state)
    except Exception as exc:
        logger.exception("proactive pipeline failed")
        reason = f"pipeline error: {exc.__class__.__name__}: {exc}"[:500]
        try:
            if state["event_id"] is None:
                state["event_id"] = violetdb.add_proactive_event(
                    event_type=event_context.get("event_type", "scheduled_check"),
                    status="failed",
                    action_type="none",
                    reason=reason,
                    metadata=event_context,
                    scheduled_at=event_context.get("scheduled_at"),
                )
            else:
                violetdb.update_proactive_event(state["event_id"], status="failed", reason=reason)
            if state["task_id"] is not None:
                violetdb.update_task_session(task_id=state["task_id"], status="failed", error_message=reason[:300])
        except Exception:
            logger.exception("could not record proactive failure")
        visualize_pipeline_end()
        _ps.pipeline_end(error=reason)
        return {
            "event_id": state["event_id"],
            "proactive": None,
            "proactive_decision": None,
            "performer": None,
            "chat": None,
            "should_send": False,
            "error": reason,
        }


def _run_proactive_pipeline(
    event_context: dict[str, Any],
    proactive_agent,
    chat_agent,
    performer_agent,
    state: dict[str, Any],
) -> dict[str, Any]:
    event_input = json.dumps(event_context, ensure_ascii=False)
    visualize_pipeline_start(f"[proactive] {event_context.get('event_type', 'scheduled_check')}")
    _ps.pipeline_start("proactive", event_context.get("event_type", "proactive"))

    visualize_step("Proactive Decision", "判断是否主动触达以及是否下发任务")
    _ps.node_start("Proactive", input_data={"event_context": event_context})
    proactive_result = proactive_agent.run(event_input)
    proactive_json = _safe_json_loads(proactive_result.get("content", ""), default={})
    if not isinstance(proactive_json, dict):
        proactive_json = {}
    if not proactive_json:
        logger.warning("ProactiveAgent returned no usable JSON; treating as 'do not act'")

    should_act = bool(proactive_json.get("should_act"))
    action_type = proactive_json.get("action_type", "none")
    if action_type not in ("none", "chat", "task"):
        logger.warning("ProactiveAgent returned unknown action_type %r; treating as 'none'", action_type)
        action_type = "none"
    conversation_state = event_context.get("conversation_state") or {}
    violation = None
    if should_act and action_type != "none":
        violation = validate_proactive_decision(proactive_json, conversation_state)
        if violation:
            logger.info("proactive decision rejected by policy: %s", violation)
    if not should_act or action_type == "none" or violation:
        _ps.node_done("Proactive", output_data={"messages": proactive_result.get("messages", []), "content": proactive_json})
        _ps.node_skip("Performer")
        _ps.node_skip("Chat")
        event_id = violetdb.add_proactive_event(
            event_type=event_context.get("event_type", "scheduled_check"),
            status="skipped",
            action_type="none",
            reason=(f"policy: {violation}" if violation else
                    proactive_json.get("reason", "ProactiveAgent decided not to act.")),
            decision=proactive_json,
            metadata=event_context,
            scheduled_at=event_context.get("scheduled_at"),
        )
        state["event_id"] = event_id
        visualize_pipeline_end()
        _ps.pipeline_end()
        return {
            "event_id": event_id,
            "proactive": proactive_result,
            "proactive_decision": proactive_json,
            "performer": None,
            "chat": None,
            "should_send": False,
        }

    event_id = violetdb.add_proactive_event(
        event_type=event_context.get("event_type", "scheduled_check"),
        status="planned",
        action_type=action_type,
        reason=proactive_json.get("reason"),
        decision=proactive_json,
        metadata=event_context,
        scheduled_at=event_context.get("scheduled_at"),
    )
    state["event_id"] = event_id

    performer_result = None
    active_task_id = None
    if action_type == "task":
        task = proactive_json.get("task")
        allowed, reason = is_proactive_task_allowed(task, event_context)
        if not allowed:
            _ps.node_done("Proactive", output_data={"messages": proactive_result.get("messages", []), "content": proactive_json})
            _ps.node_skip("Performer")
            _ps.node_skip("Chat")
            violetdb.update_proactive_event(event_id, status="failed", reason=reason)
            visualize_pipeline_end()
            _ps.pipeline_end(error=reason)
            return {
                "event_id": event_id,
                "proactive": proactive_result,
                "proactive_decision": proactive_json,
                "performer": None,
                "chat": None,
                "should_send": False,
                "error": reason,
            }

        task_config = get_proactive_task_config(task, event_context)
        _ps.node_done("Proactive", "Performer", output_data={"messages": proactive_result.get("messages", []), "content": proactive_json})
        visualize_step("Task Performer", "执行主动触达任务")
        performer_input_data = {
            "origin": "proactive",
            "event_context": event_context,
            "proactive_decision": proactive_json,
            "active_task_id": None,
            "task": task,
        }
        _ps.node_start("Performer", input_data=performer_input_data)
        active_task_id = violetdb.create_task_session(
            task_name=task["name"],
            task_description=task.get("description") or task["name"],
            task_info={
                "origin": "proactive",
                "proactive_event_id": event_id,
                "configured_task": {
                    "description": task_config.get("description"),
                    "task_info": task_config.get("task_info") or {},
                    "constraints": task_config.get("constraints") or {},
                },
                **(task.get("task_info") or {}),
                "constraints": {
                    **(task_config.get("constraints") or {}),
                    **(task.get("constraints") or {}),
                },
            },
            status="pending_info",
        )
        state["task_id"] = active_task_id
        violetdb.update_proactive_event(event_id, status="task_started", task_id=active_task_id)
        performer_input = {
            "origin": "proactive",
            "event_context": event_context,
            "proactive_decision": proactive_json,
            "active_task_id": active_task_id,
            "task": task,
        }
        performer_result = performer_agent.run(json.dumps(performer_input, ensure_ascii=False))
        _ps.node_done("Performer", "Chat", output_data={"messages": performer_result.get("messages", []), "content": performer_result.get("content")})
    else:
        _ps.node_done("Proactive", "Chat", output_data={"messages": proactive_result.get("messages", []), "content": proactive_json})
        _ps.node_skip("Performer")

    visualize_step("Chat Generation", "生成主动触达消息")
    _ps.node_start("Chat", input_data={"trigger": "[proactive event]", "proactive_decision": proactive_json, "performer_done": bool(performer_result)})
    restricted = restricted_topics_from_state(conversation_state)
    chat_result = chat_agent.run(
        "[proactive event]",
        call_context={
            "is_proactive": True,
            "proactive_result": proactive_json,
            "performer_result": performer_result,
            "restricted_topics": restricted,
            "proactive_note": event_context.get("proactive_habit_note"),     # phase 8.9.5
        },
    )
    _ps.node_done("Chat", output_data={"messages": chat_result.get("messages", []), "content": chat_result.get("content")})

    guarded, life_violations = guard_reply(chat_result.get("content", ""), entity="proactive_reply")
    if life_violations:
        chat_result = {**chat_result, "content": guarded}
        if not guarded.strip():
            reason = "policy: proactive message contradicted recorded facts"
            violetdb.update_proactive_event(event_id, status="skipped", reason=reason, decision=proactive_json)
            visualize_pipeline_end()
            _ps.pipeline_end()
            return {"event_id": event_id, "proactive": proactive_result, "proactive_decision": proactive_json,
                    "performer": performer_result, "chat": chat_result, "should_send": False, "error": reason}
    touched = find_touched_topic(chat_result.get("content", ""), restricted)
    if touched is not None:
        reason = f"policy: generated message touches restricted topic (memory {touched.get('memory_id') or touched.get('topic_key')})"
        logger.info(reason)
        violetdb.update_proactive_event(event_id, status="skipped", reason=reason, decision=proactive_json, task_id=active_task_id)
        visualize_pipeline_end()
        _ps.pipeline_end()
        return {
            "event_id": event_id,
            "proactive": proactive_result,
            "proactive_decision": proactive_json,
            "performer": performer_result,
            "chat": chat_result,
            "should_send": False,
            "error": reason,
        }

    violetdb.update_proactive_event(
        event_id,
        action_type=action_type,
        decision=proactive_json,
        task_id=active_task_id,
    )
    visualize_pipeline_end()
    _ps.pipeline_end()

    return {
        "event_id": event_id,
        "proactive": proactive_result,
        "proactive_decision": proactive_json,
        "performer": performer_result,
        "chat": chat_result,
        "should_send": True,
        "topic_key": decision_topic_key(proactive_json),
        "mentioned_memory_ids": decision_memory_ids(proactive_json),
        "mentioned_life_event_ids": decision_life_event_ids(proactive_json),
        "mentioned_web_finding_ids": decision_web_finding_ids(proactive_json),
    }


# ---------------------------------------------------------------------------
# Proactive policy helpers (phase 2)
# ---------------------------------------------------------------------------

def decision_topic_key(decision: dict[str, Any]) -> str:
    brief = decision.get("chat_brief") if isinstance(decision.get("chat_brief"), dict) else {}
    return normalize_topic_key(decision.get("topic_key") or brief.get("topic"))


def decision_memory_ids(decision: dict[str, Any]) -> list[int]:
    ids = []
    for value in decision.get("mentioned_memory_ids") or []:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids


def decision_web_finding_ids(decision: dict[str, Any]) -> list[int]:
    ids = []
    for value in decision.get("mentioned_web_finding_ids") or []:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids


def note_web_mentions(reply_text: str, used_finding_ids: list[int] | None = None) -> list[int]:
    """Findings Violet talked about in a sent reply count as shared (never offered proactively again).

    A finding used by this turn's web tools is shared when the reply names its site or talks about it;
    any other recent finding only when the reply clearly talks about its title.
    """
    from datetime import datetime, timedelta

    from ai.conversation_state import web_topic_key
    from ai.memory_model import mentions_topic, touches_topic
    from ai.web.findings import FindingsStore
    if not reply_text:
        return []
    store = FindingsStore(violetdb)
    since = (datetime.fromisoformat(violetdb.now()) - timedelta(days=3)).isoformat()
    used = {int(i) for i in used_finding_ids or []}
    shared = []
    for f in violetdb.fetch_all("SELECT * FROM web_findings WHERE status = 'active' AND updated_at >= ?", (since,)):
        if f["id"] in used:
            mentioned = f["domain"] in reply_text or mentions_topic(reply_text, f["title"]) or \
                bool(f["summary"] and touches_topic(reply_text, f["summary"]))
        else:
            mentioned = touches_topic(reply_text, f["title"])
        if mentioned:
            store.mark_shared(f["id"])
            violetdb.add_topic_mention(web_topic_key(f["id"]), "violet_reply")
            shared.append(f["id"])
    return shared


def decision_life_event_ids(decision: dict[str, Any]) -> list[int]:
    ids = []
    for value in decision.get("mentioned_life_event_ids") or []:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids


def validate_proactive_decision(decision: dict[str, Any], conversation_state: dict[str, Any]) -> str | None:
    """Code-level checks on a ProactiveAgent decision. Returns a violation or None.

    * a memory may only be referenced if it is in allowed_memories (long-term
      facts that are not cooling); events are never proactive follow-ups;
    * a cooling topic key may not be chosen again until the user re-engages.
    """
    if not conversation_state:
        return None
    allowed = {int(m["id"]) for m in conversation_state.get("allowed_memories") or [] if m.get("id") is not None}
    for memory_id in decision_memory_ids(decision):
        if memory_id not in allowed:
            return f"memory {memory_id} may not be raised proactively"
    allowed_web = {int(f["id"]) for f in conversation_state.get("allowed_web_findings") or []}
    for finding_id in decision_web_finding_ids(decision):
        if finding_id not in allowed_web:
            return f"web finding {finding_id} may not be shared now"
    allowed_life = {int(e["id"]) for e in conversation_state.get("allowed_life_events") or []}
    for event_id in decision_life_event_ids(decision):
        if event_id not in allowed_life:
            return f"life event {event_id} may not be shared now"
    # phase 9.4.4: timetable moments only as offered this time (an exam already wished for is never offered again)
    allowed_schedule = {str(m.get("id")) for m in conversation_state.get("allowed_schedule_moments") or []}
    for moment_id in decision.get("mentioned_schedule_ids") or []:
        if str(moment_id) not in allowed_schedule:
            return f"schedule moment {str(moment_id)[:60]} was not offered now"
    cooling_keys = {c.get("topic_key") for c in conversation_state.get("cooling_topics") or []}
    key = decision_topic_key(decision)
    if key in cooling_keys:
        return f"topic {key!r} is cooling (raised without engagement)"
    return None


def restricted_topics_from_state(conversation_state: dict[str, Any]) -> list[dict[str, Any]]:
    restricted = [dict(m) for m in conversation_state.get("restricted_memories") or [] if m.get("content")]
    restricted += [dict(c) for c in conversation_state.get("cooling_topics") or [] if c.get("content")]
    return restricted


def find_touched_topic(text: str, restricted: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Last line of defence: block a proactive message that clearly talks about a restricted topic."""
    if not text or not env_bool("VIOLET_PROACTIVE_TOPIC_GUARD", True):
        return None
    for topic in restricted:
        if touches_topic(text, topic.get("content") or ""):
            return topic
    return None


def run_commitment_pipeline(commitment: dict[str, Any], chat_agent) -> dict[str, Any]:
    """Generate the text of a due reminder the user explicitly asked for.

    The reminder must be delivered: if generation fails, declines ([NO_REPLY])
    or does not mention the reminded thing, a plain template is used instead
    (VIOLET_COMMITMENT_FALLBACK_TEMPLATE).
    """
    content = commitment["content"]
    decision = {
        "action_type": "commitment_reminder",
        "reason": "用户明确要求的提醒已到时间",
        "chat_brief": {"topic": f"按用户的要求提醒：{content}", "must_say": [content]},
        "commitment": {"content": content, "due_at": commitment.get("due_at"), "evidence": commitment.get("evidence")},
    }
    error = None
    text = ""
    try:
        result = chat_agent.run(
            "[commitment reminder]",
            call_context={"is_proactive": True, "proactive_result": decision, "commitment": commitment},
        )
        text = result.get("content", "")
    except Exception as exc:
        error = f"{exc.__class__.__name__}: {exc}"
        logger.warning("commitment %s: chat generation failed: %s", commitment.get("id"), error)
    from ai.channels.outbound import chat_payload, fixed_text   # phase 10.6: sealed, sendable messages only
    payload = chat_payload(text if isinstance(text, str) else "", images=False)   # reminders never send images
    joined = "".join(m["content"] for m in payload.get("messages", []))
    mentions = VioletDatabase.normalize_text(content) in VioletDatabase.normalize_text(joined) or touches_topic(joined, content)
    if payload.get("should_send") and mentions:
        return {"messages": payload["messages"], "used_fallback": False, "error": error}
    template = env_str("VIOLET_COMMITMENT_FALLBACK_TEMPLATE", "提醒你一下：{content}") or "提醒你一下：{content}"
    return {
        "messages": fixed_text(template.format(content=content)),
        "used_fallback": True,
        "error": error or ("generated text did not mention the reminder" if payload.get("should_send") else "no text generated"),
    }
