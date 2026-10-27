"""Phase 9.4.3: the Control Center timetable page's API (/api/control/schedule/...).

Every route is behind the dashboard's admin authentication (auth.py before_request: Bearer token or the admin
session; a state change with the session cookie must also be same-origin -- the CSRF check). All writes go through
ScheduleStore, which validates every field (the page's own checks are only a convenience) and writes a revision
with the actor ("admin:session" / "admin:bearer") and the source. Request bodies are JSON with a size limit; an
import file arrives as text in the body, so no local path is ever read. Error answers carry the store's field
problems or an exception class name -- never a path, a credential or a stack trace. Logs carry ids and counts only.

Violet sees a change on her next lookup: the violet-schedule tools read the store on every call (no cache, no restart).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from flask import Flask, Response, jsonify, request

logger = logging.getLogger("violet.schedule")
MAX_BODY = 64 * 1024
MAX_IMPORT_BODY = 400 * 1024          # the file (256 KB of text, JSON-escaped) plus the other fields


def register_schedule_routes(app: Flask, violetdb) -> None:
    from ai.schedule import admin, importer
    from ai.schedule.model import ScheduleValidationError
    from ai.schedule.store import PURGE_CONFIRM_ALL, ScheduleStore

    store = ScheduleStore(violetdb)
    app.register_error_handler(_TooLarge, lambda _exc: bad("请求太大", 413))

    def ok(data: Any = None, status: int = 200) -> Response:
        return jsonify({"ok": True, "data": data}), status

    def bad(message: str, status: int = 400, **extra) -> Response:
        return jsonify({"ok": False, "error": message, **extra}), status

    def actor() -> str:
        return "admin:" + str(request.environ.get("violet.auth") or "unknown")

    def body(limit: int = MAX_BODY) -> dict[str, Any]:
        if request.content_length is not None and request.content_length > limit:
            raise _TooLarge()
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def pick(data: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
        return {k: data[k] for k in names if k in data}

    def write(fn: Callable[[], Any], what: str) -> Response:
        """Run one store write; validation problems -> 400 (nothing changed), anything else -> 500 (rolled back)."""
        try:
            return ok(fn())
        except _TooLarge:
            return bad("请求太大", 413)
        except ScheduleValidationError as exc:
            return bad("保存失败，没有改动", problems=exc.problems)
        except Exception as exc:                                   # the store rolled back; no detail leaves
            logger.warning("[schedule] %s failed: %s", what, exc.__class__.__name__)
            return bad(f"保存失败，没有改动（{exc.__class__.__name__}）", 500)

    def today() -> datetime:
        from ai import settings
        return settings.now_local()

    def term_or_404(term_id: int) -> dict[str, Any] | None:
        return violetdb.fetch_one("SELECT * FROM schedule_terms WHERE id = ?", (term_id,))

    # ------------------------------------------------------------------ overview / terms
    @app.get("/api/control/schedule")
    def schedule_overview():
        from ai.settings import env_bool
        now = today()
        terms = violetdb.fetch_all("SELECT * FROM schedule_terms ORDER BY start_date DESC, id DESC")
        for t in terms:
            t.update(admin.term_state(t, now.date()))
            t["courses"] = violetdb.fetch_one("SELECT COUNT(*) AS n FROM schedule_courses WHERE term_id = ? AND "
                                           "status = 'active'", (t["id"],))["n"]
        current = next((t["id"] for t in terms if t["state"] == "active"), terms[0]["id"] if terms else None)
        try:
            from ai.tool_manager import tool_store
            server = tool_store().servers().get("violet-schedule") or {}
        except Exception:
            server = {}
        return ok({"enabled": env_bool("VIOLET_SCHEDULE_ENABLED", False),
                   "server_disabled": server.get("enabled") is False,
                   "tools_disabled": list(server.get("disabled_tools") or []),
                   "today": now.date().isoformat(), "terms": terms, "current_term_id": current,
                   "last_updated": store.load().last_updated, "limits": importer.LIMITS,
                   "purge_confirm_text": PURGE_CONFIRM_ALL})

    @app.post("/api/control/schedule/terms")
    def schedule_term_create():
        data = body()
        return write(lambda: {"id": store.create_term(**pick(data, ("name", "start_date", "end_date", "week1_monday")),
                                                      timezone=data.get("timezone"), actor=actor())}, "term create")

    @app.get("/api/control/schedule/terms/<int:term_id>")
    def schedule_term_detail(term_id: int):
        detail = admin.term_detail(store, term_id)
        if detail is None:
            return bad("not found", 404)
        detail["term"].update(admin.term_state(detail["term"], today().date()))
        detail["conflicts"] = admin.term_conflicts(store.load(), term_id)
        detail["data_version"] = store.data_version()
        return ok(detail)

    @app.post("/api/control/schedule/terms/<int:term_id>")
    def schedule_term_update(term_id: int):
        if term_or_404(term_id) is None:
            return bad("not found", 404)
        changes = pick(body(), ("name", "start_date", "end_date", "week1_monday", "timezone", "status"))
        if not changes:
            return bad("没有要保存的修改")
        return write(lambda: {"changed": store.update_term(term_id, changes, actor=actor())}, "term update")

    @app.post("/api/control/schedule/terms/<int:term_id>/archive")
    def schedule_term_archive(term_id: int):
        if term_or_404(term_id) is None:
            return bad("not found", 404)
        return write(lambda: {"changed": store.archive_term(term_id, actor=actor())}, "term archive")

    @app.post("/api/control/schedule/terms/<int:term_id>/purge")
    def schedule_term_purge(term_id: int):
        term = term_or_404(term_id)
        if term is None:
            return bad("not found", 404)
        data = body()
        if data.get("confirm") is not True or str(data.get("confirm_name") or "") != term["name"]:
            return bad("删除学期课表需要确认：confirm: true，并输入学期名称")
        return write(lambda: {"deleted": store.purge_term(term_id, confirm=True, actor=actor())}, "term purge")

    @app.get("/api/control/schedule/scope")
    def schedule_scope():
        term_id = request.args.get("term_id")
        if term_id is not None and (not term_id.isdigit() or term_or_404(int(term_id)) is None):
            return bad("not found", 404)
        return ok({"rows": store.scope_counts(int(term_id) if term_id else None)})

    @app.post("/api/control/schedule/purge-all")
    def schedule_purge_all():
        data = body()
        if data.get("confirm") is not True or data.get("confirm_text") != PURGE_CONFIRM_ALL:
            return bad(f"删除全部课表需要二次确认：confirm: true，并输入“{PURGE_CONFIRM_ALL}”")
        return write(lambda: {"deleted": store.purge_all(confirm=True, actor=actor())}, "purge all")

    @app.put("/api/control/schedule/terms/<int:term_id>/periods")
    def schedule_periods(term_id: int):
        if term_or_404(term_id) is None:
            return bad("not found", 404)
        rows = body().get("periods")
        if not isinstance(rows, list) or len(rows) > 20:
            return bad("periods 要是最多 20 项的列表")
        entries = [(r.get("period_no"), r.get("start_time"), r.get("end_time")) if isinstance(r, dict) else r
                   for r in rows]
        return write(lambda: {"periods": store.set_periods(term_id, entries, actor=actor())}, "periods")

    @app.get("/api/control/schedule/terms/<int:term_id>/week")
    def schedule_week(term_id: int):
        term = term_or_404(term_id)
        if term is None:
            return bad("not found", 404)
        week = request.args.get("week")
        if week is None:
            week = admin.term_state(term, today().date())["current_week"] or 1
        try:
            week = int(week)
        except (TypeError, ValueError):
            return bad("week 要是整数")
        return ok(admin.week_view(store.load(), term_id, week))

    @app.get("/api/control/schedule/terms/<int:term_id>/revisions")
    def schedule_revisions(term_id: int):
        if term_or_404(term_id) is None:
            return bad("not found", 404)
        try:
            limit = max(1, min(200, int(request.args.get("limit") or 100)))
            before = int(request.args["before_id"]) if request.args.get("before_id") else None
        except ValueError:
            return bad("limit / before_id 要是整数")
        return ok(store.term_revisions(term_id, limit=limit, before_id=before))

    # ------------------------------------------------------------------ courses / slots / exceptions
    COURSE_FIELDS = ("name", "kind", "code", "teacher", "notes")
    SLOT_FIELDS = ("weekday", "weeks", "start", "end", "period_start", "period_end", "location", "valid_from",
                   "valid_until")
    EXCEPTION_FIELDS = ("kind", "on_date", "slot_id", "new_date", "new_start", "new_end", "new_period_start",
                        "new_period_end", "new_location", "reason")
    EXAM_FIELDS = ("name", "exam_date", "kind", "course_id", "start", "end", "location", "seat", "notes")
    HOLIDAY_FIELDS = ("kind", "name", "start_date", "end_date", "follows_date")

    def exists(table: str, row_id: int) -> bool:
        return violetdb.fetch_one(f"SELECT id FROM {table} WHERE id = ?", (row_id,)) is not None

    @app.post("/api/control/schedule/terms/<int:term_id>/courses")
    def schedule_course_create(term_id: int):
        if term_or_404(term_id) is None:
            return bad("not found", 404)
        data = pick(body(), COURSE_FIELDS)
        return write(lambda: {"id": store.add_course(term_id, **{"name": None, **data}, actor=actor())}, "course create")

    @app.post("/api/control/schedule/courses/<int:course_id>")
    def schedule_course_update(course_id: int):
        if not exists("schedule_courses", course_id):
            return bad("not found", 404)
        changes = pick(body(), COURSE_FIELDS)
        if not changes:
            return bad("没有要保存的修改")
        return write(lambda: {"changed": store.update_course(course_id, changes, actor=actor())}, "course update")

    @app.post("/api/control/schedule/courses/<int:course_id>/disable")
    def schedule_course_disable(course_id: int):
        if not exists("schedule_courses", course_id):
            return bad("not found", 404)
        return write(lambda: {"changed": store.delete_course(course_id, actor=actor())}, "course disable")

    @app.post("/api/control/schedule/courses/<int:course_id>/restore")
    def schedule_course_restore(course_id: int):
        if not exists("schedule_courses", course_id):
            return bad("not found", 404)
        return write(lambda: {"changed": store.restore_course(course_id, actor=actor())}, "course restore")

    @app.post("/api/control/schedule/courses/<int:course_id>/purge")
    def schedule_course_purge(course_id: int):
        if not exists("schedule_courses", course_id):
            return bad("not found", 404)
        if body().get("confirm") is not True:
            return bad("彻底删除需要确认（confirm: true）")
        return write(lambda: {"deleted": store.purge_course(course_id, confirm=True, actor=actor())}, "course purge")

    @app.post("/api/control/schedule/courses/<int:course_id>/slots")
    def schedule_slot_create(course_id: int):
        if not exists("schedule_courses", course_id):
            return bad("not found", 404)
        data = {"weekday": None, "weeks": None, **pick(body(), SLOT_FIELDS)}
        return write(lambda: {"id": store.add_slot(course_id, **data, actor=actor())}, "slot create")

    @app.post("/api/control/schedule/slots/<int:slot_id>")
    def schedule_slot_update(slot_id: int):
        if not exists("schedule_slots", slot_id):
            return bad("not found", 404)
        changes = pick(body(), SLOT_FIELDS)
        if not changes:
            return bad("没有要保存的修改")
        return write(lambda: {"changed": store.update_slot(slot_id, changes, actor=actor())}, "slot update")

    @app.post("/api/control/schedule/slots/<int:slot_id>/delete")
    def schedule_slot_delete(slot_id: int):
        if not exists("schedule_slots", slot_id):
            return bad("not found", 404)
        return write(lambda: {"changed": store.delete_slot(slot_id, actor=actor())}, "slot delete")

    @app.post("/api/control/schedule/courses/<int:course_id>/exceptions")
    def schedule_exception_create(course_id: int):
        if not exists("schedule_courses", course_id):
            return bad("not found", 404)
        data = {"kind": None, "on_date": None, **pick(body(), EXCEPTION_FIELDS)}
        return write(lambda: {"id": store.add_exception(course_id, **data, actor=actor())}, "exception create")

    @app.post("/api/control/schedule/exceptions/<int:exception_id>/revoke")
    def schedule_exception_revoke(exception_id: int):
        if not exists("schedule_exceptions", exception_id):
            return bad("not found", 404)
        return write(lambda: {"changed": store.revoke_exception(exception_id, actor=actor())}, "exception revoke")

    # ------------------------------------------------------------------ exams / holidays
    @app.post("/api/control/schedule/terms/<int:term_id>/exams")
    def schedule_exam_create(term_id: int):
        if term_or_404(term_id) is None:
            return bad("not found", 404)
        data = {"name": None, "exam_date": None, **pick(body(), EXAM_FIELDS)}
        return write(lambda: {"id": store.add_exam(term_id, **data, actor=actor())}, "exam create")

    @app.post("/api/control/schedule/exams/<int:exam_id>")
    def schedule_exam_update(exam_id: int):
        if not exists("schedule_exams", exam_id):
            return bad("not found", 404)
        changes = pick(body(), EXAM_FIELDS)
        if not changes:
            return bad("没有要保存的修改")
        return write(lambda: {"changed": store.update_exam(exam_id, changes, actor=actor())}, "exam update")

    @app.post("/api/control/schedule/exams/<int:exam_id>/status")
    def schedule_exam_status(exam_id: int):
        if not exists("schedule_exams", exam_id):
            return bad("not found", 404)
        status = body().get("status")
        return write(lambda: {"changed": store.set_exam_status(exam_id, str(status), actor=actor())}, "exam status")

    @app.post("/api/control/schedule/terms/<int:term_id>/holidays")
    def schedule_holiday_create(term_id: int):
        if term_or_404(term_id) is None:
            return bad("not found", 404)
        data = {"kind": None, "name": None, "start_date": None, **pick(body(), HOLIDAY_FIELDS)}
        return write(lambda: {"id": store.add_holiday(term_id, **data, actor=actor())}, "holiday create")

    @app.post("/api/control/schedule/holidays/<int:holiday_id>/delete")
    def schedule_holiday_delete(holiday_id: int):
        if not exists("schedule_holidays", holiday_id):
            return bad("not found", 404)
        return write(lambda: {"changed": store.delete_holiday(holiday_id, actor=actor())}, "holiday delete")

    # ------------------------------------------------------------------ import / export
    def parse_upload() -> tuple[Any, dict[str, Any]]:
        data = body(MAX_IMPORT_BODY)
        term = None
        if data.get("term_id") not in (None, ""):
            if not str(data["term_id"]).isdigit() or term_or_404(int(data["term_id"])) is None:
                raise ScheduleValidationError({"term_id": "选择的学期不存在"})
            term = term_or_404(int(data["term_id"]))
        content = data.get("content")
        if not isinstance(content, str):
            raise ScheduleValidationError({"content": "要把文件内容作为文字发送"})
        fmt = data.get("format")
        if fmt not in (None, "", "csv", "json"):
            raise ScheduleValidationError({"format": "只支持 csv / json"})
        return importer.parse(content, str(data.get("file_name") or ""), fmt or None, term), data

    @app.post("/api/control/schedule/import/preview")
    def schedule_import_preview():
        try:
            parsed, data = parse_upload()
        except _TooLarge:
            return bad("文件太大", 413)
        except ScheduleValidationError as exc:
            return bad("没法预览", problems=exc.problems)
        try:
            # phase 9.4 closeout: CSV mode merge (default, 追加 / 更新) | replace (整学期替换); JSON has its own rule
            result = importer.preview(store, parsed, actor=actor(), mode=data.get("mode"))
        except ScheduleValidationError as exc:
            return bad("没法预览", problems=exc.problems)
        logger.info("[schedule] import preview (%s): %d errors, %s", parsed.fmt, len(result["errors"]),
                    result.get("counts"))
        return ok(result)

    @app.post("/api/control/schedule/import/confirm")
    def schedule_import_confirm():
        try:
            parsed, data = parse_upload()
        except _TooLarge:
            return bad("文件太大", 413)
        except ScheduleValidationError as exc:
            return bad("没法导入", problems=exc.problems)
        try:
            result = importer.commit(store, parsed, sha256=str(data.get("sha256") or ""),
                                     data_version=str(data.get("data_version") or ""), actor=actor(),
                                     mode=data.get("mode"), expected_counts=data.get("expected_counts"))
        except importer.StaleImport as exc:
            return bad(str(exc), 409, stale=True)
        except ScheduleValidationError as exc:
            return bad("导入失败，课表没有任何改动", problems=exc.problems)
        except Exception as exc:
            logger.warning("[schedule] import failed: %s", exc.__class__.__name__)
            return bad(f"导入失败，课表没有任何改动（{exc.__class__.__name__}）", 500)
        return ok(result)

    @app.get("/api/control/schedule/template/<kind>")
    def schedule_template(kind: str):
        from pathlib import Path
        files = {"csv": ("schedule_template.csv", "text/csv; charset=utf-8"),
                 "json": ("schedule_template.json", "application/json; charset=utf-8"),
                 "readme": ("README.md", "text/markdown; charset=utf-8")}
        if kind not in files:
            return bad("not found", 404)
        name, mime = files[kind]
        path = Path(__file__).resolve().parent.parent / "schedule_templates" / name   # fixed files only
        return Response(path.read_bytes(), mimetype=mime.split(";")[0], headers={
            "Content-Type": mime, "Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/control/schedule/export")
    def schedule_export():
        term_id = request.args.get("term_id") or ""
        fmt = request.args.get("format") or "json"
        if not term_id.isdigit() or term_or_404(int(term_id)) is None:
            return bad("not found", 404)
        if fmt not in ("csv", "json"):
            return bad("format 只能是 csv / json")
        personal = request.args.get("personal", "1") != "0"
        if fmt == "csv":
            body_text = admin.export_csv(store, int(term_id), include_personal=personal)
            mime = "text/csv; charset=utf-8"
        else:
            import json
            body_text = json.dumps(admin.export_document(store, int(term_id), include_personal=personal),
                                   ensure_ascii=False, indent=1)
            mime = "application/json; charset=utf-8"
        logger.info("[schedule] term %s exported (%s, personal=%s)", term_id, fmt, personal)
        return Response(body_text, headers={"Content-Type": mime, "Content-Disposition":
                                            f'attachment; filename="violet-schedule-term{term_id}.{fmt}"'})


class _TooLarge(Exception):
    pass
