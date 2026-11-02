"""2026-11-01 (user: 课表没有标识清楚，不知道该怎么用): the .xls a school's 教务系统 exports, imported directly."""
from __future__ import annotations

import base64

import pytest

from tests.test_control_center_p90 import BEARER, JSON, SAME, TOKEN, admin, api, app, env_guard, made  # noqa: F401
from tests.test_schedule_admin_p943 import B, ok, post  # noqa: F401

ROWS = [
    ["课表", "", "", "", "", "", "", ""],
    ["学年学期：2026-2027-1        班级：B250202", "", "", "", "", "", "", ""],
    ["", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"],
    ["早读\n(01,02)\n07:00-08:00", "", "", "", "", "", "", ""],
    ["第一大节\n(03,04)\n08:30-10:05", "\n数据库原理与实践\n万老师\n1-16([周])[03-04节]\nD10-404\n",
     "\nLinux操作系统\n李老师\n1-8([周])[03-04节]\nD10-403\n\nLinux操作系统\n李老师\n9-16([周])[03-04节]\nD10-404\n",
     "", "\n大学英语Ⅲ\n朱老师\n1-3,5-7,9-11,13-15([周])[03-04节]\nD10-304\n\n大学英语Ⅲ\n朱老师\n4,8,12,16([周])[03-04节]\nD10-305\n",
     "", "", ""],
    ["第二大节\n(05,06)\n10:25-12:00", "\n马克思主义基本原理\n姜老师\n1,3,5,7,9,11,13,15([周])[05-06节]\nD10-504\n",
     "", "", "", "", "", ""],
    ["第四大节\n(09,10)\n15:55-17:30", "", "", "", "\n大学体育Ⅲ\n(羽毛球)\n蒙老师\n1-16([周])[09-10节]\n", "", "", ""],
    ["：大学体育Ⅲ 蒙老师 1-16周;", "", "", "", "", "", "", ""],
]


@pytest.fixture()
def table(monkeypatch):
    from ai.schedule import school_xls
    monkeypatch.setattr(school_xls, "_cells", lambda data: ROWS)
    return school_xls


def test_the_school_table_becomes_the_csv_format(table):
    from ai.schedule.importer import parse_csv
    from ai.schedule.model import parse_weeks
    out = table.convert(b"\xd0\xcf\x11\xe0")
    assert out["term"] == "2026-2027-1" and out["count"] == 7 and not out["untimed"]
    parsed = parse_csv(out["csv"], "课表.csv", {"name": "2026-2027-1"})
    assert not parsed.errors
    courses = {c["name"]: c for c in parsed.normalized["courses"]}
    assert set(courses) == {"数据库原理与实践", "Linux操作系统", "大学英语Ⅲ", "马克思主义基本原理", "大学体育Ⅲ(羽毛球)"}
    marx = courses["马克思主义基本原理"]["slots"][0]
    assert marx["weekday"] == 1 and marx["start"] == "10:25" and marx["weeks"] == "1-15单"
    assert parse_weeks(marx["weeks"]).runs_in(9) and not parse_weeks(marx["weeks"]).runs_in(10)
    linux = sorted(courses["Linux操作系统"]["slots"], key=lambda s: s["weeks"])
    assert [(s["weeks"], s["location"]) for s in linux] == [("1-8", "D10-403"), ("9-16", "D10-404")]
    english = {s["location"]: parse_weeks(s["weeks"]) for s in courses["大学英语Ⅲ"]["slots"]}
    assert english["D10-305"].weeks() == [4, 8, 12, 16] and 4 not in english["D10-304"].weeks()
    assert courses["大学体育Ⅲ(羽毛球)"]["teacher"] == "蒙老师"


def test_files_that_are_not_a_school_table(table, monkeypatch):
    from ai.schedule import school_xls
    monkeypatch.setattr(school_xls, "_cells", lambda data: [["随便什么"], ["没有星期"]])
    with pytest.raises(school_xls.SchoolTableError, match="星期一"):
        school_xls.convert(b"x")
    monkeypatch.undo()
    with pytest.raises(school_xls.SchoolTableError, match="xlsx"):
        school_xls.convert(b"PK\x03\x04rest")
    with pytest.raises(school_xls.SchoolTableError, match="不是 Excel"):
        school_xls.convert(b"hello")


def test_the_page_imports_a_school_table(admin, table):
    tid = ok(post(admin, f"{B}/terms", {"name": "2026-2027-1", "start_date": "2026-08-31", "end_date": "2027-01-17",
                                       "timezone": "Asia/Shanghai"}))["id"]
    upload = {"file_name": "课表.xls", "data_base64": "data:application/vnd.ms-excel;base64,"
              + base64.b64encode(b"\xd0\xcf\x11\xe0fake").decode(), "term_id": tid, "mode": "merge"}
    p = ok(post(admin, f"{B}/import/preview", upload))
    assert p["ok"] and p["counts"]["created"] > 0 and p["format"] == "csv", p
    r = ok(post(admin, f"{B}/import/confirm", {**upload, "sha256": p["sha256"], "data_version": p["data_version"],
                                               "expected_counts": p["counts"]}))
    assert r["created"] == p["counts"]["created"]
    no_term = post(admin, f"{B}/import/preview", {**upload, "term_id": None}).get_json()
    assert not no_term["ok"] and "2026-2027-1" in str(no_term["problems"])


def test_the_guide_and_the_week_field_are_on_the_page():
    import os
    root = os.path.join(os.path.dirname(__file__), "..", "wx_ilink_py")
    html = open(os.path.join(root, "templates", "control.html"), encoding="utf-8").read()
    js = open(os.path.join(root, "static", "control_schedule.js"), encoding="utf-8").read()
    assert "四步录好课表" in html and ".xls" in html and "今天是第几周" in js and "data_base64" in js
    assert "xlrd==" in open(os.path.join(root, "..", "requirements.txt"), encoding="utf-8").read()
