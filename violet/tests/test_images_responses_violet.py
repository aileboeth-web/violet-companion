"""Violet 0.7.6: drawing through the Responses API's image_generation tool (gpt-5.x / Codex relays), the service's
own words in a failed drawing test, the chat SDK's User-Agent, and a channel shown as connected right after it logged
in. httpx.MockTransport / fake adapters; no network."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.image_fixtures import PNG_B64


@pytest.fixture()
def entry(monkeypatch):
    from ai.images import adapters
    from ai.images.registry import ModelEntry
    monkeypatch.setenv("TEST_IMAGE_KEY", "sk-image-test-key-123456")
    yield ModelEntry(provider_id="panel", model_id="cys", adapter_type="responses",
                     base_url="https://relay.example/v1", api_key_env="TEST_IMAGE_KEY")
    adapters.set_transport_factory(lambda entry: None)


def _serve(handler):
    from ai.images import adapters
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)
    adapters.set_transport_factory(lambda entry: httpx.MockTransport(wrapped))
    return seen


def _request(entry, prompt="一个蓝色圆形"):
    from ai.images.adapters import AdapterRequest
    return AdapterRequest(kind="image_generate", entry=entry, prompt=prompt)


def test_responses_draws_with_the_image_tool(entry):
    from ai.images.adapters import adapter_for
    seen = _serve(lambda r: httpx.Response(200, json={"output": [
        {"type": "image_generation_call", "status": "completed", "result": PNG_B64, "output_format": "png"},
        {"type": "message", "content": [{"type": "output_text", "text": "画好了"}]}]}))
    out = adapter_for(entry).run(_request(entry))
    assert out.images[0].kind == "b64" and out.images[0].value == PNG_B64 and out.text == "画好了"
    req = seen[0]
    body = json.loads(req.content)
    assert req.url.path == "/v1/responses" and body["model"] == "cys" and body["tools"] == [{"type": "image_generation"}]
    assert "一个蓝色圆形" in body["input"][0]["content"][0]["text"] and body["stream"] is False
    assert req.headers["user-agent"].startswith("OpenAI/Python") and req.headers["authorization"].startswith("Bearer ")


def test_responses_retries_streamed_when_the_service_only_streams(entry):
    from ai.images.adapters import adapter_for

    def handler(request):
        if not json.loads(request.content)["stream"]:
            return httpx.Response(400, json={"error": {"message": "Stream must be set to true"}})
        events = [{"type": "response.created", "response": {"id": "r1"}},
                  {"type": "response.output_item.done", "item": {"type": "image_generation_call", "result": PNG_B64}},
                  {"type": "response.completed", "response": {"id": "r1", "output": [], "usage": {"total_tokens": 9}}}]
        text = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
    seen = _serve(handler)
    out = adapter_for(entry).run(_request(entry))
    assert len(seen) == 2 and out.images[0].value == PNG_B64 and out.usage == {"total_tokens": 9}


def test_responses_without_an_image_says_so(entry):
    from ai.images.adapters import AdapterError, adapter_for
    _serve(lambda r: httpx.Response(200, json={"output": [
        {"type": "message", "content": [{"type": "output_text", "text": "我不能画图"}]}]}))
    with pytest.raises(AdapterError) as err:
        adapter_for(entry).run(_request(entry))
    assert "没有调用画图工具" in str(err.value) and "我不能画图" in str(err.value)


def test_a_403_page_keeps_its_words(entry):
    from ai.images.adapters import AdapterError, adapter_for
    entry.adapter_type = "openai_images"
    _serve(lambda r: httpx.Response(403, text="<html><h1>403 Forbidden</h1><p>model cys not allowed</p></html>"))
    with pytest.raises(AdapterError) as err:
        adapter_for(entry).run(_request(entry))
    assert err.value.code == "http_403" and "model cys not allowed" in str(err.value)


def test_the_page_offers_the_responses_protocol(monkeypatch):
    from ai.control_config import validate
    from ai.images.registry import ImageProviderRegistry
    assert validate("VIOLET_IMAGE_GEN_PROTOCOL", "responses") == "responses"
    monkeypatch.setenv("VIOLET_IMAGE_GEN_MODEL", "cys")
    monkeypatch.setenv("VIOLET_IMAGE_GEN_PROTOCOL", "responses")
    monkeypatch.setenv("LLM_API_URL", "https://relay.example/v1")
    assert ImageProviderRegistry().get("panel/cys").adapter_type == "responses"


def test_failed_drawing_test_records_the_reason(violetdb, monkeypatch):
    from ai.images.registry import ImageProviderRegistry
    from ai.images.service import ImageService
    from ai.images.tasks import ImageResult
    monkeypatch.setenv("VIOLET_IMAGE_GEN_MODEL", "cys")
    monkeypatch.setenv("LLM_API_URL", "https://relay.example/v1")
    registry = ImageProviderRegistry(violetdb)
    svc = ImageService(violetdb, registry=registry)
    monkeypatch.setattr(svc, "submit", lambda task: ImageResult(ok=False, task_id="t1", status="failed",
                                                               error_code="http_403", message="model cys not allowed"))
    svc.verify("panel/cys", "image_generation")
    row = violetdb.fetch_one("SELECT detail FROM image_capability_checks ORDER BY id DESC LIMIT 1")
    assert row["detail"].startswith("http_403：model cys not allowed")


def test_a_channel_is_connected_as_soon_as_it_logged_in():
    import threading
    from ai.channels.base import ChannelAdapter
    release = threading.Event()

    class Slow(ChannelAdapter):
        channel_type = "slow"
        label = "Slow"

        def receive_once(self):
            release.wait(5)                  # like QQ waiting for the next heartbeat
            return 0

        def send_text(self, peer_id, text, client_id=None):
            return {}
    ch = Slow("slow")
    ch.start(lambda m: True)
    try:
        for _ in range(100):
            if ch.status().state == "connected":
                break
            threading.Event().wait(0.02)
        assert ch.status().state == "connected"
    finally:
        release.set()
        ch.stop()
