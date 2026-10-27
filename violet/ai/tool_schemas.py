"""
Tool schemas for all agents.

Each schema follows the OpenAI function tool format:
{
    "type": "function",
    "function": {
        "name": str,
        "description": str,
        "parameters": { ... JSON Schema ... }
    }
}
"""

# ---------------------------------------------------------------------------
# MemoryAgent tools
# ---------------------------------------------------------------------------

# Phase 9.1 (A7): a memory the user confirmed (corrected in the dashboard, or corrected in chat with their own words)
# only changes its content with the user's words of THIS turn, quoted verbatim; the system checks them.
EVIDENCE_PROPERTY = {
    "type": "string",
    "description": "用户本轮原话里说明这次修改的那句话（逐字）。修改用户确认过的记忆时必填，系统会核验。",
}

SAVE_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "save_memory",
        "description": (
            "保存一条以后还值得记得的记忆。用户此刻 / 今天的状态（在哪、在做什么、方不方便、请假或不去某节课、心情）"
            "不是记忆，用 set_situation，不要用这个工具。相似内容不会被自动合并：如果返回 needs_decision，"
            "请用 related_to=<候选id>（同一件事的新进展）或 new_event=true（另一件事）再次调用。"
            "已有事件的进展或结果请优先用 update_event_progress。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "对需要记忆的内容的简洁描述。",
                },
                "category": {
                    "type": "string",
                    "description": "记忆类别，例如：event、preference、情感状态等。",
                },
                "importance": {
                    "type": "number",
                    "description": "重要性权重，1-5，越重要越高。默认 1。",
                },
                "emotional_weight": {
                    "type": "number",
                    "description": "情绪权重，1-5，越强烈越高。默认 1。",
                },
                "happened_at": {
                    "type": "string",
                    "description": "事件发生时间，可为空，例如 2026-04-29T12:00:00+08:00。",
                },
                "valid_until": {
                    "type": "string",
                    "description": "有效期（ISO 时间），可为空，系统会按类型给出默认值。",
                },
                "kind": {
                    "type": "string",
                    "enum": ["fact", "event", "background"],
                    "description": ("fact=用户长期事实；event=具体事件；background=普通聊天背景（用户此刻在哪、在做什么、"
                                    "有没有空不是背景，用 set_situation）。默认 event。"),
                },
                "progress": {
                    "type": "string",
                    "enum": ["planned", "ongoing", "done", "cancelled", "unknown"],
                    "description": "事件进展。只有用户明确说结束/取消时才用 done/cancelled；无法判断用 unknown。",
                },
                "event_time": {"type": "string", "description": "事件的绝对时间（ISO），例如 2026-10-12T10:00:00+08:00。"},
                "event_date": {"type": "string", "description": "没有绝对时间时：today/tomorrow/day_after_tomorrow/yesterday 或 YYYY-MM-DD。"},
                "event_clock": {"type": "string", "description": "配合 event_date 的时刻 HH:MM，或 早上/中午/下午/晚上。"},
                "intent": {"type": "string", "enum": ["plan", "conditional", "wish"],
                           "description": "将来的事有多确定：plan=说定了；conditional=带条件（“要是……就……”）；wish=随口的想法。"},
                "condition": {"type": "string", "description": "intent=conditional 时的条件，例如“周三下午没课”。"},
                "event_key": {"type": "string", "description": "同一件持续事件的稳定英文标识，例如 server_outage。"},
                "related_to": {"type": "integer", "description": "这条信息属于哪条已有记忆（id，来自 query_memory 或 needs_decision 候选）。"},
                "new_event": {"type": "boolean", "description": "看过本轮 needs_decision 列出的候选、确认这是另一件事时设为 true；"
                                                                 "没看过候选时传 true 会先返回候选。"},
                "evidence": EVIDENCE_PROPERTY,
            },
            "required": ["summary", "category"],
        },
    },
}

