"""Image understanding for incoming pictures (the "看图" model of the Control Center's 图片 page).

0.7.6: the call goes through the same OpenAI SDK client as the chat agents (same address, key, proxy handling and
request shape), instead of a hand-made requests.post that a relay could treat differently. The model is
VISION_LLM_MODEL, or the Chat model when that is empty (the old default "vision" exists on almost no service).
A request the service rejects (HTTP 400, e.g. a parameter it does not take) is tried once more with only model and
messages. Failures raise VisionError with the HTTP status and the service's own message, so the page and the log
say why.
"""

from __future__ import annotations

import base64
import os

from dotenv import load_dotenv

from ai.settings import ENV_PATH, env_float, env_str

_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_MAGIC = ((b"\x89PNG", "image/png"), (b"\xff\xd8", "image/jpeg"), (b"GIF8", "image/gif"))

VISION_PROMPT = (
    "请仔细观察这张图片，只描述你实际看到的内容。"
    "不要猜测图片中没有明确看到的信息。"
    "请用简洁自然的中文描述，方便另一个聊天模型继续理解用户发来的图片。"
)


class VisionError(RuntimeError):
    pass


def vision_model() -> str:
    """VISION_LLM_MODEL, else the Chat model. "vision" was the old env_example placeholder (copied into installs'
    .env), not a real model name: it counts as not chosen."""
    chosen = env_str("VISION_LLM_MODEL", "")
    return chosen if chosen and chosen != "vision" else env_str("CHAT_LLM_MODEL", "")


def _mime(file_path: str, data: bytes) -> str:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return _MIME_MAP.get(os.path.splitext(file_path)[1].lower(), "image/jpeg")


def _client(api_url: str, api_key: str):
    from openai import OpenAI, Timeout
    from ai.agents.base_agent import BaseAgent, _local_address
    if BaseAgent.client_factory is not None:              # the offline tests' fake model service
        from ai.chat import LLMConfig
        cfg = LLMConfig()
        cfg.reply_model = vision_model()
        return BaseAgent.client_factory(cfg)
    extra = {}
    if _local_address(api_url):                           # same rule as the chat agents: no system proxy for LAN
        from openai import DefaultHttpxClient
        extra["http_client"] = DefaultHttpxClient(trust_env=False)
    return OpenAI(api_key=api_key, base_url=api_url, max_retries=0,
                  timeout=Timeout(env_float("VISION_TIMEOUT_SECONDS", 60.0), connect=10.0), **extra)


def _reason(exc: Exception) -> str:
    """"HTTP 404：model vision not found" -- status and the service's message, short."""
    status = getattr(exc, "status_code", None)
    message = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        message = str(err.get("message") or "") if isinstance(err, dict) else str(err)
    if not message:
        message = str(getattr(exc, "message", "") or exc)
    message = " ".join(message.split())[:240]
    if status:
        return f"HTTP {status}：{message}" if message else f"HTTP {status}"
    return f"{exc.__class__.__name__}：{message}" if message else exc.__class__.__name__


def describe_image(file_path: str | os.PathLike[str]) -> str:
    """Return a short Chinese description of the image. Raises VisionError."""
    file_path = os.fspath(file_path)
    if not os.path.exists(file_path):
        raise VisionError(f"图片文件不见了：{os.path.basename(file_path)}")

    load_dotenv(ENV_PATH, override=False)
    from ai.chat import get_llm_api_url
    api_url = get_llm_api_url().rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        raise VisionError("还没有设置模型服务的 API Key")
    model = vision_model()
    if not model:
        raise VisionError("还没有选识图模型（也没有 Chat 模型可用）")

    with open(file_path, "rb") as f:
        data = f.read()
    url = f"data:{_mime(file_path, data)};base64,{base64.b64encode(data).decode('ascii')}"
    messages = [{"role": "user", "content": [{"type": "text", "text": VISION_PROMPT},
                                             {"type": "image_url", "image_url": {"url": url}}]}]

    from openai import BadRequestError
    from ai import trace
    from ai.model_params import apply as apply_model_params
    client = _client(api_url, api_key)
    full = apply_model_params({"model": model, "messages": messages, "reasoning_effort": "low", "max_tokens": 500})
    try:
        try:
            response = client.chat.completions.create(**full)
        except BadRequestError as exc:
            # a service that does not take max_tokens / reasoning_effort: once more with the bare request
            trace.step("识图", 模型=model, 结果="重试", 原因=_reason(exc))
            response = client.chat.completions.create(model=model, messages=messages)
    except Exception as exc:
        reason = _reason(exc)
        trace.step("识图", 模型=model, 结果="失败", 原因=reason)
        raise VisionError(f"识图模型 {model} 调用失败：{reason}") from exc

    try:
        description = (response.choices[0].message.content or "").strip()
    except (AttributeError, IndexError, TypeError):
        description = ""
    if not description:
        trace.step("识图", 模型=model, 结果="空回复")
        raise VisionError(f"识图模型 {model} 没有返回描述（这个模型可能不支持看图）")
    trace.step("识图", 模型=model, 结果="ok", 描述=trace.text(description))
    return description
