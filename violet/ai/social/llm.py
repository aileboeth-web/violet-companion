"""One model call of the social engine: the same address, key and client rules as the chat agents (offline tests use
BaseAgent.client_factory). The model is VIOLET_SOCIAL_MODEL, else the Chat model."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ai.settings import env_float, env_str

_JSON = re.compile(r"\{.*\}", re.S)


class SocialLLMError(RuntimeError):
    pass


def model_name() -> str:
    return env_str("VIOLET_SOCIAL_MODEL", "") or env_str("CHAT_LLM_MODEL", "")


def _client():
    import os
    from openai import OpenAI, Timeout
    from ai.agents.base_agent import BaseAgent, _local_address
    from ai.chat import LLMConfig, get_llm_api_url
    if BaseAgent.client_factory is not None:
        cfg = LLMConfig()
        cfg.reply_model = model_name()
        return BaseAgent.client_factory(cfg)
    url, key = get_llm_api_url(), os.getenv("LLM_API_KEY", "").strip()
    if not key:
        raise SocialLLMError("还没有设置模型服务的 API Key")
    extra = {}
    if _local_address(url):
        from openai import DefaultHttpxClient
        extra["http_client"] = DefaultHttpxClient(trust_env=False)
    return OpenAI(api_key=key, base_url=url, max_retries=1,
                  timeout=Timeout(env_float("LLM_REQUEST_TIMEOUT_SECONDS", 90.0), connect=10.0), **extra)


def parse_object(text: str | None) -> dict[str, Any]:
    if not text:
        raise SocialLLMError("模型没有回答")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("{"):] if "{" in cleaned else cleaned
    try:
        value = json.loads(cleaned)
    except ValueError:
        match = _JSON.search(cleaned)
        if not match:
            raise SocialLLMError("模型的回答不是 JSON") from None
        try:
            value = json.loads(match.group(0))
        except ValueError:
            raise SocialLLMError("模型的回答不是 JSON") from None
    if not isinstance(value, dict):
        raise SocialLLMError("模型的回答不是 JSON 对象")
    return value


def decide(system: str, user: str, source: str = "social", tools=None,
           max_rounds: int = 4) -> tuple[dict[str, Any], str, int]:
    """(the decision object, model, latency ms). Raises SocialLLMError. source: for the token ledger (ai/usage.py).
    tools (2026-10-30): an ai.social.tools.SocialTools -- the model may call them first, then answers with the JSON."""
    from ai.model_params import apply as apply_model_params
    model = model_name()
    if not model:
        raise SocialLLMError("还没有设置 Chat 模型")
    started = time.monotonic()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    schemas = list(getattr(tools, "schemas", None) or [])
    from ai import usage
    try:
        client = _client()
        content = None
        for round_index in range(max_rounds + 1):
            request = {"model": model, "messages": messages, "temperature": 0.8, "max_tokens": 700}
            if schemas and round_index < max_rounds:
                request.update(tools=schemas, tool_choice="auto")
            response = client.chat.completions.create(**apply_model_params(request))
            usage.record(source, getattr(response, "model", None) or model, getattr(response, "usage", None))
            message = response.choices[0].message
            calls = getattr(message, "tool_calls", None) or []
            if not calls or "tools" not in request:
                content = message.content
                break
            messages.append(message.model_dump(exclude_none=True))
            for call in calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except ValueError:
                    args = {}
                result = tools.run(call.function.name, args if isinstance(args, dict) else {})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            if round_index == max_rounds - 1:
                messages.append({"role": "system", "content": "工具已经用够了，现在按拿到的结果只输出 JSON。"})
    except SocialLLMError:
        raise
    except Exception as exc:                          # noqa: BLE001 -- the engine records it and goes on
        raise SocialLLMError(f"模型调用失败：{exc.__class__.__name__}") from None
    return parse_object(content), model, int((time.monotonic() - started) * 1000)
