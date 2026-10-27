"""Image understanding for incoming WeChat pictures.

Moved out of main.Eve_File unchanged in behaviour (same OpenAI-compatible
/chat/completions payload with an image_url data URL); what changed is that
failures raise VisionError instead of silently returning, so the caller can
record the error and still answer the user.
"""

from __future__ import annotations

import base64
import os

import requests
from dotenv import load_dotenv

from ai.settings import ENV_PATH, env_float

_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

VISION_PROMPT = (
    "请仔细观察这张图片，只描述你实际看到的内容。"
    "不要猜测图片中没有明确看到的信息。"
    "请用简洁自然的中文描述，方便另一个聊天模型继续理解用户发来的图片。"
)


class VisionError(RuntimeError):
    pass


def describe_image(file_path: str | os.PathLike[str]) -> str:
    """Return a short Chinese description of the image. Raises VisionError."""
    file_path = os.fspath(file_path)
    if not os.path.exists(file_path):
        raise VisionError(f"image file missing: {os.path.basename(file_path)}")

    # 确保 .env 中的配置已加载
    load_dotenv(ENV_PATH, override=False)
    api_url = os.getenv("LLM_API_URL", "http://127.0.0.1:666/v1").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        raise VisionError("LLM_API_KEY 未配置")

    mime_type = _MIME_MAP.get(os.path.splitext(file_path)[1].lower(), "image/jpeg")
    with open(file_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("ascii")

    payload = {
        "model": os.getenv("VISION_LLM_MODEL", "").strip() or "vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
                ],
            }
        ],
        "reasoning_effort": "low",
        "max_tokens": 500,
        "stream": False,
    }
    from ai.model_params import apply as apply_model_params
    payload = apply_model_params(payload)
    try:
        response = requests.post(
            f"{api_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=(10, env_float("VISION_TIMEOUT_SECONDS", 60.0)),
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise VisionError(f"vision request failed: {exc.__class__.__name__}") from exc

    try:
        description = (result.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    except (AttributeError, IndexError, TypeError):
        description = ""
    if not description:
        raise VisionError("vision model returned no description")
    return description
