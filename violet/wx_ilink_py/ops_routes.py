"""Dashboard: work waiting for a human after a crash (phase 7, D4). Same admin authentication as every route."""

from __future__ import annotations

from flask import Flask, jsonify, redirect, request

from ai.reconcile import RESOLUTIONS, pending_items, resolve


def register_ops_routes(app: Flask, violetdb) -> None:
    @app.get("/reconciliation")
    def reconciliation_page():                        # 0.7.1: the old page is gone
        return redirect("/control#diagnostics")

    @app.get("/api/reconciliation")
    def reconciliation_list():
        return jsonify({**pending_items(violetdb), "resolutions": {k: sorted(v) for k, v in RESOLUTIONS.items()}})

    @app.post("/api/reconciliation/<kind>/<item_id>")
    def reconciliation_resolve(kind: str, item_id: str):
        if kind not in RESOLUTIONS:
            return jsonify({"error": "unknown kind"}), 404
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(resolve(violetdb, kind, item_id, str(data.get("resolution") or ""), actor="dashboard"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