UPDATE_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_memory",
        "description": "更新一条已有记忆。用于把新信息合并进已有记忆，避免重复保存相似内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "integer",
                    "description": "要更新的记忆 ID。必须来自上下文中的相关记忆。",
                },
                "summary": {
                    "type": "string",
                    "description": "更新后的完整记忆内容，而不是只写新增片段。",
                },
                "category": {
                    "type": "string",
                    "description": "记忆类别，可不填，默认沿用原类别。",
                },
                "importance": {
                    "type": "number",
                    "description": "重要性权重，1-5。",
                },
                "emotional_weight": {
                    "type": "number",
                    "description": "情绪权重，1-5。",
                },
                "reason": {
                    "type": "string",
                    "description": "为什么更新这条记忆。",
                },
                "evidence": EVIDENCE_PROPERTY,
            },
            "required": ["memory_id", "summary"],
        },
    },
}

REINFORCE_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "reinforce_memory",
        "description": "强化一条已有记忆。用于用户再次提到同一事实但没有新增信息时，增加该记忆权重而不重复保存。",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "integer",
                    "description": "要强化的记忆 ID。必须来自 query_memory 返回的记忆。",
                },
                "reason": {
                    "type": "string",
                    "description": "为什么强化这条记忆。",
                },
            },
            "required": ["memory_id"],
        },
    },
}

UPSERT_CORE_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "upsert_core_memory",
        "description": "新增或更新核心长期记忆。用于保存用户长期画像、回复偏好、安慰偏好、称呼偏好、边界和关系风格。",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_key": {
                    "type": "string",
                    "description": "稳定唯一 key，例如 reply_style、comfort_style、nickname_preference、user_pet_preference。相似含义应使用同一个 key。",
                },
                "category": {
                    "type": "string",
                    "description": "类别，例如 reply_style、comfort_style、boundary、nickname、relationship、preference、user_profile。",
                },
                "content": {
                    "type": "string",
                    "description": "核心记忆完整内容。",
                },
                "priority": {
                    "type": "integer",
                    "description": "注入优先级 1-5，越高越应该提供给 ChatAgent。",
                },
                "confidence": {
                    "type": "number",
                    "description": "确定度 0-1。",
                },
                "evidence": EVIDENCE_PROPERTY,
            },
            "required": ["memory_key", "category", "content"],
        },
    },
}

UPDATE_CORE_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_core_memory",
        "description": "更新已有核心记忆。用于合并相似但更具体的新信息，避免重复核心记忆。",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer", "description": "核心记忆 ID。"},
                "content": {"type": "string", "description": "更新后的完整核心记忆内容。"},
                "category": {"type": "string", "description": "类别，可选。"},
                "memory_key": {"type": "string", "description": "稳定 key，可选。"},
                "priority": {"type": "integer", "description": "优先级 1-5，可选。"},
                "confidence": {"type": "number", "description": "确定度 0-1，可选。"},
                "evidence": EVIDENCE_PROPERTY,
            },
            "required": ["memory_id", "content"],
        },
    },
}

REINFORCE_CORE_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "reinforce_core_memory",
        "description": "强化已有核心记忆。用于用户重复表达同一长期偏好但无新增信息时增加 mention_count。",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer", "description": "核心记忆 ID。"},
                "reason": {"type": "string", "description": "强化原因。"},
            },
            "required": ["memory_id"],
        },
    },
}

UPDATE_EVENT_PROGRESS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_event_progress",
        "description": "用户明确告知已有事件的新进展（例如“服务器修好了”）时，更新该事件的进展和内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer", "description": "事件记忆 id，必须来自 query_memory 或上下文。"},
                "progress": {"type": "string", "enum": ["planned", "ongoing", "done", "cancelled", "unknown"]},
                "summary": {"type": "string", "description": "更新后的完整事件描述（可选）。"},
                "reason": {"type": "string", "description": "依据：用户原话或明确信息。"},
                "evidence": EVIDENCE_PROPERTY,
            },
            "required": ["memory_id", "progress", "reason"],
        },
    },
}

