"""Fakes used by the offline tests: scripted LLM and an in-memory WeChat client."""

from __future__ import annotations

import copy
import itertools
import json
import threading
import time
from typing import Any, Callable

from openai.types.chat import ChatCompletion

_ids = itertools.count(1)


def completion(content: str | None = None, tool_calls: list[dict[str, Any]] | None = None, model: str = "fake") -> ChatCompletion:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": f"call_{next(_ids)}",
                "type": "function",
                "function": {"name": tc["name"], "arguments": json.dumps(tc.get("arguments", {}), ensure_ascii=False)},
            }
            for tc in tool_calls
        ]
    return ChatCompletion.model_validate({
        "id": f"cmpl-{next(_ids)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop", "message": message}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })


def api_connection_error():
    """Build an openai.APIConnectionError without touching the network."""
    import openai
    try:
        import httpx2 as _httpx  # the openai 3.x SDK is built on httpx2
    except ImportError:  # pragma: no cover
        import httpx as _httpx
    return openai.APIConnectionError(request=_httpx.Request("POST", "http://127.0.0.1:9/v1/chat/completions"))


Responder = Callable[[dict[str, Any]], Any]


class FakeLLM:
    """Stand-in for the OpenAI client. Routes by model name to a responder.

    A responder receives the create() kwargs and returns a ChatCompletion, a
    plain string (wrapped as assistant content) or raises.
    """

    def __init__(self, responders: dict[str, Responder] | None = None, default: Responder | None = None):
        self.responders = dict(responders or {})
        self.default = default or (lambda kw: "ok")
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.chat = type("Chat", (), {})()
        self.chat.completions = type("Completions", (), {})()
        self.chat.completions.create = self._create

    def _create(self, **kwargs):
        # Snapshot: the agent keeps appending to the same messages list afterwards.
        kwargs = {**kwargs, "messages": copy.deepcopy(kwargs.get("messages"))}
        with self._lock:
            self.calls.append(kwargs)
        responder = self.responders.get(kwargs.get("model"), self.default)
        result = responder(kwargs)
        if isinstance(result, ChatCompletion):
            return result
        return completion(content=result, model=kwargs.get("model", "fake"))

    def calls_for(self, model: str) -> list[dict[str, Any]]:
        with self._lock:
            return [c for c in self.calls if c.get("model") == model]


def install_fake_llm(monkeypatch, fake: FakeLLM) -> FakeLLM:
    from ai.agents.base_agent import BaseAgent
    monkeypatch.setattr(BaseAgent, "client_factory", lambda cfg: fake)
    return fake


def intent_json(route: str = "chat", **extra) -> str:
    payload = {
        "route": route,
        "should_reply": route != "silent",
        "user_intent": "test",
        "emotion": "neutral",
        "silent_reason": None,
        "active_task_id": None,
        "task": {"exists": route == "task", "name": "测试任务" if route == "task" else None,
                 "description": "测试" if route == "task" else None},
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def default_responders(chat_reply: str = "好呀\n我在听") -> dict[str, Responder]:
    return {
        "fake-intent": lambda kw: intent_json("chat"),
        "fake-chat": lambda kw: chat_reply,
        "fake-memory": lambda kw: json.dumps({"should_save": False, "reason": "test"}),
        "fake-performer": lambda kw: json.dumps({"task_status": "failed", "task_id": None}),
        "fake-proactive": lambda kw: json.dumps({"should_act": False, "action_type": "none", "reason": "test"}),
    }


# ---------------------------------------------------------------------------
# WeChat iLink fake
# ---------------------------------------------------------------------------

class FakeILinkClient:
    """In-memory replacement for wx_ilink_py.ILinkClient. Never touches the network."""

    def __init__(self):
        self.sent: list[dict[str, Any]] = []
        self.typing: list[int] = []
        self.inbox_batches: list[list[dict[str, Any]]] = []
        self.committed_bufs = 0
        self.fail_typing = False
        self.send_failures: list[BaseException | dict[str, Any]] = []   # consumed one per send
        self.download_failures: set[int] = set()                        # item indexes that fail
        self.downloaded: list[dict[str, Any]] = []
        self.fetch_error: BaseException | None = None
        self._lock = threading.Lock()
        self.state = type("State", (), {"logged_in": True})()

    # --- receive -------------------------------------------------------
    def queue_messages(self, messages: list[dict[str, Any]]) -> None:
        with self._lock:
            self.inbox_batches.append(messages)

    def fetch_messages(self, timeout: int = 35, persist_buf: bool = True):
        if self.fetch_error is not None:
            err, self.fetch_error = self.fetch_error, None
            raise err
        with self._lock:
            batch = self.inbox_batches.pop(0) if self.inbox_batches else []
        return [{"raw": m, "message_id": m.get("message_id")} for m in batch], timeout

    def commit_sync_buf(self) -> None:
        self.committed_bufs += 1

    def download_media(self, item: dict[str, Any]):
        self.downloaded.append(item)
        if item.get("_fail_download"):
            raise RuntimeError("simulated CDN download failure")
        return item.get("_path", "fake-image.jpg")

    # --- send ------------------------------------------------------------
    def send_typing(self, status: int = 1, **kwargs):
        if self.fail_typing:
            raise RuntimeError("无法获取 typing_ticket，无法发送输入状态")
        self.typing.append(status)
        return {"ret": 0}

    def send_text(self, text: str, context_token=None, to_user_id=None, client_id=None):
        with self._lock:
            failure = self.send_failures.pop(0) if self.send_failures else None
        if isinstance(failure, BaseException):
            raise failure
        if isinstance(failure, dict):
            return failure
        self.sent.append({"text": text, "client_id": client_id})
        return {"ret": 0}

    def send_image(self, image_path, context_token=None, to_user_id=None):
        """Phase 6: records the image path and its sha256 (no network)."""
        import hashlib
        from pathlib import Path
        with self._lock:
            failure = self.send_failures.pop(0) if self.send_failures else None
        if isinstance(failure, BaseException):
            raise failure
        if isinstance(failure, dict):
            return failure
        data = Path(image_path).read_bytes()
        self.sent.append({"image": str(image_path), "sha256": hashlib.sha256(data).hexdigest()})
        return {"ret": 0}

    @property
    def sent_images(self) -> list[dict[str, Any]]:
        return [m for m in self.sent if "image" in m]

    @property
    def sent_texts(self) -> list[str]:
        return [m["text"] for m in self.sent if "text" in m]


def text_message(text: str, message_id: int | None = None, seq: int | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"message_type": 1, "item_list": [{"type": 1, "text_item": {"text": text}}]}
    if message_id is not None:
        msg["message_id"] = message_id
    if seq is not None:
        msg["seq"] = seq
    return msg


def image_message(message_id: int, fail_download: bool = False) -> dict[str, Any]:
    item: dict[str, Any] = {"type": 2, "image_item": {"media": {"full_url": "https://example.invalid/x"}}}
    if fail_download:
        item["_fail_download"] = True
    return {"message_id": message_id, "message_type": 1, "item_list": [item]}


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def far_quiet_hours() -> dict[str, str]:
    """A one-minute quiet window 12 hours from now.

    An empty "quiet_hours" is merged with the default 23:30-08:00, which made proactive tests
    silently depend on the time of day (phase 7 A.1). Tests that do not test quiet hours use this.
    """
    from datetime import datetime, timedelta
    from ai.settings import now_local
    far = now_local() + timedelta(hours=12)
    return {"start": far.strftime("%H:%M"), "end": (far + timedelta(minutes=1)).strftime("%H:%M")}
