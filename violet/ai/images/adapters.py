"""Protocol adapters for image providers (phase 6).

Adapters translate a standard request into one protocol and back. They know
nothing about routing, budgets, storage or WeChat.

Implemented and tested offline:
  mock              local fake (deterministic PNG), never touches the network
  openai_images     OpenAI-compatible Images API:
                      POST {base}/images/generations      (JSON)
                      POST {base}/images/edits            (multipart, image[] + prompt)
                      POST {base}/images/variations       (multipart, image)
                    response {"data": [{"b64_json"} | {"url"}], "usage"?}
  chat_completions  OpenAI-compatible POST {base}/chat/completions:
                      vision: content parts text + image_url data URLs -> text
                      image output: recognised structures only
                        message.images[].image_url.url,
                        message.content[] parts {"type": "image_url"} / {"type": "output_image"},
                        markdown ![...](data:image/...;base64,...) or ![...](https://...)
Extension points (protocol NOT confirmed -> not implemented, never guessed):
  gemini_native, zhipu_native, http_custom -> NativeMultimodalAdapter subclasses
  register_adapter("type", cls) adds new protocols.

Failure semantics (decides whether a retry could charge twice):
  AdapterError.accepted = "no"       request certainly not processed upstream
                                     (connect failure, 4xx validation / auth, 429, 503)
                        = "unknown"  may have been processed (read timeout, connection
                                     dropped after sending, 500 / 502 / 504)
                        = "yes"      processed, but the answer is unusable (unknown format)
"""

from __future__ import annotations

import base64
import json
import os
import re
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from ai.images.registry import ModelEntry

SAFE_STATUS = {400, 401, 403, 404, 405, 413, 415, 422, 429, 503}
RETRYABLE_STATUS = {429, 503}
DATA_URL_RE = re.compile(r"data:(image/[a-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)", re.I)
MD_URL_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")


class AdapterError(Exception):
    def __init__(self, code: str, message: str, accepted: str = "no", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.accepted = accepted
        self.retryable = retryable


@dataclass
class InputImage:
    data: bytes
    mime: str
    asset_id: int | None = None


@dataclass
class RawImage:
    kind: str            # "b64" | "url"
    value: str
    mime: str | None = None


@dataclass
class AdapterRequest:
    kind: str
    entry: ModelEntry
    prompt: str
    inputs: list[InputImage] = field(default_factory=list)
    references: list[InputImage] = field(default_factory=list)
    size: str | None = None
    n: int = 1
    quality: str | None = None
    transparent: bool = False
    idempotency_key: str | None = None


@dataclass
class AdapterOutput:
    images: list[RawImage] = field(default_factory=list)
    text: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    provider_cost: float | None = None


# -- HTTP ---------------------------------------------------------------------------
_TRANSPORT_FACTORY: Callable[[ModelEntry], httpx.BaseTransport | None] = lambda entry: None


def set_transport_factory(factory: Callable[[ModelEntry], httpx.BaseTransport | None]) -> None:
    """Tests install httpx.MockTransport here; production uses the default network transport."""
    global _TRANSPORT_FACTORY
    _TRANSPORT_FACTORY = factory


class HttpAdapterBase:
    def __init__(self, entry: ModelEntry):
        self.entry = entry

    def _headers(self, request: AdapterRequest) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        key = os.environ.get(self.entry.api_key_env, "") if self.entry.api_key_env else ""
        if key:
            headers["Authorization"] = f"Bearer {key}"          # read at call time, never stored
        if request.idempotency_key and self.entry.options.get("send_idempotency_header"):
            headers["Idempotency-Key"] = request.idempotency_key
        return headers

    def _post(self, path: str, request: AdapterRequest, *, json_body: dict | None = None,
              data: dict | None = None, files: list | None = None) -> dict[str, Any]:
        if not self.entry.base_url.lower().startswith(("https://", "http://")):
            raise AdapterError("config", "provider base_url is not http(s)")
        timeout = httpx.Timeout(self.entry.timeout, connect=min(10.0, self.entry.timeout))
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False,
                              transport=_TRANSPORT_FACTORY(self.entry)) as client:
                response = client.post(self.entry.base_url.rstrip("/") + path, headers=self._headers(request),
                                       json=json_body, data=data, files=files)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise AdapterError("connect", f"cannot reach provider ({exc.__class__.__name__})", "no", True) from None
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            raise AdapterError("timeout", f"provider did not answer in time ({exc.__class__.__name__})", "unknown") from None
        except httpx.HTTPError as exc:
            raise AdapterError("transport", f"connection dropped ({exc.__class__.__name__})", "unknown") from None
        if response.status_code in (301, 302, 303, 307, 308):
            raise AdapterError("redirect", "provider endpoint redirected; not followed", "no")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code != 200:
            message = ""
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                message = str(payload["error"].get("message") or payload["error"].get("code") or "")[:200]
            accepted = "no" if response.status_code in SAFE_STATUS else "unknown"
            raise AdapterError(f"http_{response.status_code}", message or f"provider returned {response.status_code}",
                               accepted, response.status_code in RETRYABLE_STATUS)
        if not isinstance(payload, dict):
            raise AdapterError("unknown_response", "provider answered with something that is not JSON", "yes")
        if isinstance(payload.get("error"), dict):
            raise AdapterError("provider_error", str(payload["error"].get("message") or "error")[:200], "yes")
        return payload