CORRECT_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "correct_memory",
        "description": "用户指出 Violet 记错了：用正确内容替换该记忆（旧内容保留在修订记录中）。",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "corrected_summary": {"type": "string", "description": "纠正后的完整内容。"},
                "progress": {"type": "string", "enum": ["planned", "ongoing", "done", "cancelled", "unknown"]},
                "reason": {"type": "string", "description": "用户的纠正原话。"},
                "evidence": EVIDENCE_PROPERTY,
            },
            "required": ["memory_id", "corrected_summary", "reason"],
        },
    },
}

RETRACT_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "retract_memory",
        "description": "用户说某条记忆根本不对/不存在时，撤销该记忆（不再被使用，修订记录保留）。",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "reason": {"type": "string"},
                "evidence": EVIDENCE_PROPERTY,
                "forget": {"type": "boolean", "description": "只有用户原话明确要求忘掉/别记/删掉这件事时设为 true（evidence 逐字引用那句话）："
                                                             "连同当时聊天记录里说这件事的句子一起抹掉，不能恢复。"},
            },
            "required": ["memory_id", "reason"],
        },
    },
}

CREATE_COMMITMENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_commitment",
        "description": (
            "仅当用户原话明确要求提醒/跟进时创建约定提醒。evidence 必须逐字引用用户原话，系统会核验。"
            "普通事件分享不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要提醒的事情，例如“交作业”。"},
                "evidence": {"type": "string", "description": "用户原话中明确提出请求的那句话（逐字）。"},
                "due_time": {"type": "string", "description": "提醒的绝对时间（ISO）。"},
                "due_date": {"type": "string", "description": "或：today/tomorrow/YYYY-MM-DD。"},
                "due_clock": {"type": "string", "description": "配合 due_date：HH:MM 或 早上/中午/下午/晚上。"},
                "kind": {"type": "string", "enum": ["reminder", "follow_up"]},
            },
            "required": ["content", "evidence"],
        },
    },
}

CANCEL_COMMITMENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "cancel_commitment",
        "description": "用户明确说不用提醒/不用跟进了时，取消对应约定。",
        "parameters": {
            "type": "object",
            "properties": {
                "commitment_id": {"type": "integer"},
                "evidence": {"type": "string", "description": "用户取消的原话（逐字）。"},
            },
            "required": ["commitment_id", "evidence"],
        },
    },
}

_PROFILE_KEY_ENUM = [
    "address_name", "identity.status", "identity.background", "study.school", "study.major", "study.grade",
    "work.role", "residence.usual", "routine.schedule", "interest", "boundary", "other",
]

REMEMBER_PROFILE_FACT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "remember_profile_fact",
        "description": (
            "保存用户亲口说出的、关于自己的稳定信息。evidence 必须逐字引用用户原话；"
            "没有原话依据的推测只会成为未确认候选。只在此刻成立的状态请用 set_situation。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "enum": _PROFILE_KEY_ENUM},
                "value": {"type": "string", "description": "简洁的值，例如“大学生”“平时住学校宿舍”“喜欢打羽毛球”。"},
                "evidence": {"type": "string", "description": "用户原话中说明这一点的那句话（逐字）。"},
                "valid_until": {"type": "string", "description": "可选：这条信息到什么时候为止（ISO），例如“这学期住校”。"},
            },
            "required": ["key", "value", "evidence"],
        },
    },
}

CORRECT_PROFILE_FACT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "correct_profile_fact",
        "description": "用户纠正之前关于自己的信息时使用；旧值保留为历史，新值成为当前值。不能确定改哪一条时什么都不写，返回候选。",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "enum": _PROFILE_KEY_ENUM},
                "new_value": {"type": "string", "description": "纠正后的完整值。"},
                "fact_id": {"type": "integer", "description": "要纠正的那条档案的 id（【已有的用户档案】里的 id=…）。优先填它。"},
                "old_value": {"type": "string", "description": "没有 fact_id 时可填旧值中的关键词（例如“豆豆”）；只能唯一对上一条时才会纠正。"},
                "evidence": {"type": "string", "description": "用户纠正的原话（逐字）。"},
            },
            "required": ["key", "new_value", "evidence"],
        },
    },
}

