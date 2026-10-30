"""Phase 9.4.3: the Control Center timetable page -- terms, periods, week view, courses, exceptions, exams,
holidays, CSV / JSON import with preview and version-checked commit, export, deletion with history cleanup, and the
link to the violet-schedule tools.

Fictional data only, an isolated SQLite database and data dir per test, the fake SDK for whole turns, no network,
no real model."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_control_center_p90 import BEARER, JSON, SAME, TOKEN, admin, api, app, env_guard, made  # noqa: F401
from tests.test_phase2_scenarios import make_world, say  # noqa: F401
from tests.test_schedule_mcp_p942 import FIND, clock, result_of, wants  # noqa: F401
from tests.test_web_p93 import chat_plays, offered


@pytest.fixture(autouse=True)
def _planned_tools_only(monkeypatch):
    """These tests describe the tools offered only when Intent plans them (VIOLET_CHAT_TOOLS_DIRECT=0); the default
    since 2026-10-30 (every read-only tool on every turn) is tested in tests/test_chat_polish_violet.py."""
    monkeypatch.setenv("VIOLET_CHAT_TOOLS_DIRECT", "0")

SH = timezone(timedelta(hours=8))
B = "/api/control/schedule"
TEACHER, ROOM, SEAT, NOTE = "虚构老师丙", "虚构楼甲301", "虚构座位42", "虚构的私人备注"
PERIODS = [{"period_no": n, "start_time": a, "end_time": b} for n, a, b in
           ((1, "08:00", "08:45"), (2, "08:55", "09:40"), (3, "10:00", "10:45"), (4, "10:55", "11:40"),
            (5, "14:00", "14:45"), (6, "14:55", "15:40"))]
CSV = ("课程,课程代码,类型,老师,星期,节次,开始,结束,周次,地点,备注\n"
       f"高等数学（虚构）,MATH1,理论,{TEACHER},1,1-2,,,1-16,{ROOM},{NOTE}\n"
       f"高等数学（虚构）,MATH1,理论,{TEACHER},3,3-4,,,1-16单,{ROOM},\n"
       "大学物理（虚构）,,理论,,2,,14:00,15:40,1-16,,\n"
       "物理实验（虚构）,,实验,,4,5-6,,,2-16双,虚构实验楼,\n")


def post(client, url, body=None, **kw):
    return client.post(url, json=body if body is not None else {}, headers={**SAME, **kw.pop("headers", {})}, **kw)


def ok(resp, status=200):
    assert resp.status_code == status, (resp.status_code, resp.get_data(as_text=True)[:500])
    body = resp.get_json()
    assert body["ok"] is True
    return body["data"]


@pytest.fixture()
def term(admin):
    """2026 秋（虚构）: week 1 = Mon 2026-09-07, six periods."""
    tid = ok(post(admin, f"{B}/terms", {"name": "2026 秋（虚构）", "start_date": "2026-09-07", "end_date": "2027-01-15",
                                       "timezone": "Asia/Shanghai"}))["id"]
    assert ok(admin.put(f"{B}/terms/{tid}/periods", json={"periods": PERIODS}, headers=SAME))["periods"] == 6
    return tid


def preview(client, content, name="t.csv", term_id=None, mode=None):
    extra = {"mode": mode} if mode else {}
    return ok(post(client, f"{B}/import/preview", {"file_name": name, "content": content, "term_id": term_id, **extra}))


def commit(client, content, p, name="t.csv", term_id=None, status=200, mode=None, expected=None):
    extra = {"mode": mode} if mode else {}
    if expected is not None:
        extra["expected_counts"] = expected
    resp = post(client, f"{B}/import/confirm", {"file_name": name, "content": content, "term_id": term_id,
                                                 "sha256": p["sha256"], "data_version": p["data_version"], **extra})
    return ok(resp, status) if status == 200 else resp


def rows(violetdb, table):
    return violetdb.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]


def all_schedule_text(violetdb) -> str:
    from ai.schedule.schema import TABLES
    return json.dumps({t: violetdb.fetch_all(f"SELECT * FROM {t}") for t in TABLES}, ensure_ascii=False)


def snapshot(violetdb):
    from ai.schedule.store import CONTENT_TABLES
    return {t: violetdb.fetch_all(f"SELECT * FROM {t} ORDER BY id") for t in (*CONTENT_TABLES, "schedule_revisions",
                                                                         "schedule_imports")}


# ----------------------------------------------------------------------------------------------- access control

def test_schedule_routes_need_login_and_same_origin(app, api, admin, violetdb, term):
    rules = [r for r in app.url_map.iter_rules() if r.rule.startswith(B)]
    assert len(rules) >= 28
    for rule in rules:
        path = re.sub(r"<(?:[^:<>]+:)?[^<>]+>", "1", rule.rule)
        for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
            resp = api.open(path, method=method, json={"confirm": True, "confirm_text": "删除全部课表"}, headers=JSON)
            assert resp.status_code == 401, (method, rule.rule)
    before = snapshot(violetdb)
    for origin in ({"Origin": "http://evil.example"}, {}):                     # cross-site, or no origin at all
        assert admin.post(f"{B}/terms/{term}/courses", json={"name": "跨站（虚构）"}, headers=origin).status_code == 403
        assert admin.post(f"{B}/purge-all", json={"confirm": True, "confirm_text": "删除全部课表"},
                          headers=origin).status_code == 403
    assert snapshot(violetdb) == before
    bearer = api.post(f"{B}/terms/{term}/courses", json={"name": "脚本（虚构）"}, headers=BEARER)
    assert bearer.status_code == 200
    created = violetdb.fetch_one("SELECT * FROM schedule_revisions WHERE entity = 'course' ORDER BY id DESC LIMIT 1")
    assert created["actor"] == "admin:bearer" and created["source"] == "manual"


def test_page_has_the_timetable_tab_and_never_parses_data_as_html(admin):
    page = admin.get("/control", headers={"Accept": "text/html"}).get_data(as_text=True)
    assert 'data-tab="schedule"' in page and "/static/control_schedule.js" in page and 'id="sch-week-grid"' in page
    static = Path(__file__).resolve().parent.parent / "wx_ilink_py" / "static"
    for name in ("control.js", "control_schedule.js"):
        code = (static / name).read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert sink not in code, (name, sink)
    assert "textContent" in (static / "control_schedule.js").read_text(encoding="utf-8")


def null_children(code: str) -> list[str]:
    """Top-level arguments of .replaceChildren(...) that can be null ("cond ? x : null" outside any bracket): the
    DOM would print them as the text "null". (h() drops null children itself, so nulls inside h(...) are fine.)"""
    def split(text: str) -> tuple[list[tuple[str, str]], int]:
        """(raw, depth-0 text) of each comma-separated argument up to the closing bracket; and where it ended."""
        depth, i, quote, args, raw, flat = 1, 0, None, [], [], []
        while i < len(text) and depth:
            ch = text[i]
            if quote:
                if ch == "\\":
                    raw.append(ch)
                    i += 1
                    ch = text[i]
                elif ch == quote:
                    quote = None
            elif ch in "'\"`":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if not depth:
                    break
            elif ch == "," and depth == 1:
                args.append(("".join(raw), "".join(flat)))
                raw, flat = [], []
                i += 1
                continue
            raw.append(ch)
            if depth == 1 and not quote and ch not in ")]}":
                flat.append(ch)
            i += 1
        args.append(("".join(raw), "".join(flat)))
        return args, i

    def nullable(args: list[tuple[str, str]]) -> list[str]:
        out = []
        for raw, flat in args:
            body = raw.strip()
            if "?" in flat and re.search(r":\s*null\s*$", flat.strip()):
                out.append(body)
            elif body.startswith("...[") and body.endswith("]"):     # a spread array that is not filtered
                out += nullable(split(body[4:])[0])
        return out

    found = []
    for m in re.finditer(r"\.replaceChildren\(", code):
        found += nullable(split(code[m.end():])[0])
    return found


def test_page_script_never_prints_null_and_only_real_changes_can_be_confirmed():
    """Browser acceptance run 2: the term line ended in a literal "null" (replaceChildren prints null children), and
    a re-upload with only hand-edited rows offered "确认导入" without saying "没有任何变化"."""
    code = (Path(__file__).resolve().parent.parent / "wx_ilink_py" / "static" / "control_schedule.js").read_text(encoding="utf-8")
    assert null_children(code) == []
    assert "const canGo = p.ok && !p.errors.length && changes > 0;" in code
    assert '["created", "updated", "deleted"]' in code


def test_stored_markup_comes_back_as_inert_json(admin, term):
    evil = '<img src=x onerror="alert(1)">（虚构）'
    cid = ok(post(admin, f"{B}/terms/{term}/courses", {"name": evil, "teacher": "<script>x</script>"}))["id"]
    resp = admin.get(f"{B}/terms/{term}")
    assert resp.mimetype == "application/json" and resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in resp.headers
    course = next(c for c in resp.get_json()["data"]["courses"] if c["id"] == cid)
    assert course["name"] == evil                                   # stored as text; the page uses textContent


# ----------------------------------------------------------------------------------------------- terms and periods

def test_terms_periods_and_validation(admin, violetdb, term):
    over = ok(admin.get(B))
    t = next(x for x in over["terms"] if x["id"] == term)
    assert t["weeks_total"] == 19 and t["week1_monday"] == "2026-09-07" and over["enabled"] is False
    bad = post(admin, f"{B}/terms", {"name": "坏学期（虚构）", "start_date": "2026-09-07", "end_date": "2026-09-01",
                                     "timezone": "Mars/Base"})
    assert bad.status_code == 400 and set(bad.get_json()["problems"]) >= {"end_date", "timezone"}
    overlap = post(admin, f"{B}/terms", {"name": "重叠（虚构）", "start_date": "2026-10-01", "end_date": "2026-12-01",
                                         "timezone": "Asia/Shanghai"})
    assert overlap.status_code == 400 and "重叠" in overlap.get_json()["problems"]["start_date"]
    wrong = admin.put(f"{B}/terms/{term}/periods", json={"periods": [{"period_no": 1, "start_time": "09:00",
                                                                     "end_time": "08:00"}]}, headers=SAME)
    assert wrong.status_code == 400 and rows(violetdb, "schedule_periods") == 6        # nothing changed
    assert post(admin, f"{B}/terms/{term}/archive").status_code == 200
    assert ok(admin.get(B))["terms"][0]["state"] == "archived"
    ok(post(admin, f"{B}/terms/{term}", {"status": "active"}))
    assert post(admin, f"{B}/terms/999").status_code == 404


# ----------------------------------------------------------------------------------------------- week view

def build_by_hand(admin, term):
    math = ok(post(admin, f"{B}/terms/{term}/courses", {"name": "高等数学（虚构）", "teacher": TEACHER, "notes": NOTE}))["id"]
    mon = ok(post(admin, f"{B}/courses/{math}/slots", {"weekday": 1, "weeks": "1-16", "period_start": 1,
                                                        "period_end": 2, "location": ROOM}))["id"]
    wed = ok(post(admin, f"{B}/courses/{math}/slots", {"weekday": 3, "weeks": "1-16单", "period_start": 3,
                                                        "period_end": 4, "location": ROOM}))["id"]
    lab = ok(post(admin, f"{B}/terms/{term}/courses", {"name": "物理实验（虚构）", "kind": "lab"}))["id"]
    thu = ok(post(admin, f"{B}/courses/{lab}/slots", {"weekday": 4, "weeks": "2-16双", "start": "14:00",
                                                       "end": "15:40"}))["id"]
    return {"math": math, "mon": mon, "wed": wed, "lab": lab, "thu": thu}


def items(week, day_index):
    return [(i["course"], i["status"], i["start"][11:16] if i["start"] else None) for i in week["days"][day_index]["items"]]


def test_week_view_shows_parity_changes_exams_and_holidays(admin, term):
    ids = build_by_hand(admin, term)
    w1 = ok(admin.get(f"{B}/terms/{term}/week?week=1"))
    assert w1["parity"] == "odd" and w1["monday"] == "2026-09-07"
    assert items(w1, 2) == [("高等数学（虚构）", "normal", "10:00")] and items(w1, 3) == []      # Wed odd / Thu even only
    w2 = ok(admin.get(f"{B}/terms/{term}/week?week=2"))
    assert items(w2, 2) == [] and items(w2, 3) == [("物理实验（虚构）", "normal", "14:00")]
    assert w2["days"][0]["items"][0]["location"] == ROOM                        # the admin page shows the place
    ok(post(admin, f"{B}/courses/{ids['math']}/exceptions", {"kind": "cancel", "on_date": "2026-09-14", "slot_id": ids["mon"]}))
    ok(post(admin, f"{B}/courses/{ids['lab']}/exceptions", {"kind": "move", "on_date": "2026-09-17", "slot_id": ids["thu"],
                                                           "new_date": "2026-09-18", "new_start": "16:00", "new_end": "17:40"}))
    ok(post(admin, f"{B}/terms/{term}/exams", {"name": "高数小测（虚构）", "exam_date": "2026-09-16", "kind": "quiz",
                                               "course_id": ids["math"], "start": "19:00", "end": "20:00", "seat": SEAT}))
    ok(post(admin, f"{B}/terms/{term}/holidays", {"kind": "holiday", "name": "中秋（虚构）", "start_date": "2026-09-15"}))
    w2 = ok(admin.get(f"{B}/terms/{term}/week?week=2"))
    assert items(w2, 0) == [("高等数学（虚构）", "cancelled", "08:00")]
    assert w2["days"][1]["day_kind"] == "holiday"
    assert items(w2, 2) == [("高数小测（虚构）", "exam", "19:00")]
    assert items(w2, 3) == [("物理实验（虚构）", "moved_out", "14:00")] and items(w2, 4) == [("物理实验（虚构）", "moved_in", "16:00")]
    bad = post(admin, f"{B}/courses/{ids['math']}/exceptions", {"kind": "cancel", "on_date": "2026-09-15", "slot_id": ids["mon"]})
    assert bad.status_code == 400 and "on_date" in bad.get_json()["problems"]               # no class that day
    ok(post(admin, f"{B}/terms/{term}/archive"))                                           # archived: still viewable
    assert items(ok(admin.get(f"{B}/terms/{term}/week?week=1")), 2) == [("高等数学（虚构）", "normal", "10:00")]


def test_week_view_items_carry_what_a_click_acts_on(admin, violetdb, term):
    """Browser acceptance run 4: clicking a class said "这一节没有可以做的操作" -- the week view had no slot_id /
    exception_ids / exam_id. The page needs them to offer 停课 / 调课 / 改时间 / 换教室 / 撤销 and to edit an exam."""
    ids = build_by_hand(admin, term)
    exc = ok(post(admin, f"{B}/courses/{ids['math']}/exceptions", {"kind": "cancel", "on_date": "2026-09-07",
                                                                  "slot_id": ids["mon"]}))["id"]
    exam = ok(post(admin, f"{B}/terms/{term}/exams", {"name": "小测（虚构）", "exam_date": "2026-09-08"}))["id"]
    week = ok(admin.get(f"{B}/terms/{term}/week?week=1"))
    mon, tue, wed = (week["days"][i]["items"][0] for i in range(3))
    assert (mon["slot_id"], mon["exception_ids"]) == (ids["mon"], [exc])
    assert (wed["slot_id"], wed["exception_ids"], wed["exam_id"]) == (ids["wed"], [], None)
    assert (tue["exam_id"], tue["slot_id"]) == (exam, None)
    from ai.schedule.tools import ScheduleTools                      # Violet's own output is unchanged
    from ai.schedule.store import ScheduleStore
    assert "slot_id" not in json.dumps(ScheduleTools(ScheduleStore(violetdb)).range("2026-09-07", 3))


def test_course_edit_conflicts_disable_restore(admin, violetdb, term):
    ids = build_by_hand(admin, term)
    clash = ok(post(admin, f"{B}/terms/{term}/courses", {"name": "撞课（虚构）"}))["id"]
    ok(post(admin, f"{B}/courses/{clash}/slots", {"weekday": 1, "weeks": "1-4", "start": "09:00", "end": "10:00"}))
    conflicts = ok(admin.get(f"{B}/terms/{term}"))["conflicts"]
    assert [(c["a"], c["b"], c["count"]) for c in conflicts] == [("高等数学（虚构）", "撞课（虚构）", 4)]
    week = ok(admin.get(f"{B}/terms/{term}/week?week=1"))
    assert all(i["conflict_with"] for i in week["days"][0]["items"])
    bad = post(admin, f"{B}/slots/{ids['mon']}", {"start": "10:00", "end": "09:00"})
    assert bad.status_code == 400 and "end" in bad.get_json()["problems"]
    assert post(admin, f"{B}/courses/{ids['math']}/slots", {"weekday": 2, "weeks": "1-40", "period_start": 1,
                                                            "period_end": 2}).status_code == 400
    assert post(admin, f"{B}/courses/{ids['math']}/slots", {"weekday": 2, "weeks": "1-4", "period_start": 7,
                                                            "period_end": 8}).status_code == 400     # period 7 unset
    ok(post(admin, f"{B}/courses/{ids['math']}", {"teacher": "虚构老师丁"}))
    ok(post(admin, f"{B}/courses/{clash}/disable"))
    assert ok(admin.get(f"{B}/terms/{term}"))["conflicts"] == []
    ok(post(admin, f"{B}/courses/{clash}/restore"))
    assert len(ok(admin.get(f"{B}/terms/{term}"))["conflicts"]) == 1
    history = ok(admin.get(f"{B}/terms/{term}/revisions"))
    assert {(r["entity"], r["action"]) for r in history} >= {("course", "restore"), ("course", "delete"),
                                                             ("course", "update"), ("slot", "create"), ("period", "update")}
    assert all(r["actor"] == "admin:session" for r in history)


# ----------------------------------------------------------------------------------------------- import

def test_csv_import_preview_writes_nothing_then_commits_atomically(admin, violetdb, term, caplog):
    caplog.set_level(logging.DEBUG)
    before = snapshot(violetdb)
    p = preview(admin, CSV, term_id=term)
    assert p["ok"] and p["errors"] == [] and p["counts"]["created"] == 7          # 3 courses + 4 meetings
    assert snapshot(violetdb) == before                                              # preview: nothing kept
    assert not [r for r in caplog.records if re.search(r"\d+ created \(", r.getMessage())]   # nothing logged as done
    done = commit(admin, CSV, p, term_id=term)
    assert done["created"] == 7 and rows(violetdb, "schedule_courses") == 3 and rows(violetdb, "schedule_slots") == 4
    record = violetdb.fetch_one("SELECT * FROM schedule_imports")
    assert record["status"] == "applied" and record["file_name"] == "t.csv" and record["actor"] == "admin:session"
    assert TEACHER not in record["summary"]
    for r in caplog.records:
        for value in (TEACHER, ROOM, NOTE):
            assert value not in r.getMessage()


def test_reimport_is_idempotent_and_keeps_manual_changes(admin, violetdb, term):
    p = preview(admin, CSV, term_id=term)
    commit(admin, CSV, p, term_id=term)
    again = preview(admin, CSV, term_id=term)
    assert again["counts"] == {"created": 0, "updated": 0, "deleted": 0, "unchanged": 7, "skipped_manual": 0}
    assert any("导入过" in w["message"] for w in again["warnings"])
    lab = violetdb.fetch_one("SELECT id FROM schedule_courses WHERE name = '物理实验（虚构）'")["id"]
    ok(post(admin, f"{B}/courses/{lab}", {"teacher": "手动改的老师（虚构）"}))         # a manual change wins
    changed = CSV.replace("物理实验（虚构）,,实验,,4", "物理实验（虚构）,,实验,文件里的老师（虚构）,4").replace(
        "大学物理（虚构）,,理论,,2,,14:00,15:40,1-16,,\n", "")                       # and 物理 is gone from the file
    merged = preview(admin, changed, term_id=term)                                  # default (phase 9.4 closeout): merge
    assert merged["mode"] == "merge" and merged["counts"]["deleted"] == 0 and merged["deleted_courses"] == []
    p2 = preview(admin, changed, term_id=term, mode="replace")                      # only an explicit replace deletes
    skipped = [i for i in p2["items"] if i["action"] == "skipped_manual"]
    assert [(i["entity"], i["label"]) for i in skipped] == [("course", "物理实验（虚构）")]
    assert {i["label"] for i in p2["items"] if i["action"] == "deleted"} == {"大学物理（虚构）"}   # with its meetings
    commit(admin, changed, p2, term_id=term, mode="replace", expected=p2["counts"])
    assert violetdb.fetch_one("SELECT teacher FROM schedule_courses WHERE id = ?", (lab,))["teacher"] == "手动改的老师（虚构）"
    assert rows(violetdb, "schedule_courses") == 3                                    # soft delete, never duplicated
    third = preview(admin, changed, term_id=term, mode="replace")
    assert third["counts"]["created"] == 0 and third["counts"]["skipped_manual"] == 1


def test_manual_course_with_the_same_name_is_not_duplicated(admin, violetdb, term):
    ok(post(admin, f"{B}/terms/{term}/courses", {"name": "高等数学 (虚构)"}))         # entered by hand first
    p = preview(admin, CSV, term_id=term)
    assert any(i["action"] == "skipped_manual" and "同名" in i["reason"] for i in p["items"])
    commit(admin, CSV, p, term_id=term)
    assert rows(violetdb, "schedule_courses") == 3 and violetdb.fetch_one(
        "SELECT COUNT(*) AS n FROM schedule_courses WHERE name LIKE '高等数学%'")["n"] == 1


def test_bad_files_are_reported_line_by_line_and_never_applied(admin, violetdb, term):
    before = snapshot(violetdb)
    bad = ("课程,星期,节次,开始,结束,周次,老师\n"
           "高数（虚构）,8,1-2,,,1-16,\n"                     # line 2: weekday
           "高数（虚构）,1,,09:00,08:00,1-16,\n"              # line 3: end before start
           "高数（虚构）,2,1-2,10:00,11:00,1-16,\n"           # line 4: both periods and times
           "高数（虚构）,3,1-2,,,1-40,\n"                     # line 5: weeks
           "物理（虚构）,1,1-2,,,1-16,甲\n"
           "物理（虚构）,1,1-2,,,1-16,乙\n"                   # line 7: teacher differs
           "化学（虚构）,1,3-4,,,1-16,\n"
           "化学（虚构）,1,3-4,,,1-16,\n"                     # line 9: duplicate meeting
           "地理（虚构）,1,9-10,,,1-16,\n")                   # line 10: periods 9-10 not set (store check)
    p = preview(admin, bad, term_id=term)
    assert not p["ok"]
    lines = sorted({e["line"] for e in p["errors"]})
    assert lines == [2, 3, 4, 5, 7, 9], p["errors"]
    resp = post(admin, f"{B}/import/confirm", {"file_name": "t.csv", "content": bad, "term_id": term,
                                               "sha256": p["sha256"], "data_version": "x"})
    assert resp.status_code in (400, 409)
    only_db = preview(admin, "课程,星期,节次,周次\n地理（虚构）,1,9-10,1-16\n历史（虚构）,2,1-2,1-25\n", term_id=term)
    assert {(e["line"], e["field"]) for e in only_db["errors"]} == {(2, "开始节次"), (3, "周次")}   # all, not only the first
    assert snapshot(violetdb) == before
    assert preview(admin, "星期,周次\n1,1-16\n", term_id=term)["errors"][0]["message"].startswith("缺少必填的列")
    assert "选择" in preview(admin, CSV)["errors"][0]["message"]                    # CSV needs a target term


def test_limits_and_no_file_paths(admin, violetdb, term, monkeypatch):
    from ai.schedule import importer
    huge = "课程,星期,节次,周次\n" + "高数（虚构）,1,1-2,1-16\n" * 20000
    resp = post(admin, f"{B}/import/preview", {"file_name": "t.csv", "content": huge, "term_id": term})
    assert resp.status_code == 413
    monkeypatch.setitem(importer.LIMITS, "max_csv_rows", 3)
    many = "课程,星期,节次,周次\n" + "".join(f"课{i}（虚构）,1,1-2,1-16\n" for i in range(5))
    assert "最多 3 行" in preview(admin, many, term_id=term)["errors"][0]["message"]
    long_cell = "课程,星期,节次,周次\n" + "长" * 600 + ",1,1-2,1-16\n"
    assert "太长" in preview(admin, long_cell, term_id=term)["errors"][0]["message"]
    deep = json.dumps({"term": {"name": "x"}, "courses": [{"name": "a", "slots": [{"weekday": {"a": {"b": {"c": 1}}}}]}]})
    assert "嵌套太深" in preview(admin, deep, "d.json")["errors"][0]["message"]
    trick = preview(admin, "C:\\Windows\\win.ini", "../../../etc/passwd.json")
    assert trick["file_name"] == "passwd.json" and not trick["ok"] and "JSON 格式错误" in trick["errors"][0]["message"]
    assert post(admin, f"{B}/import/preview", {"file_name": "x.csv", "content": {"path": "/etc"}}).status_code == 400
    gbk = "课程,星期,节次,周次\n".encode("gbk").decode("utf-8", errors="replace")      # what the browser reads
    assert "UTF-8" in preview(admin, gbk, term_id=term)["errors"][0]["message"]
    assert admin.get(f"{B}/template/..%2F..%2Fdatabase.py").status_code == 404
    assert admin.get(f"{B}/template/csv").get_data(as_text=True).startswith("课程,")
    assert rows(violetdb, "schedule_courses") == 0


def json_doc(**extra):
    doc = {"format": "violet-schedule", "version": 1,
           "term": {"name": "2027 春（虚构）", "start_date": "2027-02-22", "end_date": "2027-07-02",
                    "timezone": "Asia/Shanghai"},
           "periods": [{"no": 1, "start": "08:00", "end": "08:45"}, {"no": 2, "start": "08:55", "end": "09:40"}],
           "courses": [{"name": "线性代数（虚构）", "code": "LA1", "teacher": TEACHER,
                        "slots": [{"weekday": 2, "periods": "1-2", "weeks": "1-16", "location": ROOM}]}],
           "exceptions": [{"course": "线性代数（虚构）", "kind": "停课", "date": "2027-03-02"}],
           "exams": [{"name": "线代期中（虚构）", "kind": "期中", "date": "2027-04-20", "course": "LA1", "seat": SEAT}],
           "holidays": [{"kind": "放假", "name": "清明（虚构）", "start_date": "2027-04-05"}]}
    doc.update(extra)
    return json.dumps(doc, ensure_ascii=False)


def test_json_import_creates_a_term_and_everything_in_it(admin, violetdb):
    content = json_doc()
    p = preview(admin, content, "spring.json")
    assert p["ok"] and p["new_term"] and p["counts"]["created"] == 5
    assert rows(violetdb, "schedule_terms") == 0
    done = commit(admin, content, p, "spring.json")
    week2 = ok(admin.get(f"{B}/terms/{done['term_id']}/week?week=2"))
    assert items(week2, 1) == [("线性代数（虚构）", "cancelled", "08:00")]
    exam = violetdb.fetch_one("SELECT * FROM schedule_exams")
    assert exam["course_id"] == violetdb.fetch_one("SELECT id FROM schedule_courses")["id"] and exam["seat"] == SEAT
    assert preview(admin, content, "spring.json")["counts"]["unchanged"] == 5
    # a CSV has courses only: the exam, holiday and exception from the JSON file stay
    csv_text = "课程,课程代码,星期,节次,周次\n线性代数（虚构）,LA1,2,1-2,1-16\n"
    p = preview(admin, csv_text, term_id=done["term_id"])
    assert p["counts"]["deleted"] == 0 and p["counts"]["updated"] == 0, p["items"]   # no 老师 / 地点 column: kept
    commit(admin, csv_text, p, term_id=done["term_id"])
    assert violetdb.fetch_one("SELECT teacher FROM schedule_courses")["teacher"] == TEACHER
    assert violetdb.fetch_one("SELECT location FROM schedule_slots")["location"] == ROOM
    assert violetdb.fetch_one("SELECT status FROM schedule_exams")["status"] == "active"
    assert violetdb.fetch_one("SELECT status FROM schedule_holidays")["status"] == "active"
    assert violetdb.fetch_one("SELECT status FROM schedule_exceptions")["status"] == "active"


def test_stale_preview_and_changed_file_are_refused(admin, violetdb, term):
    p = preview(admin, CSV, term_id=term)
    ok(post(admin, f"{B}/terms/{term}/courses", {"name": "后来加的（虚构）"}))          # the timetable changes
    resp = commit(admin, CSV, p, term_id=term, status=409)
    assert resp.status_code == 409 and resp.get_json()["stale"] and "重新预览" in resp.get_json()["error"]
    assert rows(violetdb, "schedule_courses") == 1 and rows(violetdb, "schedule_imports") == 0
    p = preview(admin, CSV, term_id=term)
    other = CSV.replace("1-16单", "1-15单 ")
    resp = post(admin, f"{B}/import/confirm", {"file_name": "t.csv", "content": other, "term_id": term,
                                               "sha256": p["sha256"], "data_version": p["data_version"]})
    assert resp.status_code == 409 and rows(violetdb, "schedule_courses") == 1


def test_a_failure_during_commit_rolls_everything_back(admin, violetdb, term, monkeypatch):
    from ai.schedule.store import ScheduleStore
    p = preview(admin, CSV, term_id=term)
    real = ScheduleStore.add_slot
    calls = {"n": 0}

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:                                   # two meetings and two courses are already written
            raise sqlite3.OperationalError(r"disk I/O error at C:\secret\path\violet.db")
        return real(self, *a, **kw)
    monkeypatch.setattr(ScheduleStore, "add_slot", flaky)
    def timetable():                       # everything but the import records (a failed one is kept on purpose)
        snap = {t: v for t, v in snapshot(violetdb).items() if t != "schedule_imports"}
        snap["schedule_revisions"] = [r for r in snap["schedule_revisions"] if r["entity"] != "import"]
        return snap
    before = timetable()
    resp = commit(admin, CSV, p, term_id=term, status=500)
    text = resp.get_data(as_text=True)
    assert resp.status_code == 500 and "OperationalError" in text and "secret" not in text and "violet.db" not in text
    assert timetable() == before
    failed = violetdb.fetch_all("SELECT status, error_count FROM schedule_imports")
    assert failed == [{"status": "failed", "error_count": 1}]


# ----------------------------------------------------------------------------------------------- export

def test_export_round_trip_privacy_option_and_formula_guard(admin, violetdb, term):
    p = preview(admin, CSV, term_id=term)
    commit(admin, CSV, p, term_id=term)
    cid = ok(post(admin, f"{B}/terms/{term}/courses", {"name": "=HYPERLINK(\"http://x\")（虚构）", "teacher": "@SUM(1)"}))["id"]
    ok(post(admin, f"{B}/courses/{cid}/slots", {"weekday": 5, "weeks": "1-4", "start": "08:00", "end": "09:00",
                                                "location": "+1 楼（虚构）"}))
    csv_text = admin.get(f"{B}/export?term_id={term}&format=csv").get_data(as_text=True)
    line = next(r for r in csv_text.splitlines() if "HYPERLINK" in r)
    assert line.startswith("\"'=HYPERLINK") or line.startswith("'=HYPERLINK")
    assert ",'@SUM(1)," in line and ",'+1 楼（虚构）," in line
    for row in csv_text.lstrip("\ufeff").splitlines()[1:]:
        for cell in __import__("csv").reader([row]).__next__():
            assert not cell.startswith(("=", "+", "-", "@")), row
    doc = json.loads(admin.get(f"{B}/export?term_id={term}&format=json").get_data(as_text=True))
    assert {c["name"] for c in doc["courses"]} >= {"高等数学（虚构）", "=HYPERLINK(\"http://x\")（虚构）"}
    assert set(doc) == {"format", "version", "term", "periods", "courses", "exceptions", "exams", "holidays"}
    lean = admin.get(f"{B}/export?term_id={term}&format=json&personal=0").get_data(as_text=True)
    for value in (TEACHER, ROOM, NOTE, "@SUM(1)"):
        assert value not in lean
    # re-importing the export changes nothing (the CSV's formula guard is undone on the way in)
    back = preview(admin, csv_text, "export.csv", term_id=term)
    assert back["ok"] and back["counts"]["created"] == 0 and back["counts"]["updated"] == 0, back["items"]
    other = ok(post(admin, f"{B}/terms", {"name": "别的学期（虚构）", "start_date": "2027-03-01",
                                          "end_date": "2027-06-30", "timezone": "Asia/Shanghai"}))["id"]
    ok(post(admin, f"{B}/terms/{other}/courses", {"name": "别的学期的课（虚构）"}))
    assert "别的学期的课" not in admin.get(f"{B}/export?term_id={term}&format=json").get_data(as_text=True)


def test_exported_json_with_manual_entries_reimports_without_changes(admin, violetdb, term):
    """Browser acceptance run 2: a JSON export of a term with MANUAL exams, holidays and changes, imported back into
    the same term, must not add or change anything (manual entries win; nothing is duplicated)."""
    ids = build_by_hand(admin, term)
    ok(post(admin, f"{B}/courses/{ids['math']}/exceptions", {"kind": "move", "on_date": "2026-09-23", "slot_id": ids["wed"],
                                                            "new_date": "2026-09-25", "new_start": "16:00", "new_end": "17:40"}))
    ok(post(admin, f"{B}/terms/{term}/exams", {"name": "高数期中（虚构）", "exam_date": "2026-11-11", "kind": "midterm",
                                               "course_id": ids["math"]}))
    ok(post(admin, f"{B}/terms/{term}/holidays", {"kind": "holiday", "name": "国庆（虚构）", "start_date": "2026-10-01",
                                                  "end_date": "2026-10-07"}))
    ok(post(admin, f"{B}/terms/{term}/holidays", {"kind": "makeup", "name": "国庆补课（虚构）", "start_date": "2026-10-10",
                                                  "follows_date": "2026-10-05"}))
    p = preview(admin, CSV, term_id=term)                     # imported data next to the manual data
    commit(admin, CSV, p, term_id=term)
    content = json_doc(term={"name": "2026 秋（虚构）"}, periods=[
        {"no": p["period_no"], "start": p["start_time"], "end": p["end_time"]} for p in PERIODS], courses=[{"name": "化学（虚构）", "slots": [
        {"weekday": 5, "periods": "5-6", "weeks": "1-16"}]}], exceptions=[],
        exams=[{"name": "化学期末（虚构）", "kind": "期末", "date": "2027-01-10", "course": "化学（虚构）"}],
        holidays=[{"kind": "放假", "name": "元旦（虚构）", "start_date": "2027-01-01"}])
    p = preview(admin, content, "more.json", term_id=term)
    assert p["ok"], p["errors"]
    commit(admin, content, p, "more.json", term_id=term)
    before = snapshot(violetdb)
    exported = admin.get(f"{B}/export?term_id={term}&format=json").get_data(as_text=True)
    again = preview(admin, exported, "export.json", term_id=term)
    assert again["ok"], again["errors"]
    assert (again["counts"]["created"], again["counts"]["updated"], again["counts"]["deleted"]) == (0, 0, 0), \
        [i for i in again["items"] if i["action"] not in ("unchanged", "skipped_manual")]
    commit(admin, exported, again, "export.json", term_id=term)
    after = snapshot(violetdb)
    for table in ("schedule_courses", "schedule_slots", "schedule_exceptions", "schedule_exams", "schedule_holidays"):
        assert after[table] == before[table], table


def test_json_exam_without_a_course_field_keeps_its_link(admin, violetdb):
    """Browser acceptance run 5: an exported exam whose course had become inactive came out without "course", and
    importing it back cleared the link. No "course" field = leave the link; "course": null = remove it."""
    content = json_doc()
    p = preview(admin, content, "spring.json")
    done = commit(admin, content, p, "spring.json")
    linked = violetdb.fetch_one("SELECT course_id FROM schedule_exams")["course_id"]
    assert linked is not None
    doc = json.loads(content)
    del doc["exams"][0]["course"]
    without = json.dumps(doc, ensure_ascii=False)
    p = preview(admin, without, "spring.json", term_id=done["term_id"])
    assert p["counts"]["updated"] == 0, p["items"]
    commit(admin, without, p, "spring.json", term_id=done["term_id"])
    assert violetdb.fetch_one("SELECT course_id FROM schedule_exams")["course_id"] == linked
    doc["exams"][0]["course"] = None
    unlinked = json.dumps(doc, ensure_ascii=False)
    p = preview(admin, unlinked, "spring.json", term_id=done["term_id"])
    commit(admin, unlinked, p, "spring.json", term_id=done["term_id"])
    assert violetdb.fetch_one("SELECT course_id FROM schedule_exams")["course_id"] is None


# ----------------------------------------------------------------------------------------------- deletion

def test_purge_course_and_term_clean_their_history_copies(admin, violetdb, term):
    ids = build_by_hand(admin, term)
    ok(post(admin, f"{B}/courses/{ids['math']}", {"teacher": "改过的虚构老师", "notes": "第二条虚构备注"}))
    ok(post(admin, f"{B}/slots/{ids['mon']}", {"location": "改过的虚构教室"}))
    assert "改过的虚构老师" in all_schedule_text(violetdb)
    assert post(admin, f"{B}/courses/{ids['math']}/purge").status_code == 400          # needs confirm
    gone = ok(post(admin, f"{B}/courses/{ids['math']}/purge", {"confirm": True}))["deleted"]
    assert gone["schedule_slots"] == 2 and gone["schedule_revisions"] >= 5
    text = all_schedule_text(violetdb)
    for value in (TEACHER, ROOM, NOTE, "改过的虚构老师", "改过的虚构教室", "第二条虚构备注", "高等数学（虚构）"):
        assert value not in text
    assert violetdb.fetch_one("SELECT COUNT(*) AS n FROM schedule_courses WHERE name = '物理实验（虚构）'")["n"] == 1
    note = violetdb.fetch_one("SELECT * FROM schedule_revisions WHERE before_json LIKE '%purged%'")
    assert (note["entity"], note["entity_id"], json.loads(note["before_json"])["scope"]) == ("term", term, "course")
    other = ok(post(admin, f"{B}/terms", {"name": "保留的学期（虚构）", "start_date": "2027-03-01",
                                          "end_date": "2027-06-30", "timezone": "Asia/Shanghai"}))["id"]
    ok(post(admin, f"{B}/terms/{other}/courses", {"name": "保留的课（虚构）", "teacher": "保留的虚构老师"}))
    scope = ok(admin.get(f"{B}/scope?term_id={term}"))["rows"]
    assert scope["schedule_courses"] == 1 and scope["schedule_periods"] == 6
    assert post(admin, f"{B}/terms/{term}/purge", {"confirm": True, "confirm_name": "错的名字"}).status_code == 400
    ok(post(admin, f"{B}/terms/{term}/purge", {"confirm": True, "confirm_name": "2026 秋（虚构）"}))
    text = all_schedule_text(violetdb)
    assert "物理实验（虚构）" not in text and "2026 秋（虚构）" not in text
    assert "保留的虚构老师" in text and rows(violetdb, "schedule_terms") == 1          # the other term is untouched
    notes = violetdb.fetch_all("SELECT before_json FROM schedule_revisions WHERE before_json LIKE '%purged%'")
    # the course note went with its term; one note (scope + counts, no names) is what is left of the term
    assert len(notes) == 1 and set(json.loads(notes[0]["before_json"])) == {"purged", "scope", "rows"}


def test_purge_all_needs_the_typed_confirmation_and_leaves_no_personal_copy(admin, violetdb, term):
    build_by_hand(admin, term)
    p = preview(admin, CSV, term_id=term)
    commit(admin, CSV, p, term_id=term)
    scope = ok(admin.get(f"{B}/scope"))["rows"]
    assert scope["schedule_revisions"] > 10 and scope["schedule_imports"] == 1
    for wrong in ({"confirm": True}, {"confirm": True, "confirm_text": "删除"}, {"confirm_text": "删除全部课表"}):
        assert post(admin, f"{B}/purge-all", wrong).status_code == 400
    assert rows(violetdb, "schedule_courses") > 0
    ok(post(admin, f"{B}/purge-all", {"confirm": True, "confirm_text": "删除全部课表"}))
    text = all_schedule_text(violetdb)
    for value in (TEACHER, ROOM, NOTE, "高等数学", "t.csv"):
        assert value not in text
    assert rows(violetdb, "schedule_revisions") == 1 and rows(violetdb, "schedule_imports") == 0


# ----------------------------------------------------------------------------------------------- MCP link

def test_page_changes_reach_the_mcp_tools_at_once(admin, violetdb, term, clock):
    from ai.schedule.store import ScheduleStore
    from ai.schedule.tools import ScheduleTools
    tools = ScheduleTools(ScheduleStore(violetdb))                      # built once, like the running server
    clock.set(datetime(2026, 9, 8, 9, 30, tzinfo=SH))                # Tue, week 1
    assert tools.day("tomorrow")["items"] == []
    ids = build_by_hand(admin, term)
    assert [i["course"] for i in tools.day("tomorrow")["items"]] == ["高等数学（虚构）"]
    ok(post(admin, f"{B}/courses/{ids['math']}/exceptions", {"kind": "cancel", "on_date": "2026-09-09", "slot_id": ids["wed"]}))
    assert tools.day("tomorrow")["items"][0]["status_text"] == "停课"
    ok(post(admin, f"{B}/courses/{ids['math']}/disable"))
    assert tools.find("高数")["matches"] == []
    ok(post(admin, f"{B}/terms/{term}/archive"))
    assert tools.day("tomorrow")["data_state"] == "no_data"           # nothing active -> "no timetable", not "no class"
    ok(post(admin, f"{B}/purge-all", {"confirm": True, "confirm_text": "删除全部课表"}))
    assert "不要说成没课" in tools.day("today")["state_note"]


def test_page_tells_when_eve_cannot_use_the_timetable(admin, term, monkeypatch):
    from ai.tool_manager import tool_store
    over = ok(admin.get(B))
    assert over["enabled"] is False and over["server_disabled"] is False
    monkeypatch.setenv("VIOLET_SCHEDULE_ENABLED", "1")
    tool_store().save_server("violet-schedule", {"enabled": False}, "test")
    tool_store().set_tool("violet-schedule", "find_course", False, "test")
    over = ok(admin.get(B))
    assert over["enabled"] is True and over["server_disabled"] is True and over["tools_disabled"] == ["find_course"]
    assert over["terms"][0]["state"] in ("active", "not_started", "ended")


@pytest.fixture()
def campus(make_world, clock, violetdb, env_guard):
    from wx_ilink_py import ILinkClient, create_app
    w = make_world(VIOLET_SCHEDULE_ENABLED="1")
    w.llm.responders["fake-memory"] = lambda kw: json.dumps({"should_save": False, "reason": "t"})
    w.chats = []
    flask_app = create_app(client=ILinkClient(data_dir=env_guard, auto_login=False), violetdb=violetdb,
                           proactive_scheduler=None, all_agents=[], admin_token=TOKEN)
    flask_app.testing = True
    w.admin = flask_app.test_client()
    assert w.admin.post("/auth/login", data={"token": TOKEN}, headers=SAME).status_code == 302
    return w


def test_a_whole_turn_answers_from_what_the_page_just_saved(campus):
    w = campus
    tid = ok(post(w.admin, f"{B}/terms", {"name": "2026 秋（虚构）", "start_date": "2026-09-07", "end_date": "2027-01-15",
                                          "timezone": "Asia/Shanghai"}))["id"]
    ok(w.admin.put(f"{B}/terms/{tid}/periods", json={"periods": PERIODS}, headers=SAME))
    p = preview(w.admin, CSV, term_id=tid)
    commit(w.admin, CSV, p, term_id=tid)
    w.llm.responders["fake-intent"] = wants(FIND, reason="高数下一次")
    chat_plays(w, (FIND, {"query": "高数"}),
               final=lambda kw: "下一次是 {date} {start}".format(**next(u for u in result_of(kw)["matches"][0]["upcoming"] if u["occupies"])))
    say(w, "高数下一次什么时候上？")
    assert offered(w.chats[0]) == [FIND] and w.client.sent_texts[-1] == "下一次是 2026-09-09 10:00"
    math = w.db.fetch_one("SELECT id FROM schedule_slots WHERE weekday = 3")["id"]
    course = w.db.fetch_one("SELECT course_id FROM schedule_slots WHERE id = ?", (math,))["course_id"]
    ok(post(w.admin, f"{B}/courses/{course}/exceptions", {"kind": "cancel", "on_date": "2026-09-09", "slot_id": math}))
    w.chats.clear()
    chat_plays(w, (FIND, {"query": "高数"}),
               final=lambda kw: "下一次是 {date} {start}".format(**next(u for u in result_of(kw)["matches"][0]["upcoming"] if u["occupies"])))
    say(w, "那高数下一次呢？")
    assert w.client.sent_texts[-1] == "下一次是 2026-09-14 08:00"              # no restart, no cache
    assert w.db.fetch_one("SELECT COUNT(*) AS n FROM task_sessions")["n"] == 0
    assert not w.db.fetch_all("SELECT id FROM event_memories WHERE content LIKE '%高等数学%'")