def _usage(payload: dict[str, Any]) -> tuple[dict[str, Any], float | None]:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    cost = None
    for key in ("cost", "total_cost"):
        if isinstance(usage.get(key), (int, float)):
            cost = float(usage[key])
    return usage, cost


class OpenAIImagesAdapter(HttpAdapterBase):
    def run(self, request: AdapterRequest) -> AdapterOutput:
        model = self.entry.model_id
        fmt = self.entry.options.get("response_format")              # e.g. "b64_json" when the model supports it
        if request.kind == "image_generate":
            body: dict[str, Any] = {"model": model, "prompt": request.prompt, "n": request.n}
            if request.size:
                body["size"] = request.size
            if request.quality:
                body["quality"] = request.quality
            if request.transparent:
                body["background"] = "transparent"
            if fmt:
                body["response_format"] = fmt
            payload = self._post("/images/generations", request, json_body=body)
        elif request.kind in ("image_edit", "image_variation"):
            images = request.inputs + (request.references if request.kind == "image_edit" else [])
            if not images:
                raise AdapterError("invalid_request", "an input image is required")
            field_name = "image[]" if request.kind == "image_edit" and len(images) > 1 else "image"
            files = [(field_name, (f"input{i}.{img.mime.split('/')[-1]}", img.data, img.mime))
                     for i, img in enumerate(images if request.kind == "image_edit" else images[:1])]
            data: dict[str, Any] = {"model": model, "n": str(request.n)}
            if request.kind == "image_edit":
                data["prompt"] = request.prompt
            if request.size:
                data["size"] = request.size
            if fmt:
                data["response_format"] = fmt
            path = "/images/edits" if request.kind == "image_edit" else "/images/variations"
            payload = self._post(path, request, data=data, files=files)
        else:
            raise AdapterError("capability_not_supported", f"openai_images adapter cannot do {request.kind}")
        items = payload.get("data")
        if not isinstance(items, list) or not items:
            raise AdapterError("unknown_response", "no data[] in images response", "yes")
        images = []
        for item in items:
            if isinstance(item, dict) and item.get("b64_json"):
                images.append(RawImage("b64", item["b64_json"]))
            elif isinstance(item, dict) and item.get("url"):
                images.append(RawImage("url", item["url"]))
            else:
                raise AdapterError("unknown_response", "unrecognised image item", "yes")
        usage, cost = _usage(payload)
        return AdapterOutput(images=images, usage=usage, provider_cost=cost)