RETRACT_PROFILE_FACT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "retract_profile_fact",
        "description": "用户说某条关于自己的信息不对或已经不成立、但没有给出新值时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "enum": _PROFILE_KEY_ENUM},
                "value": {"type": "string", "description": "可多值 key 时指定要撤销的值。"},
                "evidence": {"type": "string"},
            },
            "required": ["key", "evidence"],
        },
    },
}

SET_SITUATION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "set_situation",
        "description": (
            "记录用户只在此刻/今天成立的状态（今天回家了、在上课、在开会、准备睡了、今天下午请假没去上课）。"
            "这类状态只用这个工具记，不用 save_memory（也不要两边各记一份）：它会过期，过期后不再代表现在，"
            "也不会改动课表。一句话里有几项就分别调用一次；用户改口时把变了的那几项重新记，新的会替换同一类的旧的。"),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["location", "activity", "availability", "mood"],
                         "description": ("location=此刻在哪；activity=正在做什么；availability=接下来方不方便、有没有空"
                                         "（在忙、请假、不去某节课、没课了）；mood=心情。")},
                "value": {"type": "string"},
                "evidence": {"type": "string", "description": "用户原话（逐字）。"},
                "until": {"type": "string", "description": (
                    "有效期：today / tomorrow / 3h / ISO 时间。按用户的原话填：“今天一整天”= today，“这两天”= tomorrow；"
                    "说的是今天的一段时间（今天下午、今晚、下午那节课）也填 today；没说多久就不填。")},
            },
            "required": ["kind", "value", "evidence"],
        },
    },
}

SET_INTERACTION_PREFERENCE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "set_interaction_preference",
        "description": (
            "用户明确提出交流方式的要求时记录。key/value："
            "reply_length=short|normal|long；proactive_chat=welcome|less|no；follow_up_questions=ok|less|no；"
            "advice=welcome|only_when_asked|no；humor=likes|neutral|no；avoid_topic=不想反复聊的话题（自由文本）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "enum": ["reply_length", "proactive_chat", "follow_up_questions", "advice", "humor", "avoid_topic"]},
                "value": {"type": "string"},
                "evidence": {"type": "string", "description": "用户原话（逐字）。"},
            },
            "required": ["key", "value", "evidence"],
        },
    },
}

DECLINE_PROFILE_QUESTION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "decline_profile_question",
        "description": "用户表示不想回答某个了解类问题时调用，之后不再问这一项。",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "enum": _PROFILE_KEY_ENUM},
                "evidence": {"type": "string", "description": "用户拒绝的原话（逐字）。"},
            },
            "required": ["key", "evidence"],
        },
    },
}

QUERY_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_memory",
        "description": (
            "查询事件记忆。用于在判断意图时回忆用户提到过的事件、偏好、地点、计划等。"
            "返回的每条记忆包含 kind（fact/event/background/legacy）、progress（事件进展）、event_time，"
            "以及 status 字段，表示该记忆的遗忘程度：\n"
            "- active：记忆清晰，仍在有效期内，可直接使用；\n"
            "- expired：记忆开始模糊，相关性降低，参考时应有所保留；\n"
            "- archived：记忆已相当久远，只应作为远期背景参考，不宜当作近期事实；\n"
            "- forgotten：记忆几乎消失，内容可能不准确，不建议使用；\n"
            "- deleted：记忆已被清除，不应再引用。\n"
            "memory_strength 越大表示记忆越稳固、越不容易遗忘；similarity 越大表示与本次查询越相关。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要查询的自然语言内容，例如：用户昨天提到的事、用户喜欢的饮品、约好的事等。",
                },
                "category": {
                    "type": "string",
                    "description": "可选记忆类别，例如 event、preference、情感状态等。",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回几条，默认 5。",
                },
                "min_similarity": {
                    "type": "number",
                    "description": "MemoryAgent最低相似度，默认0.25，查重时可适当降低；IntentAgent使用固定0.35，本参数不改变其阈值。",
                },
            },
            "required": ["query"],
        },
    },
}

# ---------------------------------------------------------------------------
# Task tools
# ---------------------------------------------------------------------------

