"""Offline image providers for phase 6 tests (fictional providers, no real API)."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any

from ai.images.adapters import make_png

PNG = make_png(32, 24)
PNG_B64 = base64.b64encode(PNG).decode("ascii")

MOCK_CAPS = {"vision": "verified", "image_generation": "verified", "image_edit": "verified",
             "multiple_output_images": "verified", "multiple_input_images": "verified", "return_base64": "verified"}


def config() -> dict[str, Any]:
    return copy.deepcopy({
        "budgets": {"global": {"max_images_per_day": 50, "max_cost_per_day": 5.0}},
        "providers": [
            {"provider_id": "mockA", "adapter_type": "mock", "enabled": True,
             "retry_policy": {"max_attempts": 2, "backoff_seconds": 0, "circuit_failures": 3, "circuit_seconds": 60},
             "models": [{"model_id": "img-1", "enabled": True, "capabilities": dict(MOCK_CAPS),
                         "limits": {"max_outputs": 4, "max_input_images": 4, "max_concurrency": 2},
                         "pricing": {"free": True}, "priority": 10, "fallback_candidates": ["mockB/img-1"]}]},
            {"provider_id": "mockB", "adapter_type": "mock", "enabled": True,
             "models": [{"model_id": "img-1", "enabled": True, "capabilities": dict(MOCK_CAPS),
                         "limits": {"max_outputs": 2, "max_input_images": 1, "max_concurrency": 1},
                         "pricing": {"free": True}, "priority": 20}]},
            {"provider_id": "openai", "adapter_type": "openai_images", "base_url": "https://images.example.org/v1",
             "api_key_env": "TEST_IMAGE_KEY", "enabled": True, "timeout": 5,
             "models": [{"model_id": "gpt-image-x", "enabled": True,
                         "capabilities": {"image_generation": "verified", "image_edit": "verified",
                                          "image_variation": "verified", "multiple_output_images": "verified",
                                          "multiple_input_images": "verified", "transparent_background": "verified",
                                          "return_base64": "verified", "return_url": "verified"},
                         "limits": {"max_outputs": 4, "max_input_images": 4, "sizes": ["1024x1024", "512x512"],
                                    "max_concurrency": 1},
                         "pricing": {"confirmed": True, "per_image": 0.04}, "priority": 50}]},
            {"provider_id": "chatimg", "adapter_type": "chat_completions", "base_url": "https://chat.example.org/v1",
             "api_key_env": "TEST_IMAGE_KEY", "enabled": True, "timeout": 5,
             "models": [{"model_id": "chat-img", "enabled": True,
                         "capabilities": {"image_generation": "verified", "vision": "verified",
                                          "reference_images": "verified"},
                         "limits": {"max_outputs": 1, "max_input_images": 1},
                         "pricing": {"confirmed": True, "per_image": 0.02, "per_call": 0.001}, "priority": 60}]},
            {"provider_id": "newco", "adapter_type": "openai_images", "base_url": "https://new.example.org/v1",
             "enabled": True,
             "models": [{"model_id": "fresh-1", "enabled": True,
                         "capabilities": {"image_generation": "declared", "image_variation": "declared"},
                         "pricing": {"confirmed": False}, "priority": 1}]},
            {"provider_id": "gemini", "adapter_type": "gemini_native", "base_url": "https://g.example.org",
             "enabled": True,
             "models": [{"model_id": "g-img", "enabled": True, "capabilities": {"image_generation": "verified"},
                         "pricing": {"free": True}, "priority": 0}]},
        ],
    })


def only(cfg: dict[str, Any], *entry_ids: str) -> dict[str, Any]:
    """Keep the given entries enabled, switch the others off."""
    for p in cfg["providers"]:
        for m in p["models"]:
            m["enabled"] = f"{p['provider_id']}/{m['model_id']}" in entry_ids
    return cfg


def model(cfg: dict[str, Any], entry_id: str) -> dict[str, Any]:
    provider_id, model_id = entry_id.split("/", 1)
    for p in cfg["providers"]:
        if p["provider_id"] == provider_id:
            for m in p["models"]:
                if m["model_id"] == model_id:
                    return m
    raise KeyError(entry_id)


def write(cfg: dict[str, Any], path: Path) -> Path:
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path