def _data_url(img: InputImage) -> str:
    return f"data:{img.mime};base64,{base64.b64encode(img.data).decode('ascii')}"


class ChatCompletionsAdapter(HttpAdapterBase):
    def run(self, request: AdapterRequest) -> AdapterOutput:
        parts: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
        if request.kind == "vision_analyze":
            parts += [{"type": "image_url", "image_url": {"url": _data_url(img)}} for img in request.inputs]
        elif request.kind in ("image_generate", "image_edit"):
            extra = (request.inputs if request.kind == "image_edit" else []) + request.references
            parts += [{"type": "image_url", "image_url": {"url": _data_url(img)}} for img in extra]
        else:
            raise AdapterError("capability_not_supported", f"chat_completions adapter cannot do {request.kind}")
        body: dict[str, Any] = {"model": self.entry.model_id, "messages": [{"role": "user", "content": parts}],
                                "stream": False}
        if request.kind == "vision_analyze":
            body["max_tokens"] = int(self.entry.options.get("max_tokens", 500))
        for key, value in (self.entry.options.get("extra_body") or {}).items():
            body.setdefault(key, value)                    # e.g. {"modalities": ["image", "text"]} once confirmed
        from ai.model_params import apply as apply_model_params
        payload = self._post("/chat/completions", request, json_body=apply_model_params(body))
        usage, cost = _usage(payload)
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise AdapterError("unknown_response", "no choices[0].message", "yes") from None
        if request.kind == "vision_analyze":
            text = message.get("content")
            if isinstance(text, list):
                text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
            if not isinstance(text, str) or not text.strip():
                raise AdapterError("empty_response", "vision model returned no text", "yes")
            return AdapterOutput(text=text.strip(), usage=usage, provider_cost=cost)
        images = extract_chat_images(message)
        if not images:
            raise AdapterError("unknown_response", "no recognised image in the chat response", "yes")
        return AdapterOutput(images=images, text=message.get("content") if isinstance(message.get("content"), str) else None,
                             usage=usage, provider_cost=cost)


def extract_chat_images(message: dict[str, Any]) -> list[RawImage]:
    found: list[RawImage] = []

    def add_url(url: str) -> None:
        m = DATA_URL_RE.fullmatch(url.strip())
        if m:
            found.append(RawImage("b64", re.sub(r"\s+", "", m.group(2)), m.group(1).lower()))
        elif url.lower().startswith(("https://", "http://")):
            found.append(RawImage("url", url))

    for item in message.get("images") or []:
        if isinstance(item, dict):
            url = (item.get("image_url") or {}).get("url") if isinstance(item.get("image_url"), dict) else item.get("url")
            if isinstance(url, str):
                add_url(url)
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url" and isinstance(part.get("image_url"), dict):
                add_url(str(part["image_url"].get("url") or ""))
            elif part.get("type") in ("output_image", "image") and (part.get("b64_json") or part.get("image_base64")):
                found.append(RawImage("b64", part.get("b64_json") or part.get("image_base64")))
    elif isinstance(content, str):
        for m in DATA_URL_RE.finditer(content):
            found.append(RawImage("b64", re.sub(r"\s+", "", m.group(2)), m.group(1).lower()))
        for m in MD_URL_RE.finditer(content):
            add_url(m.group(1))
    return found


class NativeMultimodalAdapter:
    """Extension point for vendor-native protocols (Gemini, Zhipu, local inference ...).

    Subclass, implement run(request) -> AdapterOutput following the same failure
    semantics, and register_adapter(). Until a protocol is confirmed against its
    real documentation and a test call, the entry stays unimplemented.
    """
    protocol = "native"

    def __init__(self, entry: ModelEntry):
        self.entry = entry

    def run(self, request: AdapterRequest) -> AdapterOutput:
        raise AdapterError("adapter_not_implemented",
                           f"{self.protocol} protocol is not implemented until it is confirmed", "no")