CREATE_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_task",
        "description": "创建一个新任务，供 performer_agent 后续执行。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_name": {
                    "type": "string",
                    "description": "任务名称，例如：查一下明天的课。",
                },
                "task_description": {
                    "type": "string",
                    "description": "任务描述，说明用户希望完成什么。",
                },
                "task_info": {
                    "type": "object",
                    "description": "已知任务状态 JSON，例如餐厅、餐品、地址等。",
                    "additionalProperties": True,
                },
                "last_question": {
                    "type": "string",
                    "description": "如果任务缺少信息，下一步要问用户的问题。",
                },
            },
            "required": ["task_name", "task_description"],
        },
    },
}

GET_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_task",
        "description": "查询指定任务会话的完整状态。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "任务会话 ID。",
                }
            },
            "required": ["task_id"],
        },
    },
}

LIST_ACTIVE_TASKS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_active_tasks",
        "description": "查询当前所有未完成任务会话。",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

UPDATE_TASK_INFO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_task_info",
        "description": "合并保存用户已经补充的任务信息 task_info，例如商品、数量、规格、地址。默认不会清空已有字段。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "任务会话 ID。",
                },
                "task_info": {
                    "type": "object",
                    "description": "要合并进已有 task_info 的增量信息，例如 {\"items\": \"板烧鸡腿堡套餐\"}。不要只因为本轮没提到某字段就把它设为 null。",
                    "additionalProperties": True,
                },
                "merge": {
                    "type": "boolean",
                    "description": "是否合并到已有 task_info。默认为 true。只有明确要整体替换时才传 false。",
                },
            },
            "required": ["task_id", "task_info"],
        },
    },
}

UPDATE_TASK_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_task_status",
        "description": "更新任务状态，可同时写入 last_question、result 或 error_message。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "任务会话 ID。",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending_info", "completed", "failed", "cancelled"],
                    "description": "任务状态。",
                },
                "last_question": {
                    "type": "string",
                    "description": "下一步要问用户的问题。",
                },
                "result": {
                    "type": "object",
                    "description": "任务结果 JSON。",
                    "additionalProperties": True,
                },
                "error_message": {
                    "type": "string",
                    "description": "失败或取消原因。",
                },
            },
            "required": ["task_id", "status"],
        },
    },
}

# ---------------------------------------------------------------------------
# Proactive tools / structured actions
# ---------------------------------------------------------------------------

PROACTIVE_DECISION_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "proactive_decision",
        "schema": {
            "type": "object",
            "properties": {
                "should_act": {
                    "type": "boolean",
                    "description": "本次是否应该主动触达用户。",
                },
                "action_type": {
                    "type": "string",
                    "enum": ["none", "chat", "task"],
                    "description": "主动动作类型。none 表示不触达；chat 表示只聊天；task 表示需要先执行任务。",
                },
                "reason": {
                    "type": "string",
                    "description": "简短说明为什么做出这个决定。",
                },
                "chat_brief": {
                    "type": "object",
                    "description": "传给 ChatAgent 的主动消息生成摘要。",
                    "properties": {
                        "tone": {"type": "string"},
                        "topic": {"type": "string"},
                        "time_bucket": {"type": "string"},
                        "must_say": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "must_not_say": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "additionalProperties": True,
                },
                "task": {
                    "type": ["object", "null"],
                    "description": "需要执行任务时填写；不需要任务时为 null。",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "task_info": {
                            "type": "object",
                            "additionalProperties": True,
                        },
                        "constraints": {
                            "type": "object",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["name", "description"],
                    "additionalProperties": True,
                },
            },
            "required": ["should_act", "action_type", "reason", "chat_brief", "task"],
            "additionalProperties": False,
        },
    },
}


# Phase 7 B.2: completion signal of a background memory job (offered only there).
MEMORY_DONE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory_done",
        "description": "本轮需要的记忆写入已经全部放在这一次回复里调用时，同时调用它（无参数）。"
                       "这些写入全部成功时系统直接结束，不再请你总结；只要还需要先查询、核对或看结果再决定，就不要调用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}
