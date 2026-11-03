"""Shorter descriptions for the MemoryAgent's tools (2026-11-02, user: 只要是同样的效果，token 可以更省).

The memory step sends 17 tool schemas (~4k tokens) with every call. Much of their wording repeats MEMORY_RULES /
PROFILE_RULES, which are in the same call (and which the code enforces anyway): when to use set_situation instead of
save_memory, that a reminder needs the user's explicit request, what intent=plan/conditional/wish mean... Here every
tool keeps its name, parameters, types, enums and required fields; only descriptions that the rules already say are
shortened or dropped. Formats the model must produce (event_date, until, key/value pairs) stay.

VIOLET_MEMORY_COMPACT_TOOLS=1 (default) uses them; 0 = the full descriptions (tools/decision_eval.py compares both
with the real model).
"""

from __future__ import annotations

import copy
from typing import Any

from ai.settings import env_bool

_DROP = object()          # remove the parameter's description

# tool -> (new tool description or None = keep, {parameter: new description | _DROP})
COMPACT: dict[str, tuple[str | None, dict[str, Any]]] = {
    # Kept word for word where a real failure put them there (git log -S on ai/tool_schemas.py): phase 9.4 r2 (a leave
    # of absence saved as a memory instead of a situation -> the boundary sits in BOTH tools and the rules), B6
    # (new_event=true passed early), 9.1 (the shared evidence text), 9.1.1 (correct_profile_fact: unsure -> write
    # nothing), 9.2.1 (intent: a conditional idea is not a plan).
    # save_memory: NOT shortened (0.7.37). The real-model comparison on the user's server (decision_eval, 2026-11-03)
    # had the short form skip "下周面试" (event) while the full form saved it -- the main memory tool stays full.
    "update_memory": ("把新信息合并进一条已有记忆。",
                      {"memory_id": _DROP, "summary": "更新后的完整内容", "category": _DROP, "importance": "1-5",
                       "emotional_weight": "1-5", "reason": _DROP}),
    "reinforce_memory": ("用户又提到同一事实、没有新信息时，强化那条记忆。", {"memory_id": _DROP, "reason": _DROP}),
    "upsert_core_memory": (
        "新增或更新核心长期记忆（长期画像、回复偏好、安慰偏好、称呼偏好、边界、关系风格）。",
        {"memory_key": "稳定的 key，如 reply_style、nickname_preference；同义用同一个 key",
         "category": "如 reply_style、boundary、preference、user_profile", "content": "完整内容",
         "priority": "1-5", "confidence": "0-1"},
    ),
    "update_core_memory": ("更新已有核心记忆。", {"memory_id": _DROP, "content": "完整内容", "category": _DROP,
                                               "memory_key": _DROP, "priority": "1-5", "confidence": "0-1"}),
    "reinforce_core_memory": ("用户重复同一长期偏好、没有新信息时强化它。", {"memory_id": _DROP, "reason": _DROP}),
    "update_event_progress": ("更新已有事件的进展。", {"memory_id": _DROP, "summary": "更新后的完整描述（可选）",
                                                  "reason": "依据"}),
    "correct_memory": ("用户指出记错了：换成正确内容。", {"corrected_summary": "纠正后的完整内容", "reason": "用户原话"}),
    "retract_memory": ("用户说某条记忆根本不对：撤销它。",
                       {"forget": "用户原话明确要求忘掉/删掉时为 true（连聊天里的原话一起抹掉，不能恢复）"}),
    "create_commitment": ("用户明确要求提醒/跟进时创建提醒。",
                          {"content": "要提醒的事", "evidence": "用户提出请求的原话（逐字）", "due_time": "ISO",
                           "due_date": "today/tomorrow/YYYY-MM-DD", "due_clock": "HH:MM 或 早上/中午/下午/晚上"}),
    "cancel_commitment": ("用户说不用提醒了时取消。", {"evidence": "原话（逐字）"}),
    "remember_profile_fact": ("保存用户亲口说的、关于自己的稳定信息。",
                              {"value": "简洁的值，如“大学生”“喜欢打羽毛球”", "evidence": "原话（逐字）",
                               "valid_until": "可选，到什么时候为止（ISO）"}),
    "correct_profile_fact": ("用户纠正之前关于自己的信息时使用。不能确定改哪一条时什么都不写，返回候选。",
                             {"new_value": "纠正后的完整值", "fact_id": "【已有的用户档案】里的 id，优先填",
                              "old_value": "没有 fact_id 时填旧值关键词", "evidence": "原话（逐字）"}),
    "retract_profile_fact": ("用户说某条关于自己的信息不对或不成立、没给新值时撤销。", {"value": "多值 key 时要撤销的值"}),
    "set_situation": (
        "记录用户只在此刻/今天成立的状态（今天回家了、在上课、请假没去上课）。这类状态只用这个工具记，不用 save_memory；"
        "不会改动课表。一句话里有几项就分别调用一次；用户改口时重新记变了的那几项，新的替换同一类旧的。",
        {"evidence": "原话（逐字）"},
    ),
    "set_interaction_preference": (
        "用户明确提出交流方式的要求时记录。value：reply_length=short|normal|long；proactive_chat=welcome|less|no；"
        "follow_up_questions=ok|less|no；advice=welcome|only_when_asked|no；humor=likes|neutral|no；avoid_topic=话题",
        {"evidence": "原话（逐字）"},
    ),
    "decline_profile_question": ("用户不想回答某个了解类问题时调用。", {"evidence": "原话（逐字）"}),
}


def enabled() -> bool:
    return env_bool("VIOLET_MEMORY_COMPACT_TOOLS", True)


def compact(tool: dict[str, Any]) -> dict[str, Any]:
    """The short form of one tool schema (unchanged when it has none)."""
    fn = tool.get("function") or {}
    plan = COMPACT.get(fn.get("name"))
    if plan is None:
        return tool
    out = copy.deepcopy(tool)
    description, params = plan
    if description:
        out["function"]["description"] = description
    props = (out["function"].get("parameters") or {}).get("properties") or {}
    for name, text in params.items():
        if name not in props:
            continue
        if text is _DROP:
            props[name].pop("description", None)
        else:
            props[name]["description"] = text
    return out


def apply(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [compact(t) for t in tools] if enabled() else tools
