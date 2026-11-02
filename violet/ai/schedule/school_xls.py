"""2026-11-01 (user: 课表没有标识清楚，不知道该怎么用; their school's 教务系统 export): the timetable .xls of Chinese
academic systems, read straight into the project's CSV format, so the import (preview -> confirm) stays the one path.

The layout (rows = 大节, columns = 星期一..星期日):
  row label  "第一大节\n(03,04)\n08:30-10:05"            -> the time of every class in that row
  cell       "\n数据库原理与实践\n万利永\n1-16([周])[03-04节]\nD10-404\n" -- several classes split by an empty line;
             name (more lines before the teacher are part of it, e.g. "(羽毛球)"), teacher, weeks, location (optional)
  title      "学年学期：2026-2027-1  班级：…"               -> the term's name
Weeks are written the way the timetable model reads them: "1-16", "1,3,5,…,15" (odd weeks), "1-3,5-7", "4,8,12,16".
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any

MAX_BYTES = 700 * 1024                     # fits the import request (base64) of schedule_routes.MAX_IMPORT_BODY
WEEKDAY_NAMES = "一二三四五六日"
TIME_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-~–—]\s*(\d{1,2}:\d{2})")
WEEKS_RE = re.compile(r"^\s*([\d,，\-]+)\s*(?:\(|（)?\s*\[?周")
TERM_RE = re.compile(r"学年学期[:：]\s*(\S+)")


class SchoolTableError(ValueError):
    pass


def _cells(data: bytes) -> list[list[str]]:
    if len(data) > MAX_BYTES:
        raise SchoolTableError("文件太大（最大 700 KB）")
    if not data.startswith(b"\xd0\xcf\x11\xe0"):
        if data[:2] == b"PK":
            raise SchoolTableError("这是 .xlsx：请在 Excel 里“另存为 Excel 97-2003 工作簿 (.xls)”或者从教务系统重新导出 .xls")
        raise SchoolTableError("不是 Excel（.xls）文件")
    try:
        import xlrd
    except ImportError:
        raise SchoolTableError("服务器缺少读取 .xls 的组件 xlrd：请更新 Violet（更新时会自动安装）") from None
    try:
        sheet = xlrd.open_workbook(file_contents=data).sheet_by_index(0)
    except Exception as exc:                                  # noqa: BLE001 -- a damaged / encrypted file
        raise SchoolTableError(f"读不了这个 .xls（{exc.__class__.__name__}）") from None
    return [[str(sheet.cell_value(r, c) or "") for c in range(sheet.ncols)] for r in range(sheet.nrows)]


def _classes(cell: str) -> list[dict[str, str]]:
    out = []
    for block in re.split(r"\n\s*\n", cell.strip("\n")):
        lines = [x.strip() for x in block.split("\n") if x.strip()]
        at = next((i for i, x in enumerate(lines) if WEEKS_RE.match(x)), None)
        if at is None or at < 1:
            continue
        weeks = WEEKS_RE.match(lines[at]).group(1).replace("，", ",").strip(",")
        if at == 1:                                           # no teacher line
            name, teacher = lines[0], ""
        else:
            name, teacher = lines[0] + "".join(lines[1:at - 1]), lines[at - 1]
        out.append({"course": name, "teacher": teacher, "weeks": weeks,
                    "location": lines[at + 1] if at + 1 < len(lines) else ""})
    return out


def convert(data: bytes) -> dict[str, Any]:
    """{"csv": text in the project's CSV format, "term": the term name in the title or None, "count": classes}."""
    rows = _cells(data)
    head = next((r for r, row in enumerate(rows) if sum("星期" in c for c in row) >= 5), None)
    if head is None:
        raise SchoolTableError("没找到“星期一 … 星期日”那一行：这不像教务系统导出的课表")
    days = {}
    for c, text in enumerate(rows[head]):
        text = text.strip()
        if text.startswith("星期") and text[-1:] in WEEKDAY_NAMES:
            days[c] = WEEKDAY_NAMES.index(text[-1]) + 1
    term = None
    for row in rows[:head]:
        match = TERM_RE.search(" ".join(row))
        if match:
            term = match.group(1)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["课程", "老师", "星期", "开始", "结束", "周次", "地点"])
    count, untimed = 0, 0
    for row in rows[head + 1:]:
        times = TIME_RE.search(row[0] if row else "")
        for c, day in days.items():
            if c >= len(row) or not row[c].strip():
                continue
            found = _classes(row[c])
            if found and not times:
                untimed += len(found)
                continue
            for item in found:
                writer.writerow([item["course"], item["teacher"], f"周{WEEKDAY_NAMES[day - 1]}", times.group(1).zfill(5),
                                 times.group(2).zfill(5), item["weeks"], item["location"]])
                count += 1
    if not count:
        raise SchoolTableError("表里没有认出任何一节课（每节课要有课名、老师、周次，比如“1-16([周])”）")
    return {"csv": out.getvalue(), "term": term, "count": count, "untimed": untimed}
