"""Standard image tasks and results (phase 6). Business code only uses these."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

KINDS = ("vision_analyze", "image_generate", "image_edit", "image_variation")


@dataclass
class ImageTask:
    kind: str
    prompt: str = ""
    input_asset_ids: list[int] = field(default_factory=list)       # vision / edit / variation inputs
    reference_asset_ids: list[int] = field(default_factory=list)   # visual identity references (optional use)
    size: str | None = None              # "1024x1024"
    aspect: str | None = None            # "1:1", "3:4", ...
    n: int = 1
    quality: str | None = None
    transparent: bool = False
    model: str | None = None             # explicit "provider/model" (or unique model name)
    allow_fallback: bool | None = None   # explicit model: False unless the user allowed it
    origin: str = "chat"                 # chat | life | dashboard | verification
    idempotency_key: str | None = None
    turn_key: str | None = None
    scene: dict[str, Any] | None = None  # life scene used for an Violet picture
    life_event_id: int | None = None
    caption: str | None = None
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def required_capabilities(self) -> list[str]:
        from ai.images.registry import TASK_CAPABILITY
        caps = [TASK_CAPABILITY[self.kind]]
        if self.kind == "vision_analyze" and len(self.input_asset_ids) > 1:
            caps.append("multiple_input_images")
        if self.kind == "image_edit" and len(self.input_asset_ids) > 1:
            caps.append("multiple_input_images")
        if self.n > 1:
            caps.append("multiple_output_images")
        if self.transparent:
            caps.append("transparent_background")
        return caps


@dataclass
class ImageResult:
    ok: bool
    task_id: str
    status: str                                   # succeeded | failed | uncertain | rejected
    error_code: str | None = None
    message: str | None = None
    asset_ids: list[int] = field(default_factory=list)
    text: str | None = None
    entry_id: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    reused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "task_id": self.task_id, "status": self.status, "error_code": self.error_code,
                "message": self.message, "asset_ids": self.asset_ids, "text": self.text, "entry_id": self.entry_id,
                "attempts": self.attempts, "reused": self.reused}
