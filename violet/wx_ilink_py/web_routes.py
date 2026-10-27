"""Dashboard API for web access (phase 5B). Same admin authentication as every route."""

from __future__ import annotations

import json

from flask import Flask, jsonify, render_template, request

from ai.settings import env_bool, env_str
from ai.web.explorer import WebExplorer
from ai.web.findings import FindingsStore
from ai.web.service import WebConfig


def register_web_routes(app: Flask, violetdb) -> None:
    store = FindingsStore(violetdb)

    def ok(payload, status=200):
        return jsonify(payload), status

    def bad(message, status=400):
        return jsonify({"error": message}), status

    def body() -> dict:
        return request.get_json(silent=True) or {}

    @app.get("/web")
    def web_page():
        return render_template("web.html")

    @app.get("/api/web/status")
    def web_status():
        cfg = WebConfig.from_env()
        day = violetdb.now()[:10]
        failures = violetdb.fetch_all("SELECT kind, origin, status, error_code, target, created_at FROM web_requests "
                                   "WHERE created_at >= ? AND status IN ('error', 'blocked', 'budget') "
                                   "ORDER BY id DESC LIMIT 30", (day,))
        return ok({
            "web_enabled": env_bool("VIOLET_WEB_ENABLED", False),
            "search_provider": env_str("VIOLET_WEB_SEARCH_PROVIDER", "none"),
            "limits": {"max_searches_per_day": cfg.max_searches_per_day, "max_fetches_per_day": cfg.max_fetches_per_day},
            "used_today": {"search": store.count_today("search"), "fetch": store.count_today("fetch"),
                           "explorer_search": store.count_today("search", "explorer")},
            "usage": store.usage_today(),
            "recent_failures": failures,
            "explorer": {"mode": WebExplorer(violetdb).mode(), "authorized": WebExplorer.authorized(),
                         "cost_today": WebExplorer(violetdb).cost_today()},
        })

    @app.get("/api/web/requests")
    def web_requests():
        return ok({"requests": store.requests(limit=min(int(request.args.get("limit", 100)), 500))})

    @app.get("/api/web/findings")
    def web_findings():
        return ok({"findings": store.list(limit=min(int(request.args.get("limit", 100)), 500),
                                          status=request.args.get("status"), origin=request.args.get("origin"))})

    @app.post("/api/web/findings/<int:finding_id>/<action>")
    def web_finding_action(finding_id: int, action: str):
        if action not in ("disable", "enable"):
            return bad("unknown action", 404)
        if not store.get(finding_id):
            return bad("not found", 404)
        return ok(store.set_status(finding_id, "disabled" if action == "disable" else "active"))

    @app.get("/api/web/sources")
    def web_sources():
        return ok({"sources": store.sources()})

    @app.post("/api/web/sources")
    def web_source_set():
        data = body()
        domain = str(data.get("domain") or "").strip()
        if not domain or "/" in domain or " " in domain:
            return bad("domain required (e.g. example.org)")
        try:
            return ok(store.set_source(domain, str(data.get("status") or "disabled"), data.get("reason")))
        except ValueError as exc:
            return bad(str(exc))

    @app.get("/api/web/explorer")
    def web_explorer():
        explorer = WebExplorer(violetdb)
        runs = violetdb.fetch_all("SELECT * FROM life_engine_runs WHERE run_key LIKE 'explore:%' ORDER BY id DESC LIMIT 30")
        return ok({"mode": explorer.mode(), "authorized": explorer.authorized(), "runs": runs,
                   "cost_today": explorer.cost_today()})

    @app.post("/api/web/explorer/<action>")
    def web_explorer_action(action: str):
        if action not in ("enable", "disable"):
            return bad("unknown action", 404)
        try:
            return ok({"mode": WebExplorer(violetdb).set_enabled(action == "enable")})
        except PermissionError as exc:
            return bad(str(exc), 403)

    @app.get("/api/web/reading-records")
    def web_reading_records():
        rows = violetdb.fetch_all("SELECT * FROM life_events WHERE event_type = 'reading_record' ORDER BY id DESC LIMIT 100")
        for row in rows:
            try:
                detail = json.loads(row.get("source_detail") or "{}")
            except ValueError:
                detail = {}
            row["source_url"] = detail.get("external_ref")
            row["finding_id"] = detail.get("finding_id")
        return ok({"reading_records": rows})
