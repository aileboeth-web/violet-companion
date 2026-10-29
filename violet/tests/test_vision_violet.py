"""Violet 0.7.6: the recognition model ("看图") goes through the OpenAI SDK like the chat agents, falls back to the Chat
model (the old placeholder "vision" counts as not chosen), retries a rejected request once without the extra
parameters, and says why it failed. Fake client; no network."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest

import ai.vision as vision
from ai.images.adapters import make_png


def _error(cls, status, message):
    response = httpx.Response(status, request=httpx.Request("POST", "https://relay.example/v1/chat/completions"))
    return cls(message, response=response, body={"error": {"message": message}})


class FakeClient:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=out))])


@pytest.fixture()
def picture(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-key-123")
    monkeypatch.setenv("LLM_API_URL", "https://relay.example/v1")
    monkeypatch.setenv("CHAT_LLM_MODEL", "gpt-5.4")
    monkeypatch.delenv("VISION_LLM_MODEL", raising=False)
    path = tmp_path / "photo.jpg"                     # a PNG under a .jpg name: the bytes decide the type
    path.write_bytes(make_png(8, 8, (220, 40, 40)))
    return path


def _use(monkeypatch, client):
    monkeypatch.setattr(vision, "_client", lambda url, key: client)
    return client


def test_model_falls_back_to_chat(monkeypatch, picture):
    assert vision.vision_model() == "gpt-5.4"
    monkeypatch.setenv("VISION_LLM_MODEL", "vision")                  # old env_example placeholder
    assert vision.vision_model() == "gpt-5.4"
    monkeypatch.setenv("VISION_LLM_MODEL", "gpt-4o")
    assert vision.vision_model() == "gpt-4o"


def test_describe_sends_the_image_with_its_real_type(monkeypatch, picture):
    client = _use(monkeypatch, FakeClient("一个红色方块"))
    assert vision.describe_image(picture) == "一个红色方块"
    call = client.calls[0]
    assert call["model"] == "gpt-5.4"
    image = call["messages"][0]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/png;base64,")


def test_rejected_request_is_retried_bare(monkeypatch, picture):
    client = _use(monkeypatch, FakeClient(_error(openai.BadRequestError, 400, "Unsupported parameter: 'max_tokens'"),
                                          "红色"))
    assert vision.describe_image(picture) == "红色"
    assert "max_tokens" in client.calls[0] and set(client.calls[1]) == {"model", "messages"}


def test_failure_says_why(monkeypatch, picture):
    _use(monkeypatch, FakeClient(_error(openai.NotFoundError, 404, "model gpt-5.4 not found")))
    with pytest.raises(vision.VisionError) as err:
        vision.describe_image(picture)
    assert "HTTP 404" in str(err.value) and "not found" in str(err.value) and "gpt-5.4" in str(err.value)
    _use(monkeypatch, FakeClient(""))
    with pytest.raises(vision.VisionError, match="不支持看图"):
        vision.describe_image(picture)


def test_missing_key_is_clear(monkeypatch, picture):
    monkeypatch.setenv("LLM_API_KEY", "")
    with pytest.raises(vision.VisionError, match="API Key"):
        vision.describe_image(Path(picture))


def test_setting_is_optional():
    from ai.control_config import BY_KEY, validate
    assert BY_KEY["VISION_LLM_MODEL"].optional and validate("VISION_LLM_MODEL", "") == ""