class GeminiNativeAdapter(NativeMultimodalAdapter):
    protocol = "gemini_native"


class ZhipuNativeAdapter(NativeMultimodalAdapter):
    protocol = "zhipu_native"


class CustomHttpAdapter(NativeMultimodalAdapter):
    protocol = "http_custom"


# -- mock -----------------------------------------------------------------------------
def make_png(width: int = 64, height: int = 64, color: tuple[int, int, int] = (200, 180, 160)) -> bytes:
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class MockImageAdapter:
    """Offline provider. options.mock: {"fail": [codes...], "return": "b64"|"url", "url_base": "..."}.

    fail codes are consumed one per call: "connect" (not accepted), "timeout_after_accept"
    (uncertain), "http_500", "http_429", "unknown_format".
    """
    calls: list[dict[str, Any]] = []

    def __init__(self, entry: ModelEntry):
        self.entry = entry

    def run(self, request: AdapterRequest) -> AdapterOutput:
        mock = self.entry.options.setdefault("mock", {})
        MockImageAdapter.calls.append({"entry": self.entry.entry_id, "kind": request.kind, "prompt": request.prompt,
                                       "inputs": len(request.inputs), "references": len(request.references),
                                       "n": request.n, "idempotency_key": request.idempotency_key})
        failures = mock.get("fail") or []
        if failures:
            code = failures.pop(0)
            if code == "connect":
                raise AdapterError("connect", "mock: cannot connect", "no", True)
            if code == "http_429":
                raise AdapterError("http_429", "mock: rate limited", "no", True)
            if code == "timeout_after_accept":
                raise AdapterError("timeout", "mock: timed out after sending", "unknown")
            if code == "http_500":
                raise AdapterError("http_500", "mock: server error", "unknown")
            if code == "unknown_format":
                raise AdapterError("unknown_response", "mock: unrecognised answer", "yes")
        if request.kind == "vision_analyze":
            return AdapterOutput(text=mock.get("vision_text", f"一张图片（共 {len(request.inputs)} 张输入）"),
                                 usage={"total_tokens": 10})
        width, height = 64, 64
        if request.size and re.fullmatch(r"\d+x\d+", request.size):
            width, height = (min(int(v), 256) for v in request.size.split("x"))
        images = []
        for i in range(max(1, request.n)):
            png = make_png(width, height, (100 + i * 20, 150, 200))
            if mock.get("return") == "url":
                images.append(RawImage("url", f"{mock.get('url_base', 'https://img.example.org/out')}/{i}.png"))
            else:
                images.append(RawImage("b64", base64.b64encode(png).decode("ascii")))
        return AdapterOutput(images=images, usage={"images": len(images)}, provider_cost=mock.get("cost"))


ADAPTERS: dict[str, Any] = {
    "mock": MockImageAdapter, "openai_images": OpenAIImagesAdapter, "chat_completions": ChatCompletionsAdapter,
    "gemini_native": GeminiNativeAdapter, "zhipu_native": ZhipuNativeAdapter, "http_custom": CustomHttpAdapter,
}


def register_adapter(adapter_type: str, cls: Any) -> None:
    ADAPTERS[adapter_type] = cls


def adapter_for(entry: ModelEntry):
    cls = ADAPTERS.get(entry.adapter_type)
    if cls is None:
        raise AdapterError("adapter_not_implemented", f"no adapter {entry.adapter_type!r}", "no")
    return cls(entry)


def implemented(entry: ModelEntry) -> bool:
    """False for unknown adapters and for native protocols that are still placeholders."""
    cls = ADAPTERS.get(entry.adapter_type)
    if cls is None:
        return False
    if issubclass(cls, NativeMultimodalAdapter):
        return cls.run is not NativeMultimodalAdapter.run
    return True


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
