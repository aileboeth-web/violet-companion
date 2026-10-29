"""Dashboard API for the image system (phase 6). Same admin authentication as every route.

Never returns a key: only api_key_env (the variable name) and whether it is set.
Changing models (enable / disable / priority) and running capability checks
(which may cost money and therefore need {"confirm_paid": true} for non-free
models) are POSTs, so they also pass the dashboard's same-origin checks.
"""

from __future__ import annotations

from flask import Flask, jsonify, redirect, request, send_file

from ai.images.media import MediaAssetStore, MediaError
from ai.images.registry import CAPABILITIES, ImageProviderRegistry
from ai.images.router import ImageBudget, ImageHealth
from ai.images.service import ImageService, images_enabled
from ai.images.visual import VisualIdentityManager


def register_image_routes(app: Flask, violetdb) -> None:
    def ok(payload, status=200):
        return jsonify(payload), status

    def bad(message, status=400):
        return jsonify({"error": message}), status

    def body() -> dict:
        return request.get_json(silent=True) or {}

    @app.get("/images")
    def images_page():                                # 0.7.1: the old page is gone
        return redirect("/control#settings")

    # ---------------------------------------------------------------- 0.7.2: the Control Center's "图片" page
    def _panel_state() -> dict:
        from ai.images.registry import panel_entry_id
        from ai.settings import env_bool, env_str
        from ai.vision import vision_model
        registry = ImageProviderRegistry(violetdb)
        entry_id = panel_entry_id()
        entry = registry.get(entry_id) if entry_id else None
        last = None
        if entry is not None:
            last = violetdb.fetch_one("SELECT passed, detail, config_hash, created_at FROM image_capability_checks "
                                      "WHERE entry_id = ? AND capability = 'image_generation' ORDER BY id DESC LIMIT 1",
                                      (entry_id,))
            if last:
                last = {"passed": bool(last["passed"]), "detail": last["detail"], "at": last["created_at"],
                        "current": last["config_hash"] == entry.config_hash}
        budget = ImageBudget(violetdb, registry)
        return {"enabled": images_enabled(),
                "gen": None if entry is None else {
                    "entry_id": entry_id, "model": entry.model_id, "protocol": entry.adapter_type,
                    "base_url": entry.base_url, "own_url": bool(env_str("VIOLET_IMAGE_API_URL", "")),
                    "own_key": bool(env_str("VIOLET_IMAGE_API_KEY", "")),
                    "key_present": entry.public()["api_key_present"],
                    "verified": entry.state("image_generation") == "verified",
                    "daily_limit": entry.limits.get("max_images_per_day"), "usage_today": budget.usage(entry_id),
                    "last_check": last},
                "vision": {"model": env_str("VISION_LLM_MODEL", ""), "effective": vision_model(),
                           "legacy": env_bool("VIOLET_IMAGE_LEGACY_VISION", True)}}

    @app.get("/api/images/panel")
    def images_panel():
        assets = MediaAssetStore(violetdb).list(limit=24)
        for a in assets:
            a.pop("storage_path", None)
        return ok({**_panel_state(), "assets": [a for a in assets if a.get("status") == "ready"]})

    @app.post("/api/images/panel/test-generate")
    def images_panel_test_generate():
        """One real drawing with the page's model (may cost money: {"confirm_paid": true}); a pass verifies it."""
        from ai.images.registry import panel_entry_id
        if body().get("confirm_paid") is not True:
            return bad("测试会真实画一张图，可能产生费用：需要确认（confirm_paid: true）", 409)
        entry_id = panel_entry_id()
        if not entry_id:
            return bad("先选一个画图模型并保存")
        registry = ImageProviderRegistry(violetdb)
        try:
            result = ImageService(violetdb, registry=registry).verify(entry_id, "image_generation", actor="dashboard")
        except (KeyError, ValueError) as exc:
            return bad(str(exc))
        return ok({**result, "state": _panel_state()})

    @app.post("/api/images/panel/test-vision")
    def images_panel_test_vision():
        """One real call of the recognition model with a small red square ("图里是什么颜色？")."""
        import tempfile
        import time as _time
        from pathlib import Path as _Path
        from ai.images.adapters import make_png
        from ai.vision import VisionError, describe_image, vision_model
        started = _time.monotonic()
        with tempfile.TemporaryDirectory(prefix="violet-vision-") as folder:
            path = _Path(folder) / "probe.png"
            path.write_bytes(make_png(64, 64, (220, 40, 40)))
            try:
                text = describe_image(path)
                return ok({"ok": True, "text": text[:300], "model": vision_model(), "latency_ms": int((_time.monotonic() - started) * 1000)})
            except VisionError as exc:
                return ok({"ok": False, "error": str(exc)[:300],
                           "latency_ms": int((_time.monotonic() - started) * 1000)})

    @app.get("/api/images/providers")
    def images_providers():
        registry = ImageProviderRegistry(violetdb)
        budget = ImageBudget(violetdb, registry)
        health = ImageHealth.snapshot()
        models = []
        for entry in registry.entries.values():
            item = entry.public()
            item["usage_today"] = budget.usage(entry.entry_id)
            item["health"] = health.get(entry.entry_id)
            models.append(item)
        return ok({"enabled": images_enabled(), "capabilities": list(CAPABILITIES), "models": models,
                   "budgets": registry.budgets(), "usage_today": budget.usage(),
                   "config_file": str(registry.path)})

    @app.post("/api/images/models/<provider_id>/<model_id>/<action>")
    def images_model_action(provider_id: str, model_id: str, action: str):
        registry = ImageProviderRegistry(violetdb)
        entry_id = f"{provider_id}/{model_id}"
        if registry.get(entry_id) is None:
            return bad("unknown model", 404)
        if action in ("enable", "disable"):
            if action == "enable" and registry.get(entry_id).config_errors:
                return bad("model has configuration errors: " + "; ".join(registry.get(entry_id).config_errors))
            registry.set_override(entry_id, enabled=action == "enable")
        elif action == "priority":
            try:
                registry.set_override(entry_id, priority=int(body().get("priority")))
            except (TypeError, ValueError):
                return bad("priority must be an integer")
        else:
            return bad("unknown action", 404)
        return ok(registry.get(entry_id).public())

    @app.post("/api/images/models/<provider_id>/<model_id>/verify/<capability>")
    def images_verify(provider_id: str, model_id: str, capability: str):
        registry = ImageProviderRegistry(violetdb)
        entry = registry.get(f"{provider_id}/{model_id}")
        if entry is None:
            return bad("unknown model", 404)
        if capability not in CAPABILITIES:
            return bad("unknown capability")
        if not entry.pricing.get("free") and body().get("confirm_paid") is not True:
            return bad("this check calls the real API and may cost money: send {\"confirm_paid\": true}", 409)
        try:
            return ok(ImageService(violetdb, registry=registry).verify(entry.entry_id, capability, actor="dashboard"))
        except ValueError as exc:
            return bad(str(exc))

    @app.get("/api/images/tasks")
    def images_tasks():
        rows = violetdb.fetch_all("SELECT task_id, kind, origin, status, requested_model, entry_id, error_code, "
                               "output_asset_ids, created_at FROM image_tasks ORDER BY id DESC LIMIT ?",
                               (min(int(request.args.get("limit", 100)), 500),))
        return ok({"tasks": rows})

    @app.get("/api/images/usage")
    def images_usage():
        rows = violetdb.fetch_all(
            "SELECT entry_id, substr(created_at, 1, 10) AS day, COUNT(*) AS calls, "
            "SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) AS failed, SUM(images) AS images, "
            "SUM(COALESCE(est_cost, 0)) AS est_cost, SUM(actual_cost) AS actual_cost, AVG(latency_ms) AS avg_latency_ms "
            "FROM image_calls GROUP BY entry_id, day ORDER BY day DESC LIMIT 200")
        return ok({"usage": rows, "media": MediaAssetStore(violetdb).usage()})

    @app.get("/api/images/assets")
    def images_assets():
        assets = MediaAssetStore(violetdb).list(limit=min(int(request.args.get("limit", 100)), 500),
                                             direction=request.args.get("direction"))
        for a in assets:
            a.pop("storage_path", None)
        return ok({"assets": assets})

    @app.get("/api/images/assets/<int:asset_id>/file")
    def images_asset_file(asset_id: int):
        store = MediaAssetStore(violetdb)
        asset = store.get(asset_id)
        if asset is None or asset["status"] != "ready":
            return bad("not found", 404)
        response = send_file(store.path_of(asset), mimetype=asset["mime_type"], download_name=f"{asset_id}.{asset['mime_type'].split('/')[-1]}")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'"
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.delete("/api/images/assets/<int:asset_id>")
    def images_asset_delete(asset_id: int):
        try:
            if not MediaAssetStore(violetdb).delete(asset_id, "dashboard"):
                return bad("not found", 404)
        except MediaError as exc:
            return bad(str(exc), 409)
        return ok({"deleted": asset_id})

    @app.get("/api/images/visual-profile")
    def images_visual_profile():
        manager = VisualIdentityManager(violetdb)
        history = violetdb.fetch_all("SELECT id, version, sha256, actor, note, created_at FROM visual_profile_versions "
                                  "ORDER BY id DESC LIMIT 50")
        return ok({"profile": manager.load(), "sha256": manager.sha(), "versions": history})

    @app.put("/api/images/visual-profile")
    def images_visual_profile_update():
        data = body()
        if not isinstance(data.get("patch"), dict) or not data["patch"]:
            return bad("patch (object) required")
        try:
            return ok(VisualIdentityManager(violetdb).update(data["patch"], actor="dashboard", note=str(data.get("note") or "")))
        except ValueError as exc:
            return bad(str(exc))
