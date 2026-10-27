"""Phase 9.4 closeout: CSV import is "追加 / 更新" (merge) by default; "整学期替换" (replace) only when chosen.

Root cause of the open pre-release item: ScheduleStore._import_courses deleted every imported course of the term whose
key was not in THIS file, so a CSV with only 物理 marked 数学 and 英语 (imported from another file) deleted.

Through the Control Center API (Flask test client, admin session), fictional data, an isolated SQLite database."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from tests.test_control_center_p90 import BEARER, JSON, SAME, TOKEN, admin, api, app, env_guard, made  # noqa: F401
from tests.test_schedule_admin_p943 import B, PERIODS, SH, commit, ok, post, preview, rows, snapshot, term  # noqa: F401
from tests.test_schedule_mcp_p942 import clock  # noqa: F401

HEAD = "课程,课程代码,类型,老师,星期,节次,开始,结束,周次,地点,备注\n"
MATH = "高等数学（虚构）,MATH1,理论,虚构老师甲,1,1-2,,,1-16,虚构楼A101,\n"
ENGLISH = "大学英语（虚构）,ENG1,理论,虚构老师乙,1,3-4,,,1-16,虚构楼B202,\n"
PHYSICS = "大学物理（虚构）,PHY1,理论,虚构老师丙,2,,14:00,15:40,1-16,虚构楼C303,\n"
A, B_FILE = HEAD + MATH + ENGLISH, HEAD + PHYSICS


def names(*items):
    return sorted(items)


def live_courses(violetdb, term_id):
    return sorted(r["name"] for r in violetdb.fetch_all(
        "SELECT name FROM schedule_courses WHERE term_id = ? AND status = 'active'", (term_id,)))


def do(admin, content, term_id, mode=None, name="t.csv"):
    p = preview(admin, content, name=name, term_id=term_id, mode=mode)
    return p, commit(admin, content, p, name=name, term_id=term_id, mode=p["mode"] if p["mode"] != "json" else None,
                     expected=p["counts"])


def test_default_merge_keeps_courses_from_other_files(admin, violetdb, term):
    p, _ = do(admin, A, term)
    assert p["mode"] == "merge"
    p_b, done = do(admin, B_FILE, term)
    assert p_b["counts"]["deleted"] == 0 and p_b["deleted_courses"] == []
    assert (done["created"], done["deleted"], done["mode"]) == (2, 0, "merge")              # 1 course + 1 meeting
    assert live_courses(violetdb, term) == names("大学英语（虚构）", "大学物理（虚构）", "高等数学（虚构）")


def test_importing_the_same_file_again_changes_nothing_and_never_duplicates(admin, violetdb, term):
    do(admin, A, term)
    do(admin, B_FILE, term)
    counts = {t: rows(violetdb, t) for t in ("schedule_courses", "schedule_slots", "schedule_exams")}
    again = preview(admin, B_FILE, term_id=term)
    assert again["counts"] == {"created": 0, "updated": 0, "deleted": 0, "unchanged": 2, "skipped_manual": 0}
    commit(admin, B_FILE, again, term_id=term, mode="merge", expected=again["counts"])
    do(admin, A, term)
    assert {t: rows(violetdb, t) for t in counts} == counts                                  # no extra course / meeting / exam


def test_a_manual_change_survives_a_merge_reimport(admin, violetdb, term):
    do(admin, A, term)
    do(admin, B_FILE, term)
    math = violetdb.fetch_one("SELECT id FROM schedule_courses WHERE name = '高等数学（虚构）'")["id"]
    ok(post(admin, f"{B}/courses/{math}", {"teacher": "手动改的老师（虚构）"}))
    p = preview(admin, A, term_id=term)
    assert [(i["entity"], i["label"]) for i in p["items"] if i["action"] == "skipped_manual"] == [
        ("course", "高等数学（虚构）")]
    commit(admin, A, p, term_id=term, mode="merge", expected=p["counts"])
    assert violetdb.fetch_one("SELECT teacher FROM schedule_courses WHERE id = ?", (math,))["teacher"] == "手动改的老师（虚构）"
    assert live_courses(violetdb, term) == names("大学英语（虚构）", "大学物理（虚构）", "高等数学（虚构）")


def test_merge_moves_a_meeting_of_a_listed_course_without_a_duplicate(admin, violetdb, term):
    do(admin, A, term)
    moved = A.replace("高等数学（虚构）,MATH1,理论,虚构老师甲,1,1-2", "高等数学（虚构）,MATH1,理论,虚构老师甲,3,1-2")
    p, done = do(admin, moved, term)
    assert (done["created"], done["deleted"]) == (1, 1)                                  # the meeting, not the course
    math = violetdb.fetch_one("SELECT id FROM schedule_courses WHERE name = '高等数学（虚构）'")["id"]
    assert [r["weekday"] for r in violetdb.fetch_all("SELECT weekday FROM schedule_slots WHERE course_id = ? AND "
                                                  "status = 'active'", (math,))] == [3]
    assert live_courses(violetdb, term) == names("大学英语（虚构）", "高等数学（虚构）")


def test_replace_previews_the_deletions_and_cancel_changes_nothing(admin, violetdb, term):
    do(admin, A, term)
    do(admin, B_FILE, term)
    before = snapshot(violetdb)
    p = preview(admin, B_FILE, term_id=term, mode="replace")
    assert p["mode"] == "replace" and sorted(p["deleted_courses"]) == names("大学英语（虚构）", "高等数学（虚构）")
    assert p["counts"]["deleted"] == 2                                                    # 2 courses (their meetings go with them)
    assert snapshot(violetdb) == before                                                      # preview: nothing kept
    # the user cancels: no confirm request is sent
    assert snapshot(violetdb) == before and live_courses(violetdb, term) == names("大学英语（虚构）", "大学物理（虚构）", "高等数学（虚构）")


def test_replace_after_confirm_only_touches_its_term_and_keeps_manual_rows(admin, violetdb, term):
    other = ok(post(admin, f"{B}/terms", {"name": "2027 春（虚构）", "start_date": "2027-02-22", "end_date": "2027-06-30",
                                         "timezone": "Asia/Shanghai"}))["id"]
    assert ok(admin.put(f"{B}/terms/{other}/periods", json={"periods": PERIODS}, headers=SAME))["periods"] == 6
    do(admin, A, other)                                                                   # another term, same courses
    do(admin, A, term)
    do(admin, B_FILE, term)
    hand = ok(post(admin, f"{B}/terms/{term}/courses", {"name": "手动录入的课（虚构）", "kind": "lecture"}))["id"]
    math = violetdb.fetch_one("SELECT id FROM schedule_courses WHERE term_id = ? AND name = '高等数学（虚构）'", (term,))["id"]
    ok(post(admin, f"{B}/courses/{math}", {"teacher": "手动改的老师（虚构）"}))
    p = preview(admin, B_FILE, term_id=term, mode="replace")
    assert p["deleted_courses"] == ["大学英语（虚构）"]                                     # manual ones are kept
    done = commit(admin, B_FILE, p, term_id=term, mode="replace", expected=p["counts"])
    assert done["mode"] == "replace"
    assert live_courses(violetdb, term) == names("大学物理（虚构）", "手动录入的课（虚构）", "高等数学（虚构）")
    assert live_courses(violetdb, other) == names("大学英语（虚构）", "高等数学（虚构）")                 # the other term: untouched
    assert violetdb.fetch_one("SELECT status FROM schedule_courses WHERE id = ?", (hand,))["status"] == "active"


def test_a_confirm_must_match_its_preview(admin, violetdb, term):
    do(admin, A, term)
    before = snapshot(violetdb)
    merged = preview(admin, B_FILE, term_id=term)                                          # previewed as merge ...
    resp = commit(admin, B_FILE, merged, term_id=term, mode="replace", expected=merged["counts"], status=409)
    assert resp.status_code == 409 and resp.get_json()["stale"] is True                   # ... confirmed as replace
    assert snapshot(violetdb) == before
    replace = preview(admin, B_FILE, term_id=term, mode="replace")
    resp = commit(admin, B_FILE, replace, term_id=term, mode="replace", status=409)          # replace without the counts
    assert resp.status_code == 409 and snapshot(violetdb) == before
    assert rows(violetdb, "schedule_imports") == 1                                           # refused confirms leave nothing


def test_a_change_after_the_preview_is_refused_with_409(admin, violetdb, term):
    do(admin, A, term)
    p = preview(admin, B_FILE, term_id=term, mode="replace")
    math = violetdb.fetch_one("SELECT id FROM schedule_courses WHERE name = '高等数学（虚构）'")["id"]
    ok(post(admin, f"{B}/courses/{math}", {"teacher": "预览之后改的（虚构）"}))
    after_edit = snapshot(violetdb)
    resp = commit(admin, B_FILE, p, term_id=term, mode="replace", expected=p["counts"], status=409)
    assert resp.status_code == 409 and resp.get_json()["stale"] is True
    assert snapshot(violetdb) == after_edit


def test_old_clients_and_bad_modes(admin, violetdb, term):
    p = preview(admin, A, term_id=term)                                                   # no mode at all: merge
    done = commit(admin, A, p, term_id=term)                                              # old confirm: no mode, no counts
    assert done["mode"] == "merge"
    p_b = preview(admin, B_FILE, term_id=term)
    commit(admin, B_FILE, p_b, term_id=term)
    assert live_courses(violetdb, term) == names("大学英语（虚构）", "大学物理（虚构）", "高等数学（虚构）")   # old clients are safe too
    bad = post(admin, f"{B}/import/preview", {"file_name": "t.csv", "content": A, "term_id": term, "mode": "wipe"})
    assert bad.status_code == 400 and "mode" in bad.get_json()["problems"]
    bad = post(admin, f"{B}/import/preview", {"file_name": "t.json", "content": "{}", "term_id": term, "mode": "merge"})
    assert bad.status_code == 400 and "mode" in bad.get_json()["problems"]


def test_json_keeps_its_section_replace_semantics(admin, violetdb, term):
    do(admin, A, term)
    one = json.dumps({"format": "violet-schedule", "version": 1, "courses": [
        {"name": "高等数学（虚构）", "code": "MATH1", "kind": "lecture", "teacher": "虚构老师甲",
         "slots": [{"weekday": 1, "periods": "1-2", "weeks": "1-16", "location": "虚构楼A101"}]}]}, ensure_ascii=False)
    p = preview(admin, one, name="t.json", term_id=term)
    assert p["mode"] == "json" and p["deleted_courses"] == ["大学英语（虚构）"]             # a "courses" section is the list
    no_courses = json.dumps({"format": "violet-schedule", "version": 1, "exams": []})
    assert preview(admin, no_courses, name="t.json", term_id=term)["deleted_courses"] == []  # no section: left alone


def test_csv_export_reimports_as_unchanged(admin, violetdb, term):
    do(admin, A, term)
    do(admin, B_FILE, term)
    exported = admin.get(f"{B}/export?term_id={term}&format=csv&personal=1").get_data(as_text=True)
    for mode in (None, "replace"):
        again = preview(admin, exported, name="export.csv", term_id=term, mode=mode)
        assert (again["counts"]["created"], again["counts"]["updated"], again["counts"]["deleted"]) == (0, 0, 0), \
            [i for i in again["items"] if i["action"] != "unchanged"]


def test_the_mcp_tools_see_a_merge_import_at_once(admin, violetdb, term, clock):
    from ai.schedule.store import ScheduleStore
    from ai.schedule.tools import ScheduleTools
    tools = ScheduleTools(ScheduleStore(violetdb))                                           # built once, like the server
    clock.set(datetime(2026, 9, 7, 9, 0, tzinfo=SH))                                      # Mon, week 1
    do(admin, A, term)
    assert tools.find("物理")["matches"] == []
    do(admin, B_FILE, term)
    assert [m["course"] for m in tools.find("物理")["matches"]] == ["大学物理（虚构）"]
    assert [m["course"] for m in tools.find("高数")["matches"]] == ["高等数学（虚构）"]        # still there
    assert [i["course"] for i in tools.day("tomorrow")["items"]] == ["大学物理（虚构）"]
