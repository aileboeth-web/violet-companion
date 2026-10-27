"""Dashboard API for the user profile (phase 3).

All routes sit behind the phase-1 admin authentication (wx_ilink_py/auth.py);
nothing here is public. Deleting is a real deletion (see DELETION_POLICY).
"""

from __future__ import annotations

from flask import Flask, jsonify, request

from ai.onboarding import OnboardingPlanner
from ai.profile_store import Profile, purge_event_memory

DELETION_POLICY = {
    "disable": "数据保留，但不再进入任何模型上下文、检索或主动话题；它的值在拼装的上下文中会被遮蔽。可以重新启用。",
    "delete": (
        "物理删除：删除这条信息及其同值的历史版本；把它的值和原话证据从聊天记录、收件箱、发件箱、后台记忆任务、"
        "修订记录、其他证据字段中替换为“[已删除的个人信息]”；同时删除内容中包含该值的事件记忆和由它导入的旧核心记忆；"
        "启用 SQLite secure_delete 并做 WAL checkpoint，避免旧数据留在数据库文件中。不可恢复。"
    ),
    "audit": "审计日志只记录：对象类型、id、动作、操作者、key、数量和时间；从不记录被删除的值或原话。",
    "not_covered": "已经发送到微信的消息、外部模型服务商的日志、数据库备份文件不在本系统可删除范围内。",
}


def register_profile_routes(app: Flask, violetdb) -> None:
    profile = Profile(violetdb)
    onboarding = OnboardingPlanner(violetdb, profile.facts)

    def ok(payload, status=200):
        return jsonify(payload), status

    def bad(message, status=400):
        return jsonify({"error": message}), status

    def body() -> dict:
        return request.get_json(silent=True) or {}

    # -- overview ---------------------------------------------------------------------
    @app.get("/api/profile")
    def profile_overview():
        return ok({
            "facts": profile.facts.list_facts(),
            "situations": {"current": profile.situations.current(include_disabled=True),
                           "recent": profile.situations.list(limit=30)},
            "preferences": {"effective": profile.preferences.effective(), "all": profile.preferences.list()},
            "questions": violetdb.fetch_all("SELECT * FROM profile_questions ORDER BY id DESC LIMIT 50"),
            "audit": profile.audit.entries(limit=50),
            "deletion_policy": DELETION_POLICY,
        })

    @app.get("/api/profile/deletion-policy")
    def deletion_policy():
        return ok(DELETION_POLICY)

    # -- facts --------------------------------------------------------------------------
    @app.post("/api/profile/facts")
    def create_fact():
        data = body()
        result = profile.facts.remember(data.get("key", ""), data.get("value", ""), source="dashboard",
                                        actor="dashboard", valid_until=data.get("valid_until"))
        return ok(result, 201 if result["action"] in ("created", "superseded_previous") else 400)

    @app.patch("/api/profile/facts/<int:fact_id>")
    def correct_fact(fact_id: int):
        data = body()
        fact = profile.facts.get_fact(fact_id)
        if not fact:
            return bad("not found", 404)
        try:
            if data.get("confirm"):
                result = profile.facts.confirm_candidate(fact_id, key=data.get("key"), value=data.get("value"))
            else:
                if not data.get("value"):
                    return bad("value required")
                result = profile.facts.correct_from_dashboard(fact_id, data["value"])
                if result["action"] in ("failed", "ambiguous"):     # phase 9.1.1: nothing was written
                    return ok(result, 400)
        except ValueError as exc:
            return bad(str(exc))
        return ok(result)

    @app.post("/api/profile/facts/<int:fact_id>/disable")
    def disable_fact(fact_id: int):
        try:
            return ok(profile.facts.set_enabled(fact_id, False))
        except ValueError:
            return bad("not found", 404)

    @app.post("/api/profile/facts/<int:fact_id>/enable")
    def enable_fact(fact_id: int):
        try:
            return ok(profile.facts.set_enabled(fact_id, True))
        except ValueError:
            return bad("not found", 404)

    @app.delete("/api/profile/facts/<int:fact_id>")
    def delete_fact(fact_id: int):
        cascade = request.args.get("cascade", "1") != "0"
        try:
            return ok(profile.facts.delete(fact_id, cascade=cascade))
        except ValueError:
            return bad("not found", 404)

    @app.get("/api/profile/facts/<int:fact_id>/source")
    def fact_source(fact_id: int):
        try:
            return ok(profile.facts.source_of(fact_id))
        except ValueError:
            return bad("not found", 404)

    # -- situations -----------------------------------------------------------------------
    @app.get("/api/profile/situations")
    def situations():
        return ok({"current": profile.situations.current(include_disabled=True), "recent": profile.situations.list()})

    @app.post("/api/profile/situations")
    def create_situation():
        data = body()
        result = profile.situations.observe(data.get("kind", ""), data.get("value", ""), "dashboard",
                                            until=data.get("until"), actor="dashboard")
        return ok(result, 201 if result["action"] == "created" else 400)

    @app.post("/api/profile/situations/<int:situation_id>/<action>")
    def toggle_situation(situation_id: int, action: str):
        if action not in ("disable", "enable"):
            return bad("unknown action", 404)
        if not profile.situations.get(situation_id):
            return bad("not found", 404)
        return ok(profile.situations.set_enabled(situation_id, action == "enable"))

    @app.delete("/api/profile/situations/<int:situation_id>")
    def delete_situation(situation_id: int):
        try:
            return ok(profile.situations.delete(situation_id))
        except ValueError:
            return bad("not found", 404)

    # -- preferences ------------------------------------------------------------------------
    @app.get("/api/profile/preferences")
    def preferences():
        return ok({"effective": profile.preferences.effective(), "all": profile.preferences.list()})

    @app.put("/api/profile/preferences/<key>")
    def set_preference(key: str):
        result = profile.preferences.set_explicit(key, body().get("value", ""), source="dashboard", actor="dashboard")
        return ok(result, 200 if result["action"] in ("set", "already_known") else 400)

    @app.delete("/api/profile/preferences/<key>")
    def clear_preference(key: str):
        return ok({"cleared": profile.preferences.clear_explicit(key, request.args.get("value"))})

    @app.delete("/api/profile/preferences/id/<int:pref_id>")
    def delete_preference(pref_id: int):
        try:
            profile.preferences.delete(pref_id)
        except ValueError:
            return bad("not found", 404)
        return ok({"deleted": pref_id})

    @app.post("/api/profile/questions/reset")
    def reset_questions():
        return ok({"reset": onboarding.reset()})

    # -- event memories: forbid / delete ---------------------------------------------------
    @app.post("/api/memories/<int:memory_id>/<action>")
    def toggle_memory(memory_id: int, action: str):
        if action not in ("disable", "enable"):
            return bad("unknown action", 404)
        if not violetdb.get_event_memory(memory_id):
            return bad("not found", 404)
        memory = violetdb.set_event_memory_enabled(memory_id, action == "enable")
        profile.audit.log("event_memory", memory_id, action, "dashboard")
        return ok(memory)

    @app.delete("/api/memories/<int:memory_id>")
    def delete_memory(memory_id: int):
        try:
            return ok(purge_event_memory(violetdb, memory_id, extra_phrases=body().get("redact") or []))
        except ValueError:
            return bad("not found", 404)
