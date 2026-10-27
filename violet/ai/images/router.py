"""Budgets, health and routing for image tasks (phase 6).

ImageBudget (per entry and global, per local day, from image_calls):
  * images per day, estimated cost per day (entry: limits / pricing; global:
    budgets.global in the config);
  * unknown price is never "free": an entry without pricing.confirmed=true
    (and not pricing.free=true) may only be called pricing.unpriced_max_calls_per_day
    times (default 0, i.e. not at all);
  * max concurrent calls per entry (non-blocking: busy -> try another / fail).
ImageHealth: consecutive failures open a per-entry circuit for a while.

ImageRouter.plan(task):
  explicit model  -> only that entry (no switching unless task.allow_fallback);
                     ambiguous / unknown names are errors, not guesses
  automatic       -> enabled, implemented entries whose REQUIRED capabilities
                     are all "verified" (declared ones only for origin
                     "verification"), whose limits fit the request, with
                     budget, closed circuit; ordered by priority, then cost
  fallback        -> after a failure that was certainly not processed
                     upstream, only the failed entry's configured
                     fallback_candidates that are themselves eligible
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ai.images.adapters import implemented
from ai.images.registry import ImageProviderRegistry, ModelEntry
from ai.images.tasks import ImageTask
from ai.settings import env_bool

_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}
_SEM_LOCK = threading.Lock()
_HEALTH: dict[str, dict[str, Any]] = {}


def reset_runtime_state() -> None:
    """Tests: forget semaphores and circuit states."""
    with _SEM_LOCK:
        _SEMAPHORES.clear()
        _HEALTH.clear()


class ImageHealth:
    @staticmethod
    def is_open(entry: ModelEntry) -> bool:
        h = _HEALTH.get(entry.entry_id)
        return bool(h and h.get("open_until", 0) > time.monotonic())

    @staticmethod
    def record(entry: ModelEntry, ok: bool, code: str | None = None) -> None:
        h = _HEALTH.setdefault(entry.entry_id, {"failures": 0, "open_until": 0.0, "last_error": None})
        if ok:
            h.update(failures=0, open_until=0.0)
            return
        h["failures"] += 1
        h["last_error"] = code
        threshold = int(entry.retry_policy.get("circuit_failures", 3))
        if h["failures"] >= threshold:
            h["open_until"] = time.monotonic() + float(entry.retry_policy.get("circuit_seconds", 300))

    @staticmethod
    def snapshot() -> dict[str, Any]:
        now = time.monotonic()
        return {k: {"failures": v["failures"], "open": v["open_until"] > now, "last_error": v["last_error"]}
                for k, v in _HEALTH.items()}


def estimate_cost(entry: ModelEntry, task: ImageTask) -> float | None:
    """None = price unknown."""
    p = entry.pricing
    if p.get("free"):
        return 0.0
    if not p.get("confirmed"):
        return None
    if task.kind == "vision_analyze":
        return float(p.get("per_call", 0.0))
    per_image = p.get("per_image")
    if isinstance(per_image, dict):
        per_image = per_image.get(task.size or "default", per_image.get("default"))
    return float(per_image or 0.0) * max(1, task.n) + float(p.get("per_call", 0.0))


class ImageBudget:
    def __init__(self, db, registry: ImageProviderRegistry):
        self.db = db
        self.registry = registry

    def _rows_today(self) -> list[dict[str, Any]]:
        return self.db.fetch_all("SELECT entry_id, status, images, est_cost, actual_cost FROM image_calls "
                                 "WHERE created_at >= ?", (self.db.now()[:10],))

    def usage(self, entry_id: str | None = None) -> dict[str, Any]:
        rows = [r for r in self._rows_today() if entry_id is None or r["entry_id"] == entry_id]
        cost = sum(float(r["actual_cost"] if r["actual_cost"] is not None else (r["est_cost"] or 0)) for r in rows)
        return {"calls": len(rows), "images": sum(int(r["images"] or 0) for r in rows), "cost": round(cost, 6),
                "failures": len([r for r in rows if r["status"] != "ok"])}

    def check(self, entry: ModelEntry, task: ImageTask) -> str | None:
        """None if allowed, else the reason."""
        cost = estimate_cost(entry, task)
        mine = self.usage(entry.entry_id)
        if cost is None:
            allowed = int(entry.pricing.get("unpriced_max_calls_per_day", 0))
            if mine["calls"] >= allowed:
                return "price_unconfirmed" if allowed == 0 else "unpriced_daily_limit"
        images = 0 if task.kind == "vision_analyze" else max(1, task.n)
        limits = entry.limits
        if images and "max_images_per_day" in limits and mine["images"] + images > int(limits["max_images_per_day"]):
            return "model_daily_images"
        if "max_calls_per_day" in limits and mine["calls"] >= int(limits["max_calls_per_day"]):
            return "model_daily_calls"
        if cost and "max_cost_per_day" in limits and mine["cost"] + cost > float(limits["max_cost_per_day"]):
            return "model_daily_cost"
        g = self.registry.budgets().get("global") or {}
        total = self.usage()
        if images and "max_images_per_day" in g and total["images"] + images > int(g["max_images_per_day"]):
            return "global_daily_images"
        if cost and "max_cost_per_day" in g and total["cost"] + cost > float(g["max_cost_per_day"]):
            return "global_daily_cost"
        return None

    @staticmethod
    def acquire(entry: ModelEntry) -> threading.BoundedSemaphore | None:
        with _SEM_LOCK:
            sem = _SEMAPHORES.get(entry.entry_id)
            if sem is None:
                sem = _SEMAPHORES[entry.entry_id] = threading.BoundedSemaphore(max(1, int(entry.limits.get("max_concurrency", 1))))
        return sem if sem.acquire(blocking=False) else None


@dataclass
class RoutePlan:
    candidates: list[ModelEntry] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    error_code: str | None = None
    explicit: bool = False


class ImageRouter:
    def __init__(self, registry: ImageProviderRegistry, budget: ImageBudget):
        self.registry = registry
        self.budget = budget

    def eligibility(self, entry: ModelEntry, task: ImageTask) -> str | None:
        if not entry.enabled:
            return "disabled" if not entry.config_errors else "config_error"
        if not implemented(entry):
            return "adapter_not_implemented"
        allow_declared = task.origin == "verification" or (env_bool("VIOLET_IMAGE_ALLOW_UNVERIFIED", False)
                                                           and task.origin == "dashboard")
        for cap in task.required_capabilities():
            state = entry.state(cap)
            if state == "unsupported":
                return f"capability_not_supported:{cap}"
            if state == "declared" and not allow_declared:
                return f"capability_unverified:{cap}"
        limits = entry.limits
        if task.n > int(limits.get("max_outputs", 1)):
            return "too_many_outputs"
        if task.size and limits.get("sizes") and task.size not in limits["sizes"]:
            return "size_not_supported"
        if task.aspect and limits.get("aspect_ratios") and task.aspect not in limits["aspect_ratios"]:
            return "aspect_not_supported"
        if task.quality and limits.get("qualities") and task.quality not in limits["qualities"]:
            return "quality_not_supported"
        if len(task.input_asset_ids) > int(limits.get("max_input_images", 1)):
            return "too_many_inputs"
        if ImageHealth.is_open(entry):
            return "circuit_open"
        reason = self.budget.check(entry, task)
        if reason:
            return f"budget:{reason}"
        return None

    def plan(self, task: ImageTask) -> RoutePlan:
        plan = RoutePlan()
        if task.model:
            entry, error = self.registry.resolve(task.model)
            plan.explicit = True
            if entry is None:
                plan.error_code = error
                return plan
            reason = self.eligibility(entry, task)
            if reason is None:
                plan.candidates.append(entry)
            else:
                plan.rejected[entry.entry_id] = reason
            if task.allow_fallback:
                plan.candidates += self._fallbacks(entry, task, plan)
            if not plan.candidates:
                plan.error_code = self._error_for(plan.rejected)
            return plan
        eligible = []
        for entry in self.registry.entries.values():
            reason = self.eligibility(entry, task)
            if reason is None:
                eligible.append(entry)
            else:
                plan.rejected[entry.entry_id] = reason
        if not eligible:
            plan.error_code = self._error_for(plan.rejected)
            return plan
        eligible.sort(key=lambda e: (e.priority, estimate_cost(e, task) or 0.0, e.entry_id))
        primary = eligible[0]
        plan.candidates = [primary] + self._fallbacks(primary, task, plan)
        return plan

    def _fallbacks(self, entry: ModelEntry, task: ImageTask, plan: RoutePlan) -> list[ModelEntry]:
        out = []
        for fid in entry.fallback_candidates:
            fb = self.registry.get(fid)
            if fb is None or fb is entry:
                continue
            reason = self.eligibility(fb, task)
            if reason is None:
                out.append(fb)
            else:
                plan.rejected.setdefault(fb.entry_id, reason)
        return out

    @staticmethod
    def _error_for(rejected: dict[str, str]) -> str:
        if not rejected:
            return "no_image_model_configured"
        # only models that are switched on say anything about the task itself
        reasons = [r for r in rejected.values() if r not in ("disabled", "config_error", "adapter_not_implemented")]
        if not reasons:
            return "no_image_model_enabled"
        if all(r.startswith("capability_not_supported") for r in reasons):
            return "capability_not_supported"
        if any(r.startswith("capability_unverified") for r in reasons) and \
                not any(r.startswith("budget") or r == "circuit_open" for r in reasons):
            return "capability_unverified"
        if any(r.startswith("budget") for r in reasons):
            return "budget_exceeded"
        if any(r == "circuit_open" for r in reasons):
            return "provider_unavailable"
        return "no_eligible_model"


SIZE_RE = re.compile(r"^\d{2,5}x\d{2,5}$")
