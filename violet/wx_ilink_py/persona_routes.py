"""Dashboard API for Violet's persona and virtual life (phase 4).

Behind the same admin authentication as every other route (wx_ilink_py/auth.py).
The persona core is read-only here on purpose: it is changed only by editing
persona/persona_core.json by hand (and bumping its version).
"""

from __future__ import annotations

from flask import Flask, jsonify, redirect, request

from ai.consistency import ConsistencyGuard
from ai.life_store import LifeStore, TransitionError
from ai.life_scheduler import LifeControl
from ai.persona import PersonaCore, PersonaDynamics, persona_dir

CORE_EDIT_NOTE = "核心人设只能人工编辑 persona/persona_core.json 并递增 version；系统和 Dashboard 都不能修改。"


def register_persona_routes(app: Flask, violetdb) -> None:
    store = LifeStore(violetdb)

    def ok(payload, status=200):
        return jsonify(payload), status

    def bad(message, status=400):
        return jsonify({"error": message}), status

    def body() -> dict:
        return request.get_json(silent=True) or {}

    @app.get("/persona")
    def persona_page():
        # Violet stage 3: the page is part of the Control Center now (same look, same navigation)
        return redirect("/control#persona")

    # -- persona -----------------------------------------------------------------------
    @app.get("/api/persona")
    def persona_overview():
        core = PersonaCore.load(violetdb)
        dynamics = PersonaDynamics(violetdb)
        dynamics.active()       # expires temporary interests
        return ok({
            "core": {"version": core.version, "sha256": core.sha256, "data": core.data,
                     "file": str(persona_dir() / "persona_core.json"), "edit_policy": CORE_EDIT_NOTE},
            "core_versions": violetdb.fetch_all(
                "SELECT id, version, sha256, loaded_at FROM persona_core_versions ORDER BY id DESC LIMIT 20"),
            "dynamics": dynamics.list(),
        })

    @app.route("/api/persona/core", methods=["PUT", "PATCH", "POST", "DELETE"])
    def persona_core_write():
        return bad(CORE_EDIT_NOTE, 405)

    # -- 2026-10-29: persona templates (incl. the blank one); the memory is kept or cleared (archived first) -------------
    @app.get("/api/persona/templates")
    def persona_templates():
        from ai import persona_templates as pt
        return ok({"templates": pt.templates(), "current": pt.current()})

    @app.post("/api/persona/switch")
    def persona_switch():
        from ai import persona_templates as pt
        data = body()
        if data.get("confirm") is not True:
            return bad("换人设需要确认（confirm: true）")
        try:
            result = pt.switch(violetdb, str(data.get("template") or ""), str(data.get("memory") or ""),
                               str(data.get("name") or "") or None)
        except ValueError as exc:
            return bad(str(exc))
        return ok(result)

    @app.post("/api/persona/dynamics/<int:entry_id>/decide")
    def persona_decide(entry_id: int):
        approve = body().get("approve")
        if not isinstance(approve, bool):
            return bad("approve must be true or false")
        try:
            return ok(PersonaDynamics(violetdb).decide(entry_id, approve))
        except ValueError as exc:
            return bad(str(exc))

    @app.get("/api/persona/dynamics/<int:entry_id>/history")
    def persona_history(entry_id: int):
        entry = PersonaDynamics(violetdb).get(entry_id)
        if not entry:
            return bad("not found", 404)
        return ok({"entry": entry, "revisions": store.revisions("persona_dynamic", entry_id)})

    # -- life ----------------------------------------------------------------------------
    @app.get("/api/life")
    def life_overview():
        return ok({
            "threads": store.threads(),
            "plans": store.plans(),
            "events": store.events(statuses=None, limit=200),
            "flags": store.flags("open"),
            "runs": violetdb.fetch_all("SELECT * FROM life_engine_runs ORDER BY id DESC LIMIT 30"),
            "engine": LifeControl(violetdb).status(),
            "llm_usage": violetdb.fetch_all(
                "SELECT purpose, status, COUNT(*) AS calls, SUM(total_tokens) AS tokens FROM aux_llm_calls "
                "WHERE created_at >= ? GROUP BY purpose, status", (violetdb.now()[:10],)),
        })

    @app.get("/api/life/engine")
    def life_engine_status():
        return ok(LifeControl(violetdb).status())

    @app.post("/api/life/engine/<action>")
    def life_engine_action(action: str):
        if action not in ("pause", "resume"):
            return bad("unknown action", 404)
        control = LifeControl(violetdb)
        if control.mode() == "off":
            return bad("life engine is off (VIOLET_LIFE_ENGINE=0 or life_config.enabled=false)", 409)
        return ok({"mode": control.set_paused(action == "pause", "dashboard", body().get("reason"))})

    @app.post("/api/life/threads/<int:thread_id>/<action>")
    def life_thread_action(thread_id: int, action: str):
        targets = {"pause": "paused", "resume": "active", "complete": "completed", "abandon": "abandoned",
                   "cancel": "cancelled"}
        if action not in targets:
            return bad("unknown action", 404)
        try:
            return ok(store.set_thread_status(thread_id, targets[action], body().get("reason") or "dashboard", "dashboard"))
        except TransitionError as exc:
            return bad(str(exc))

    @app.post("/api/life/plans/<int:plan_id>/<action>")
    def life_plan_action(plan_id: int, action: str):
        targets = {"pause": "paused", "resume": "planned", "cancel": "cancelled", "abandon": "abandoned"}
        if action not in targets:      # completing needs a recorded event: not offered here
            return bad("unknown action", 404)
        try:
            return ok(store.set_plan_status(plan_id, targets[action], body().get("reason") or "dashboard", "dashboard"))
        except TransitionError as exc:
            return bad(str(exc))

    @app.patch("/api/life/events/<int:event_id>")
    def life_event_correct(event_id: int):
        data = body()
        content, reason = (data.get("content") or "").strip(), (data.get("reason") or "").strip()
        if not content or not reason:
            return bad("content and reason are required")
        violations = ConsistencyGuard(violetdb)._persona_violations(content)
        if violations:
            return bad("correction conflicts with the persona core: " + ", ".join(v.check for v in violations))
        try:
            return ok(store.correct_event(event_id, content, reason))
        except TransitionError as exc:
            return bad(str(exc))

    @app.post("/api/life/events/<int:event_id>/retract")
    def life_event_retract(event_id: int):
        reason = (body().get("reason") or "").strip()
        if not reason:
            return bad("reason required")
        try:
            return ok(store.retract_event(event_id, reason))
        except TransitionError as exc:
            return bad(str(exc))

    @app.get("/api/life/<entity>/<int:entity_id>/history")
    def life_history(entity: str, entity_id: int):
        getters = {"thread": store.thread, "plan": store.plan, "event": store.event}
        if entity not in getters:
            return bad("unknown entity", 404)
        row = getters[entity](entity_id)
        if not row:
            return bad("not found", 404)
        return ok({"entity": row, "revisions": store.revisions(entity, entity_id)})

    @app.post("/api/life/flags/<int:flag_id>/resolve")
    def life_flag_resolve(flag_id: int):
        store.resolve_flag(flag_id)
        return ok({"ok": True})
