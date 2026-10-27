"""Image models, their declared capabilities, and the provider registry (phase 6).

Every model is one entry, identified by entry_id = "<provider_id>/<model_id>"
(so the same model name at two providers are two entries). Capabilities are
never guessed from a name: each must be listed, with a state:

    "verified"     confirmed by a real call (recorded in the config with a note,
                   or by a passed check in image_capability_checks for the
                   current config of that entry)
    "declared"     claimed by documentation / the operator, NOT yet verified:
                   never used for normal business calls
    "unsupported"  (or absent) not available

Keys are never in the file: api_key_env names the environment variable that
holds it, read at call time. An entry carrying a literal "api_key" is refused.
Dashboard changes (enable / disable / priority) are stored in
image_model_settings and override the file without editing it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai.settings import PROJECT_ROOT, env_str

logger = logging.getLogger("violet.images")

CAPABILITIES = ("vision", "image_generation", "image_edit", "image_variation", "reference_images",
                "multiple_input_images", "multiple_output_images", "transparent_background",
                "return_base64", "return_url")
STATES = ("verified", "declared", "unsupported")
TASK_CAPABILITY = {"vision_analyze": "vision", "image_generate": "image_generation", "image_edit": "image_edit",
                   "image_variation": "image_variation"}
KNOWN_ADAPTERS = ("mock", "openai_images", "chat_completions", "gemini_native", "zhipu_native", "http_custom")


def providers_path() -> Path:
    override = env_str("VIOLET_IMAGE_PROVIDERS_FILE", "")
    return Path(override) if override else PROJECT_ROOT / "config" / "image_providers.json"


@dataclass
class ModelEntry:
    provider_id: str
    model_id: str
    adapter_type: str
    base_url: str = ""
    api_key_env: str = ""
    enabled: bool = False
    capabilities: dict[str, str] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    timeout: float = 60.0
    retry_policy: dict[str, Any] = field(default_factory=dict)
    pricing: dict[str, Any] = field(default_factory=dict)
    priority: int = 100
    fallback_candidates: list[str] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    status_note: str = ""
    config_errors: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def entry_id(self) -> str:
        return f"{self.provider_id}/{self.model_id}"

    @property
    def config_hash(self) -> str:
        raw = {k: v for k, v in self.raw.items() if k not in ("enabled", "priority", "status_note")}
        return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]

    def state(self, capability: str) -> str:
        value = self.capabilities.get(capability, "unsupported")
        return value if value in STATES else "unsupported"

    def public(self) -> dict[str, Any]:
        """Dashboard view: never the key itself."""
        import os
        return {
            "entry_id": self.entry_id, "provider_id": self.provider_id, "model_id": self.model_id,
            "adapter_type": self.adapter_type, "base_url": self.base_url, "api_key_env": self.api_key_env,
            "api_key_present": bool(self.api_key_env and os.environ.get(self.api_key_env)),
            "enabled": self.enabled, "capabilities": dict(self.capabilities), "limits": self.limits,
            "timeout": self.timeout, "retry_policy": self.retry_policy, "pricing": self.pricing,
            "priority": self.priority, "fallback_candidates": self.fallback_candidates,
            "status_note": self.status_note, "config_errors": self.config_errors,
        }


class ImageProviderRegistry:
    def __init__(self, db=None, path: Path | None = None, data: dict[str, Any] | None = None):
        self.db = db
        self.path = path or providers_path()
        self.data = data if data is not None else self._read()
        self.entries: dict[str, ModelEntry] = {}
        self._load()

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error("image provider config %s unreadable (%s): no image models available", self.path, exc)
            return {}

    def _load(self) -> None:
        for provider in self.data.get("providers") or []:
            if not isinstance(provider, dict):
                continue
            for model in provider.get("models") or []:
                if not isinstance(model, dict):
                    continue
                raw = {**{k: v for k, v in provider.items() if k != "models"}, **model}
                entry = ModelEntry(
                    provider_id=str(provider.get("provider_id") or ""), model_id=str(model.get("model_id") or ""),
                    adapter_type=str(raw.get("adapter_type") or ""), base_url=str(raw.get("base_url") or ""),
                    api_key_env=str(raw.get("api_key_env") or ""),
                    enabled=bool(provider.get("enabled", True)) and bool(model.get("enabled", False)),
                    capabilities={k: str(v) for k, v in (model.get("capabilities") or {}).items()},
                    limits=dict(model.get("limits") or {}), timeout=float(raw.get("timeout") or 60),
                    retry_policy=dict(raw.get("retry_policy") or {}), pricing=dict(model.get("pricing") or {}),
                    priority=int(model.get("priority", 100)), fallback_candidates=list(model.get("fallback_candidates") or []),
                    options=dict(raw.get("options") or {}), status_note=str(model.get("status_note") or ""), raw=raw)
                errors = []
                if not entry.provider_id or not entry.model_id:
                    errors.append("provider_id and model_id are required")
                if entry.adapter_type not in KNOWN_ADAPTERS:
                    errors.append(f"unknown adapter_type {entry.adapter_type!r}")
                if any(k.lower() in ("api_key", "key", "token", "secret") for k in raw):
                    errors.append("a literal key in the config file is not allowed; use api_key_env")
                for cap in entry.capabilities:
                    if cap not in CAPABILITIES:
                        errors.append(f"unknown capability {cap!r}")
                if errors:
                    entry.enabled = False
                    entry.config_errors = errors
                if entry.entry_id in self.entries:
                    entry.enabled = False
                    entry.config_errors.append("duplicate entry id")
                    logger.error("duplicate image model entry %s ignored", entry.entry_id)
                    continue
                self.entries[entry.entry_id] = entry
        self._apply_overrides()

    def _apply_overrides(self) -> None:
        if self.db is None:
            return
        for row in self.db.fetch_all("SELECT * FROM image_model_settings"):
            entry = self.entries.get(row["entry_id"])
            if entry is None:
                continue
            if row["enabled"] is not None and not entry.config_errors:
                entry.enabled = bool(row["enabled"])
            if row["priority"] is not None:
                entry.priority = int(row["priority"])
        for row in self.db.fetch_all("SELECT entry_id, capability, config_hash FROM image_capability_checks "
                                     "WHERE passed = 1 ORDER BY id"):
            entry = self.entries.get(row["entry_id"])
            # a check only counts for the exact configuration it was made with
            if entry and row["config_hash"] == entry.config_hash and entry.state(row["capability"]) == "declared":
                entry.capabilities[row["capability"]] = "verified"

    # -- queries --------------------------------------------------------------------------
    def get(self, entry_id: str) -> ModelEntry | None:
        return self.entries.get(entry_id)

    def resolve(self, requested: str) -> tuple[ModelEntry | None, str | None]:
        """"provider/model" or a model name that exists at exactly one provider."""
        if requested in self.entries:
            return self.entries[requested], None
        matches = [e for e in self.entries.values() if e.model_id == requested]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, "ambiguous_model"
        return None, "unknown_model"

    def budgets(self) -> dict[str, Any]:
        return dict(self.data.get("budgets") or {})

    # -- dashboard ------------------------------------------------------------------------
    def set_override(self, entry_id: str, *, enabled: bool | None = None, priority: int | None = None) -> None:
        if self.db is None or entry_id not in self.entries:
            raise KeyError(entry_id)
        current = self.db.fetch_one("SELECT * FROM image_model_settings WHERE entry_id = ?", (entry_id,))
        new_enabled = (1 if enabled else 0) if enabled is not None else (current or {}).get("enabled")
        new_priority = priority if priority is not None else (current or {}).get("priority")
        self.db.execute(
            "INSERT INTO image_model_settings (entry_id, enabled, priority, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(entry_id) DO UPDATE SET enabled = excluded.enabled, priority = excluded.priority, "
            "updated_at = excluded.updated_at", (entry_id, new_enabled, new_priority, self.db.now()))
        if enabled is not None and not self.entries[entry_id].config_errors:
            self.entries[entry_id].enabled = bool(enabled)
        if priority is not None:
            self.entries[entry_id].priority = int(priority)

    def record_check(self, entry_id: str, capability: str, passed: bool, detail: str, actor: str) -> None:
        entry = self.entries[entry_id]
        self.db.execute(
            "INSERT INTO image_capability_checks (entry_id, capability, passed, detail, config_hash, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entry_id, capability, 1 if passed else 0, detail[:500], entry.config_hash, actor, self.db.now()))
        if passed and entry.state(capability) == "declared":
            entry.capabilities[capability] = "verified"
