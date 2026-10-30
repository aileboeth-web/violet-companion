"""
ProactiveAgent: 主动触达决策 agent。

职责：接收 scheduler 生成的主动事件上下文，判断是否应该主动给用户发消息，
以及是否需要下发受限任务。它不直接发送消息，也不直接执行任务。
"""

import json
from typing import Any

from ai.chat import violetdb, format_active_tasks
from ai.context_engine import ContextEngine
from .base_agent import BaseAgent


class ProactiveAgent(BaseAgent):
    """
    主动触达决策 agent。

    从 .env 读取提示词的环境变量：PROACTIVE_LLM_PROMPT
    """

    mcp_agent_name = "proactive"
    model_env_key = "PROACTIVE_LLM_MODEL"
    default_model = "gpt-5-mini"
    system_prompt_env_key = "PROACTIVE_LLM_PROMPT"
    # 2026-10-30: v3 = v1 without the built-in "虚拟女友" (the relationship section says how close she is)
    default_prompt_file = "persona/proactive_prompt_versions/proactive.v3.txt"
    temperature_env_key = "PROACTIVE_LLM_MODEL_TEMPERATURE"
    default_temperature = 0.2
    default_system_prompt = (
        "你是 Violet 的主动触达决策 agent。你只负责判断当前是否应该主动联系用户，"
        "以及是否需要下发一个已经被配置文件授权的任务。"
        "你必须严格输出 JSON，不要输出 Markdown 或解释。"
    )

    def register_tools(self) -> None:
        """ProactiveAgent 第一版不注册工具，只做决策。"""
        pass

    def build_context_messages(self, user_message: str) -> list[dict[str, Any]]:
        output_contract = {
            "should_act": "boolean，本次是否应该主动触达用户",
            "action_type": "none/chat/task，none 表示不触达，chat 表示只发消息，task 表示需要先执行任务",
            "reason": "简短中文原因",
            "topic_key": "本次话题的简短英文标识，例如 daily_checkin / music / food；同一话题始终用同一个 key",
            "mentioned_memory_ids": "本次会提到的记忆 id 列表，只能取自 conversation_state.allowed_memories；没有则为 []",
            "mentioned_life_event_ids": "如果分享 Violet 自己的生活小事，填 conversation_state.allowed_life_events 中的 id；否则 []",
            "mentioned_web_finding_ids": "如果提到 Violet 在网上看到的东西，填 conversation_state.allowed_web_findings 中的 id；否则 []",
            "chat_brief": {
                "tone": "给 ChatAgent 的语气要求",
                "topic": "主动消息主题",
                "time_bucket": "morning/noon/afternoon/evening/night/other",
                "must_not_say": ["不能说的内容，可为空"],
            },
            "task": {
                "name": "任务名称；无任务时为 null",
                "description": "任务描述",
                "task_info": "任务初始信息 object",
                "constraints": "任务限制 object",
            },
        }
        guardrails = (
            "硬性规则：\n"
            "1. 如果配置或 event_context 不允许任务，action_type 不能是 task。\n"
            "2. 如果要下发任务，只能选择 event_context.allowed_tasks 中明确允许的任务。\n"
            "3. event_context.allowed_tasks 可能包含多个任务；每个任务都有 description、task_info、constraints、频率和时间窗。\n"
            "4. 任务必须遵守 event_context 中的预算、时间窗、每日/每周次数、商家、地址、忌口等限制。\n"
            "5. task.description 应基于所选任务的 description，task_info/constraints 应合并配置给出的默认值。\n"
            "6. 不要把主动事件当作用户新说的话。\n"
            "7. 文案应自然、短、温柔，不要使用强迫或责备语气。\n"
            "8. 输出必须是可被 json.loads 解析的单个 JSON 对象。\n"
            "9. 不发送是正常结果：没有自然、值得说的内容时 should_act=false，不要为了触发而硬发。\n"
            "10. conversation_state.restricted_memories 里的事件（计划中/进行中/进展未知/已结束）不得主动提起或追问；"
            "时间过去不代表事情有了结果。只有用户明确要求的提醒才会由系统另行发送。\n"
            "11. conversation_state.cooling_topics 是你最近主动提过、对方没有积极回应的话题，不要换个说法再提。\n"
            "12. 只有 conversation_state.allowed_memories 中的内容可以作为自然话题，并把 id 写进 mentioned_memory_ids："
            "type=fact 是长期事实；type=upcoming_event 是用户最近亲口提到、还没发生的事，只能在发生前自然地提一次（例如祝顺利），"
            "不要追问结果。conversation_state.avoid_topics 是用户说过不想反复聊的话题。\n"
            "13. conversation_state.cold_streak 是你连续主动发出却没有得到积极回应的消息数，数值越大越应该保持安静。\n"
            "14. conversation_state.allowed_life_events 是 Violet 最近确实发生过、还没告诉用户的生活小事，可以偶尔自然地分享一件"
            "（写进 mentioned_life_event_ids）；分享不是任务，不发也完全正常；不要分享其他没有记录的经历。\n"
            "15. conversation_state.allowed_web_findings 是 Violet 在网上看到的、可能和用户兴趣有关的外部资料（不是她的经历）。"
            "大多数时候不必提；要提就写进 mentioned_web_finding_ids，说成“看到一篇…”，并说明来源网站。\n"
            "以上第 10–12、14、15 条由系统代码校验，违反的决定会被直接丢弃。"
        )
        # Phase 3: profile / situation / preferences come from the shared ContextEngine;
        # recent chat is passed as real dialogue turns.
        packet = ContextEngine(violetdb).build("decision", "", {})
        from ai.agents.chat_agent import adapt_messages_for_channel      # phase 10: where the message will go
        return adapt_messages_for_channel([
            {"role": "system", "content": self._config.reply_prompt},
            {"role": "system", "content": guardrails},
            {"role": "system", "content": f"输出 JSON 结构：\n{json.dumps(output_contract, ensure_ascii=False)}"},
            *({"role": "system", "content": section} for section in packet.sections),
            {"role": "system", "content": format_active_tasks()},
            *packet.history,
            {"role": "user", "content": user_message},
        ])
