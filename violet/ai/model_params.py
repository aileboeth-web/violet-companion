"""Per-model request parameter policy (phase 7, R-M1).

Different models / relays accept different parameters (e.g. some reasoning models
reject a non-default temperature or want max_completion_tokens instead of
max_tokens). Nothing is assumed here: config/model_params.json ships with
defaults that reproduce the current behaviour and NO model entries. An entry is
added only after a real call has shown what a model accepts.

Policy fields (defaults, overridable per model name):
  send_temperature      true  -> pass temperature; false -> leave it out
  max_tokens_field      "max_tokens" | "max_completion_tokens" | "none"
  allowed_extra_params  extra fields that may be sent (e.g. "reasoning_effort")
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ai.settings import PROJECT_ROOT, env_str

logger = logging.getLogger("violet.model_params")
DEFAULTS = {"send_temperature": True, "max_tokens_field": "max_tokens", "allowed_extra_params": ["reasoning_effort"]}
EXTRA_PARAMS = ("reasoning_effort",)
_CACHE: dict[str, Any] = {"path": None, "mtime": None, "data": None}


def policy_path() -> Path:
    override = env_str("VIOLET_MODEL_PARAMS_FILE", "")
    return Path(override) if override else PROJECT_ROOT / "config" / "model_params.json"


def _load() -> dict[str, Any]:
    path = policy_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"defaults": DEFAULTS, "models": {}}
    if _CACHE["path"] == str(path) and _CACHE["mtime"] == mtime:
        return _CACHE["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("model parameter policy %s unreadable (%s): using defaults", path, exc)
        data = {}
    data = {"defaults": {**DEFAULTS, **(data.get("defaults") or {})}, "models": data.get("models") or {}}
    _CACHE.update(path=str(path), mtime=mtime, data=data)
    return data


def policy_for(model: str | None) -> dict[str, Any]:
    data = _load()
    return {**data["defaults"], **{k: v for k, v in (data["models"].get(model or "") or {}).items() if not k.startswith("_")}}


def apply(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of chat.completions.create kwargs adjusted to the model's policy."""
    out = dict(kwargs)
    policy = policy_for(out.get("model"))
    if not policy.get("send_temperature", True):
        out.pop("temperature", None)
    if "max_tokens" in out:
        value = out.pop("max_tokens")
        field = policy.get("max_tokens_field", "max_tokens")
        if field in ("max_tokens", "max_completion_tokens"):
            out[field] = value
    allowed = set(policy.get("allowed_extra_params") or [])
    for name in EXTRA_PARAMS:
        if name in out and name not in allowed:
            out.pop(name)
    return out
