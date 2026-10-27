"""Phase 9.4.3: timetable import from the project's CSV / JSON formats (schedule_templates/README.md).

upload -> parse (format errors, every line) -> validate (a DRY RUN of ScheduleStore.apply_import inside a
transaction that is always rolled back: the same code as the real import, every bad item reported, nothing kept,
nothing logged as done) -> preview (created / updated / deleted / unchanged / kept-because-manual, conflicts) ->
the user confirms -> commit: the same file again (its SHA-256 must match the preview), checked against the data
version the preview was computed on, recorded and applied in ONE transaction. Anything failing rolls it all back
and leaves only a "failed" import record.

The file arrives as text in the request body; no path is ever opened. Limits: LIMITS below.

Identity (the same in every file, so importing a file twice changes nothing and two files describe the same
course the same way):
* term: its name;
* course: the 课程编号 / "key" column if given, else "code:<课程代码>", else "name:<课名>" (NFKC, no spaces, case-folded);
* meeting (slot): its "key" if given, else "<星期>|<节次 p1-2 or 08:00-09:40>|<周次, canonical>" within the course;
* exception: "<kind>|<course>|<slot>|<date>"; exam: "<name>|<date>"; holiday: "<kind>|<start date>".
A changed location / teacher is an update; a changed weekday, time or weeks is a different meeting (the old one is
removed, the new one added). Rows the user changed by hand (manual_override) and courses entered by hand under the
same name are never overwritten -- the preview lists them.

Modes (phase 9.4 closeout, see CSV_MODES): a CSV is "merge" (追加 / 更新) unless the user chooses "replace"
(整学期替换); a JSON file replaces the sections it has. The confirm must come out exactly as previewed.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ai.schedule.model import ScheduleValidationError, parse_date, parse_time, parse_weeks
from ai.schedule.store import ScheduleStore, name_key, quiet

LIMITS = {"max_bytes": 256 * 1024, "max_csv_rows": 1000, "max_cell_chars": 500, "max_json_depth": 6,
          "max_courses": 200, "max_slots_per_course": 30, "max_exceptions": 500, "max_exams": 200,
          "max_holidays": 100, "max_periods": 20, "max_file_name": 200, "max_errors": 200}
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

CSV_COLUMNS = {
    "course": ("course", "课程", "课程名", "课名"),
    "course_key": ("course_key", "课程编号"),
    "code": ("code", "课程代码"),
    "kind": ("kind", "类型", "课程类型"),
    "teacher": ("teacher", "老师", "教师"),
    "weekday": ("weekday", "星期"),
    "periods": ("periods", "节次"),
    "start": ("start", "开始", "开始时间"),
    "end": ("end", "结束", "结束时间"),
    "weeks": ("weeks", "周次"),
    "location": ("location", "地点", "教室"),
    "notes": ("notes", "备注"),
    "key": ("key", "编号", "上课时间编号"),
}
CSV_REQUIRED = ("course", "weekday", "weeks")
KINDS = {"lecture": "lecture", "理论": "lecture", "讲授": "lecture", "lab": "lab", "实验": "lab", "seminar": "seminar",
         "研讨": "seminar", "讨论": "seminar", "pe": "pe", "体育": "pe", "other": "other", "其他": "other"}
EXAM_KINDS = {"final": "final", "期末": "final", "midterm": "midterm", "期中": "midterm", "quiz": "quiz", "小测": "quiz",
              "测验": "quiz", "other": "other", "其他": "other"}
HOLIDAY_KINDS = {"holiday": "holiday", "放假": "holiday", "假期": "holiday", "no_class": "no_class", "停课": "no_class",
                 "停课日": "no_class", "makeup": "makeup", "调休": "makeup", "补课": "makeup", "补课日": "makeup"}
EXCEPTION_KINDS = {"cancel": "cancel", "停课": "cancel", "move": "move", "调课": "move", "time": "time",
                   "改时间": "time", "room": "room", "换教室": "room", "extra": "extra", "加课": "extra"}
WEEKDAYS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7, "mon": 1, "tue": 2, "wed": 3,
            "thu": 4, "fri": 5, "sat": 6, "sun": 7}
FIELD_LABELS = {"name": "名称", "kind": "类型", "code": "课程代码", "teacher": "老师", "notes": "备注", "weekday": "星期",
                "weeks": "周次", "start": "开始", "end": "结束", "period_start": "开始节次", "period_end": "结束节次",
                "time": "时间", "location": "地点", "valid_from": "生效日期", "valid_until": "失效日期",
                "start_date": "开始日期", "end_date": "结束日期", "week1_monday": "第一周周一", "timezone": "时区",
                "exam_date": "考试日期", "seat": "座位号", "on_date": "日期", "new_date": "新日期",
                "new_start": "新开始", "new_end": "新结束", "new_period_start": "新开始节次",
                "new_period_end": "新结束节次", "new_location": "新地点", "reason": "原因", "slot_id": "上课时间",
                "slot": "上课时间", "course": "课程", "follows_date": "按哪天的课表", "course_id": "课程", "row": "这一行"}


@dataclass
class Parsed:
    fmt: str
    file_name: str
    sha256: str
    normalized: dict[str, Any] | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    lines: dict[str, Any] = field(default_factory=dict)       # item label -> CSV line / JSON path
    rows: int = 0

    def error(self, message: str, *, line: int | None = None, where: str | None = None,
              field_name: str | None = None) -> None:
        if len(self.errors) < LIMITS["max_errors"]:
            self.errors.append({"line": line, "where": where, "field": FIELD_LABELS.get(field_name or "", field_name),
                                "message": message})

    def warn(self, message: str, **where) -> None:
        self.warnings.append({**where, "message": message})


# ----------------------------------------------------------------------------------------------- small parsers

def clean_file_name(name: Any) -> str:
    """Only a label: the last path component, no control characters, at most 200 characters."""
    text = re.split(r"[\\/]", str(name or ""))[-1]
    text = re.sub(r"[\x00-\x1f\x7f]", "", text).strip()
    return text[:LIMITS["max_file_name"]] or "upload"


def unescape_cell(value: str) -> str:
    """Undo the export's formula guard ('=x -> =x)."""
    if len(value) > 1 and value[0] == "'" and value[1] in FORMULA_START:
        return value[1:]
    return value


def parse_weekday(value: Any) -> int:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    if text.isdigit() and 1 <= int(text) <= 7:
        return int(text)
    text = re.sub(r"^(周|星期|礼拜)", "", text)
    for name, number in WEEKDAYS.items():
        if text == name or (len(name) == 3 and text.startswith(name)):
            return number
    raise ValueError("星期要写 1–7 或 周一…周日")


def parse_periods(value: Any) -> tuple[int, int]:
    text = re.sub(r"[第节\s]", "", unicodedata.normalize("NFKC", str(value or ""))).replace("~", "-").replace("–", "-")
    match = re.fullmatch(r"(\d{1,2})(?:-(\d{1,2}))?", text)
    if not match:
        raise ValueError("节次要写成 1-2 或 3")
    a, b = int(match.group(1)), int(match.group(2) or match.group(1))
    if not (1 <= a <= 20 and 1 <= b <= 20) or b < a:
        raise ValueError("节次要在 1–20 之间、从小到大")
    return a, b


def _alias(table: dict[str, str], value: Any, default: str | None, what: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        if default is None:
            raise ValueError(f"{what}不能为空")
        return default
    if text not in table:
        raise ValueError(f"{what}不认识（可以写 {' / '.join(sorted({k for k in table if not k.isascii()}))}）")
    return table[text]


def course_key(name: Any, code: Any = None, explicit: Any = None) -> str:
    if str(explicit or "").strip():
        return str(explicit).strip()[:120]
    if str(code or "").strip():
        return "code:" + name_key(code)[:110]
    return "name:" + name_key(name)[:110]


def slot_key(weekday: int, start: str | None, end: str | None, periods: tuple[int, int] | None, weeks: str) -> str:
    when = f"p{periods[0]}-{periods[1]}" if periods else f"{start}-{end}"
    return f"{weekday}|{when}|{weeks}"[:120]


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _slot_fields(parsed: Parsed, raw: dict[str, Any], label: dict[str, Any]) -> dict[str, Any] | None:
    """weekday / weeks / time-or-periods / location of one meeting (CSV row or JSON slot); None when invalid."""
    ok = True
    out: dict[str, Any] = {}
    try:
        out["weekday"] = parse_weekday(raw.get("weekday"))
    except ValueError as exc:
        parsed.error(str(exc), field_name="weekday", **label)
        ok = False
    try:
        out["weeks"] = parse_weeks(raw.get("weeks")).text()
    except ValueError as exc:
        parsed.error(str(exc), field_name="weeks", **label)
        ok = False
    periods = raw.get("periods")
    if periods in (None, "") and (raw.get("period_start") not in (None, "") or raw.get("period_end") not in (None, "")):
        periods = f"{raw.get('period_start')}-{raw.get('period_end') or raw.get('period_start')}"
    has_time = raw.get("start") not in (None, "") or raw.get("end") not in (None, "")
    has_periods = periods not in (None, "")
    out["periods"] = None
    if has_time == has_periods:
        parsed.error("节次和开始 / 结束时间只写一种（必须写一种）", field_name="time", **label)
        ok = False
    elif has_periods:
        try:
            out["periods"] = parse_periods(periods)
        except ValueError as exc:
            parsed.error(str(exc), field_name="period_start", **label)
            ok = False
    else:
        for name in ("start", "end"):
            try:
                out[name] = parse_time(raw.get(name)).strftime("%H:%M")
            except ValueError as exc:
                parsed.error(str(exc), field_name=name, **label)
                ok = False
        if ok and out["end"] <= out["start"]:
            parsed.error("结束要晚于开始（不支持跨午夜的课）", field_name="end", **label)
            ok = False
    for name in ("location", "valid_from", "valid_until"):
        value = raw.get(name)
        out[name] = str(value).strip() if value not in (None, "") else None
    return out if ok else None


def _normalized_slot(fields: dict[str, Any], key: str) -> dict[str, Any]:
    periods = fields["periods"]
    return {"key": key, "weekday": fields["weekday"], "weeks": fields["weeks"],
            "start": None if periods else fields.get("start"), "end": None if periods else fields.get("end"),
            "period_start": periods[0] if periods else None, "period_end": periods[1] if periods else None,
            "location": fields.get("location"), "valid_from": fields.get("valid_from"),
            "valid_until": fields.get("valid_until")}


# ----------------------------------------------------------------------------------------------- CSV

def parse_csv(content: str, file_name: str, term: dict[str, Any] | None) -> Parsed:
    parsed = Parsed("csv", clean_file_name(file_name), _sha(content))
    if not _size_ok(parsed, content):
        return parsed
    if term is None:
        parsed.error("CSV 只有课程和上课时间：请先选择要导入到哪个学期")
        return parsed
    text = content.lstrip("﻿")
    reader = csv.reader(io.StringIO(text, newline=""))
    header: dict[str, int] | None = None
    courses: dict[str, dict[str, Any]] = {}
    origin: dict[str, dict[str, int]] = {}          # course key -> field -> line that set it
    try:
        for row in reader:
            line = reader.line_num
            cells = [unescape_cell(c.strip()) for c in row]
            if not any(cells) or cells[0].startswith("#"):
                continue
            if header is None:
                header = _csv_header(parsed, cells, line)
                if header is None:
                    return parsed
                continue
            parsed.rows += 1
            if parsed.rows > LIMITS["max_csv_rows"]:
                parsed.error(f"最多 {LIMITS['max_csv_rows']} 行", line=line)
                return parsed
            values = {name: (cells[i] if i < len(cells) else "") for name, i in header.items()}
            too_long = [n for n, v in values.items() if len(v) > LIMITS["max_cell_chars"]]
            if too_long:
                parsed.error(f"内容太长（每格最多 {LIMITS['max_cell_chars']} 字）", line=line, field_name=too_long[0])
                continue
            _csv_row(parsed, values, line, courses, origin)
    except csv.Error as exc:
        parsed.error(f"CSV 格式错误：{str(exc)[:80]}", line=reader.line_num)
        return parsed
    if header is None:
        parsed.error("文件是空的（第一行要是列名）")
    elif not courses and not parsed.errors:
        parsed.error("文件里没有任何课程")
    if not parsed.errors:
        parsed.normalized = {"term": {"name": term["name"]}, "periods": None, "courses": list(courses.values()),
                             "exceptions": None, "exams": None, "holidays": None}     # a CSV only has courses
    return parsed


def _csv_header(parsed: Parsed, cells: list[str], line: int) -> dict[str, int] | None:
    lookup = {alias.lower(): name for name, aliases in CSV_COLUMNS.items() for alias in aliases}
    header: dict[str, int] = {}
    for index, cell in enumerate(cells):
        name = lookup.get(cell.strip().lower())
        if name is None:
            if cell.strip():
                parsed.warn(f"不认识的列“{cell[:20]}”，已忽略", line=line)
            continue
        if name in header:
            parsed.error(f"列“{cell[:20]}”重复", line=line)
            return None
        header[name] = index
    missing = [CSV_COLUMNS[n][1] for n in CSV_REQUIRED if n not in header]
    if missing:
        parsed.error("缺少必填的列：" + "、".join(missing), line=line)
        return None
    if "periods" not in header and not ("start" in header and "end" in header):
        parsed.error("要有“节次”列，或者“开始”“结束”两列", line=line)
        return None
    return header


def _csv_row(parsed: Parsed, values: dict[str, str], line: int, courses: dict[str, dict[str, Any]],
             origin: dict[str, dict[str, int]]) -> None:
    label = {"line": line}
    name = values.get("course", "")
    if not name:
        parsed.error("课程名不能为空", field_name="name", **label)
        return
    try:
        kind = _alias(KINDS, values.get("kind"), "lecture", "类型")
    except ValueError as exc:
        parsed.error(str(exc), field_name="kind", **label)
        return
    fields = _slot_fields(parsed, values, label)
    if fields is None:
        return
    key = course_key(name, values.get("code"), values.get("course_key"))
    course = courses.get(key)
    given = {"name": name, "code": values.get("code") or None, "teacher": values.get("teacher") or None,
             "notes": values.get("notes") or None, "kind": kind if values.get("kind") else None}
    if course is None:
        # a column the file does not have leaves that field as it is (it is not cleared)
        course = courses[key] = {"key": key, "name": name, "slots": [],
                                 **{k: (kind if k == "kind" else given[k]) for k in ("kind", "code", "teacher", "notes")
                                    if k in values}}
        origin[key] = {k: line for k, v in given.items() if v}
        parsed.lines[f"courses[{key}]"] = line
    else:
        for attr, value in given.items():
            if not value:
                continue
            if attr == "name" and name_key(value) == name_key(course["name"]):
                continue
            if course.get(attr) and course[attr] != value:
                parsed.error(f"和第 {origin[key].get(attr, parsed.lines[f'courses[{key}]'])} 行同一门课的"
                             f"{FIELD_LABELS[attr]}不一致", field_name=attr, **label)
                return
            course[attr] = value
            origin[key].setdefault(attr, line)
    if len(course["slots"]) >= LIMITS["max_slots_per_course"]:
        parsed.error(f"一门课最多 {LIMITS['max_slots_per_course']} 个上课时间", **label)
        return
    skey = (values.get("key") or "").strip()[:120] or slot_key(fields["weekday"], fields.get("start"),
                                                                fields.get("end"), fields["periods"], fields["weeks"])
    where = f"courses[{key}].slots[{skey}]"
    if where in parsed.lines:
        parsed.error(f"和第 {parsed.lines[where]} 行是同一个上课时间（重复）", **label)
        return
    parsed.lines[where] = line
    slot = _normalized_slot(fields, skey)
    for name in ("valid_from", "valid_until") + (() if "location" in values else ("location",)):
        slot.pop(name)                                # not in this CSV: left as it is
    course["slots"].append(slot)


# ----------------------------------------------------------------------------------------------- JSON

def _depth(value: Any, limit: int, level: int = 1) -> bool:
    if level > limit:
        return False
    if isinstance(value, dict):
        return all(_depth(v, limit, level + 1) for v in value.values())
    if isinstance(value, list):
        return all(_depth(v, limit, level + 1) for v in value)
    return True


def _scalar(parsed: Parsed, obj: dict[str, Any], name: str, where: str, *aliases: str) -> Any:
    for key in (name, *aliases):
        if key in obj:
            value = obj[key]
            if value is None or isinstance(value, bool):
                return None if value is None else value
            if isinstance(value, (int, float)):
                return value
            if not isinstance(value, str):
                parsed.error("要是文字或数字", where=where, field_name=name)
                return None
            value = value.strip()
            if len(value) > LIMITS["max_cell_chars"]:
                parsed.error(f"内容太长（最多 {LIMITS['max_cell_chars']} 字）", where=where, field_name=name)
                return None
            return value or None
    return None


def _list(parsed: Parsed, data: dict[str, Any], name: str, limit: int, required: bool = False) -> list | None:
    value = data.get(name)
    if value is None:
        if required:
            parsed.error(f"缺少 {name}", where=name)
        return None
    if not isinstance(value, list):
        parsed.error("要是列表", where=name)
        return None
    if len(value) > limit:
        parsed.error(f"最多 {limit} 条", where=name)
        return None
    return value


def parse_json(content: str, file_name: str, term: dict[str, Any] | None) -> Parsed:
    parsed = Parsed("json", clean_file_name(file_name), _sha(content))
    if not _size_ok(parsed, content):
        return parsed
    try:
        data = json.loads(content.lstrip("﻿"))
    except json.JSONDecodeError as exc:
        parsed.error(f"JSON 格式错误（第 {exc.lineno} 行第 {exc.colno} 列）", line=exc.lineno)
        return parsed
    except RecursionError:
        parsed.error(f"嵌套太深（最多 {LIMITS['max_json_depth']} 层）")
        return parsed
    if not isinstance(data, dict):
        parsed.error("最外层要是一个对象 {…}")
        return parsed
    if not _depth(data, LIMITS["max_json_depth"]):
        parsed.error(f"嵌套太深（最多 {LIMITS['max_json_depth']} 层）")
        return parsed
    if data.get("format") not in (None, "violet-schedule") or data.get("version") not in (None, 1):
        parsed.error("不是 Violet 的课表格式（format 要是 violet-schedule，version 要是 1）")
        return parsed
    term_data = _json_term(parsed, data.get("term"), term)
    periods = _json_periods(parsed, data)
    courses, by_name = _json_courses(parsed, data)
    first_monday = None
    if term_data:
        try:
            start = parse_date(term_data.get("start_date") or (term or {}).get("start_date"))
            first_monday = parse_date(term_data.get("week1_monday") or (term or {}).get("week1_monday")
                                      or start - timedelta(days=start.weekday()))
        except ValueError:
            first_monday = None
    exceptions = _json_exceptions(parsed, data, courses, by_name, first_monday)
    exams = _json_exams(parsed, data, courses, by_name)
    holidays = _json_holidays(parsed, data)
    if not parsed.errors and term_data is not None:
        # a section the file leaves out is left alone in the timetable (None); [] means "none any more"
        parsed.normalized = {"term": term_data, "periods": periods,
                             "courses": list(courses.values()) if "courses" in data else None,
                             "exceptions": exceptions, "exams": exams if "exams" in data else None,
                             "holidays": holidays if "holidays" in data else None}
    return parsed


def _json_term(parsed: Parsed, raw: Any, selected: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        if selected is None:
            parsed.error("文件里没有 term，也没有选择要导入到哪个学期", where="term")
            return None
        return {"name": selected["name"]}
    if not isinstance(raw, dict):
        parsed.error("要是一个对象", where="term")
        return None
    out = {k: _scalar(parsed, raw, k, "term") for k in ("name", "start_date", "end_date", "week1_monday", "timezone")}
    if not out["name"]:
        parsed.error("学期名称不能为空", where="term", field_name="name")
        return None
    if selected is not None and name_key(out["name"]) != name_key(selected["name"]):
        parsed.error(f"文件里的学期是“{str(out['name'])[:60]}”，和选择的学期不同（不选学期，就按文件里的学期名导入）",
                     where="term", field_name="name")
        return None
    if selected is not None:
        out["name"] = selected["name"]
    return {k: v for k, v in out.items() if v is not None}


def _json_periods(parsed: Parsed, data: dict[str, Any]) -> list[tuple[int, str, str]] | None:
    items = _list(parsed, data, "periods", LIMITS["max_periods"])
    if items is None:
        return None
    out = []
    for index, item in enumerate(items):
        where = f"periods[{index}]"
        if isinstance(item, list) and len(item) == 3:
            item = {"no": item[0], "start": item[1], "end": item[2]}
        if not isinstance(item, dict):
            parsed.error("要写成 {\"no\": 1, \"start\": \"08:00\", \"end\": \"08:45\"}", where=where)
            continue
        out.append((_scalar(parsed, item, "no", where, "period"), _scalar(parsed, item, "start", where),
                    _scalar(parsed, item, "end", where)))
    return out


def _json_courses(parsed: Parsed, data: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    courses: dict[str, dict[str, Any]] = {}
    by_name: dict[str, str] = {}
    for index, raw in enumerate(_list(parsed, data, "courses", LIMITS["max_courses"]) or []):
        where = f"courses[{index}]"
        if not isinstance(raw, dict):
            parsed.error("要是一个对象", where=where)
            continue
        name = _scalar(parsed, raw, "name", where)
        if not name:
            parsed.error("课程名不能为空", where=where, field_name="name")
            continue
        try:
            kind = _alias(KINDS, _scalar(parsed, raw, "kind", where), "lecture", "类型")
        except ValueError as exc:
            parsed.error(str(exc), where=where, field_name="kind")
            continue
        code = _scalar(parsed, raw, "code", where)
        key = course_key(name, code, _scalar(parsed, raw, "key", where))
        if key in courses:
            parsed.error(f"和 {parsed.lines[f'courses[{key}]']} 是同一门课（重复）", where=where)
            continue
        parsed.lines[f"courses[{key}]"] = where
        by_name.setdefault(name_key(name), key)
        course = {"key": key, "name": name, "kind": kind, "code": code,
                  "teacher": _scalar(parsed, raw, "teacher", where), "notes": _scalar(parsed, raw, "notes", where),
                  "slots": []}
        for s_index, slot in enumerate(_list(parsed, raw, "slots", LIMITS["max_slots_per_course"]) or []):
            s_where = f"{where}.slots[{s_index}]"
            if not isinstance(slot, dict):
                parsed.error("要是一个对象", where=s_where)
                continue
            values = {k: _scalar(parsed, slot, k, s_where) for k in ("weekday", "weeks", "periods", "period_start",
                                                                    "period_end", "start", "end", "location",
                                                                    "valid_from", "valid_until", "key")}
            fields = _slot_fields(parsed, values, {"where": s_where})
            if fields is None:
                continue
            skey = str(values.get("key") or "")[:120] or slot_key(fields["weekday"], fields.get("start"),
                                                                    fields.get("end"), fields["periods"],
                                                                    fields["weeks"])
            label = f"courses[{key}].slots[{skey}]"
            if label in parsed.lines:
                parsed.error(f"和 {parsed.lines[label]} 是同一个上课时间（重复）", where=s_where)
                continue
            parsed.lines[label] = s_where
            course["slots"].append(_normalized_slot(fields, skey))
        courses[key] = course
    return courses, by_name


def _course_ref(parsed: Parsed, ref: Any, courses: dict[str, dict[str, Any]], by_name: dict[str, str],
                where: str) -> str | None:
    text = str(ref or "").strip()
    if text in courses:
        return text
    if name_key(text) in by_name:
        return by_name[name_key(text)]
    by_code = {name_key(c["code"]): k for k, c in courses.items() if c.get("code")}
    if name_key(text) in by_code:
        return by_code[name_key(text)]
    parsed.error("课程要写这个文件里某门课的名称、课程代码或 key", where=where, field_name="course")
    return None


def _json_exceptions(parsed: Parsed, data: dict[str, Any], courses: dict[str, dict[str, Any]],
                     by_name: dict[str, str], first_monday: date | None) -> list[dict[str, Any]] | None:
    items = _list(parsed, data, "exceptions", LIMITS["max_exceptions"])
    if items is None:
        return None
    out, seen = [], {}
    for index, raw in enumerate(items):
        where = f"exceptions[{index}]"
        if not isinstance(raw, dict):
            parsed.error("要是一个对象", where=where)
            continue
        ckey = _course_ref(parsed, _scalar(parsed, raw, "course", where), courses, by_name, where)
        try:
            kind = _alias(EXCEPTION_KINDS, _scalar(parsed, raw, "kind", where), None, "变动类型")
            on = parse_date(_scalar(parsed, raw, "date", where, "on_date"))
        except ValueError as exc:
            parsed.error(str(exc), where=where, field_name="kind" if "类型" in str(exc) else "on_date")
            continue
        if ckey is None:
            continue
        skey = _scalar(parsed, raw, "slot", where)
        if kind != "extra" and not skey:
            candidates = [s["key"] for s in courses[ckey]["slots"] if s["weekday"] == on.isoweekday()
                          and (first_monday is None or parse_weeks(s["weeks"]).runs_in((on - first_monday).days // 7 + 1))]
            if len(candidates) != 1:
                parsed.error("这门课那天" + ("没有课" if not candidates else "有不止一次课，要用 slot 写明是哪一次（"
                                              + " / ".join(candidates) + "）"), where=where, field_name="slot")
                continue
            skey = candidates[0]
        new = {"new_date": _scalar(parsed, raw, "new_date", where), "new_start": _scalar(parsed, raw, "new_start", where),
               "new_end": _scalar(parsed, raw, "new_end", where), "new_period_start": None, "new_period_end": None}
        if _scalar(parsed, raw, "new_periods", where):
            try:
                new["new_period_start"], new["new_period_end"] = parse_periods(_scalar(parsed, raw, "new_periods", where))
            except ValueError as exc:
                parsed.error(str(exc), where=where, field_name="new_period_start")
                continue
        key = str(_scalar(parsed, raw, "key", where) or "")[:120] or f"{kind}|{ckey}|{skey or ''}|{on.isoformat()}"[:120]
        if key in seen:
            parsed.error(f"和 {seen[key]} 重复", where=where)
            continue
        seen[key] = where
        parsed.lines[f"exceptions[{key}]"] = where
        out.append({"key": key, "course_key": ckey, "slot_key": skey if kind != "extra" else None, "kind": kind,
                    "on_date": on.isoformat(), **new, "new_location": _scalar(parsed, raw, "new_location", where),
                    "reason": _scalar(parsed, raw, "reason", where)})
    return out


def _json_exams(parsed: Parsed, data: dict[str, Any], courses: dict[str, dict[str, Any]],
                by_name: dict[str, str]) -> list[dict[str, Any]]:
    out, seen = [], {}
    for index, raw in enumerate(_list(parsed, data, "exams", LIMITS["max_exams"]) or []):
        where = f"exams[{index}]"
        if not isinstance(raw, dict):
            parsed.error("要是一个对象", where=where)
            continue
        name, on = _scalar(parsed, raw, "name", where), _scalar(parsed, raw, "date", where, "exam_date")
        try:
            kind = _alias(EXAM_KINDS, _scalar(parsed, raw, "kind", where), "other", "考试类型")
        except ValueError as exc:
            parsed.error(str(exc), where=where, field_name="kind")
            continue
        if not name or not on:
            parsed.error("考试要写名称和日期", where=where, field_name="name" if not name else "exam_date")
            continue
        ckey = None
        if _scalar(parsed, raw, "course", where):
            ckey = _course_ref(parsed, _scalar(parsed, raw, "course", where), courses, by_name, where)
            if ckey is None:
                continue
        key = str(_scalar(parsed, raw, "key", where) or "")[:120] or f"{name_key(name)}|{on}"[:120]
        if key in seen:
            parsed.error(f"和 {seen[key]} 重复", where=where)
            continue
        seen[key] = where
        parsed.lines[f"exams[{key}]"] = where
        # no "course" in the object: the exam's course link is left as it is; "course": null removes it
        link = {"course_key": ckey, "course_id": None} if "course" in raw else {}
        out.append({"key": key, **link, "name": name, "kind": kind, "exam_date": on,
                    "start": _scalar(parsed, raw, "start", where), "end": _scalar(parsed, raw, "end", where),
                    "location": _scalar(parsed, raw, "location", where), "seat": _scalar(parsed, raw, "seat", where),
                    "notes": _scalar(parsed, raw, "notes", where)})
    return out


def _json_holidays(parsed: Parsed, data: dict[str, Any]) -> list[dict[str, Any]]:
    out, seen = [], {}
    for index, raw in enumerate(_list(parsed, data, "holidays", LIMITS["max_holidays"]) or []):
        where = f"holidays[{index}]"
        if not isinstance(raw, dict):
            parsed.error("要是一个对象", where=where)
            continue
        try:
            kind = _alias(HOLIDAY_KINDS, _scalar(parsed, raw, "kind", where), None, "假期类型")
        except ValueError as exc:
            parsed.error(str(exc), where=where, field_name="kind")
            continue
        start = _scalar(parsed, raw, "start_date", where, "date")
        key = str(_scalar(parsed, raw, "key", where) or "")[:120] or f"{kind}|{start}"[:120]
        if key in seen:
            parsed.error(f"和 {seen[key]} 重复", where=where)
            continue
        seen[key] = where
        parsed.lines[f"holidays[{key}]"] = where
        out.append({"key": key, "kind": kind, "name": _scalar(parsed, raw, "name", where), "start_date": start,
                    "end_date": _scalar(parsed, raw, "end_date", where),
                    "follows_date": _scalar(parsed, raw, "follows_date", where)})
    return out


def _size_ok(parsed: Parsed, content: str) -> bool:
    if not isinstance(content, str):
        parsed.error("文件内容要是文字（UTF-8）")
        return False
    if len(content.encode("utf-8")) > LIMITS["max_bytes"]:
        parsed.error(f"文件太大（最大 {LIMITS['max_bytes'] // 1024} KB）")
        return False
    if "\x00" in content:
        parsed.error("文件里有二进制内容，不是文字文件")
        return False
    if "\ufffd" in content:                 # what a GBK / ANSI file turns into when it is read as UTF-8
        parsed.error("文件不是 UTF-8 编码（Excel 请“另存为 CSV UTF-8”），没有导入")
        return False
    return True


def parse(content: str, file_name: str, fmt: str | None = None, term: dict[str, Any] | None = None) -> Parsed:
    """fmt csv | json (None: from the file name). term = the selected target term row (or None)."""
    name = clean_file_name(file_name)
    kind = str(fmt or (name.rsplit(".", 1)[-1] if "." in name else "")).lower()
    if kind == "csv":
        return parse_csv(content, name, term)
    if kind == "json":
        return parse_json(content, name, term)
    parsed = Parsed(kind or "?", name, _sha(content if isinstance(content, str) else ""))
    parsed.error("只支持 .csv 和 .json 文件")
    return parsed


# ----------------------------------------------------------------------------------------------- preview / commit

class _Undo(Exception):
    """Raised inside the dry run so its transaction is always rolled back."""


class StaleImport(Exception):
    pass


# Phase 9.4 closeout: how a CSV meets the courses already in the term. A CSV used to replace the term's whole list
# of imported courses, so a file with only 物理 deleted 数学 and 英语 imported earlier.
#   merge   (default) 追加 / 更新: the file's courses are created or updated; nothing else in the term changes
#   replace 整学期替换 (explicit): the term's imported courses become the file's; the others are deleted (soft)
# JSON keeps its own rule: every section the file has replaces that section of the term ("json").
CSV_MODES = ("merge", "replace")
COUNT_KEYS = ("created", "updated", "deleted", "unchanged", "skipped_manual")


def resolve_mode(parsed: Parsed, mode: Any) -> str:
    text = str(mode or "").strip().lower()
    if parsed.fmt == "json":
        if text not in ("", "json"):
            raise ScheduleValidationError({"mode": "JSON 按文件里写了的部分整体替换，不用选择追加 / 替换"})
        return "json"
    if not text:
        return "merge"
    if text not in CSV_MODES:
        raise ScheduleValidationError({"mode": "导入方式只能是 merge（追加 / 更新）或 replace（整学期替换）"})
    return text


def _scope(mode: str) -> str:
    return "file" if mode == "merge" else "term"


def _locate(parsed: Parsed, where: str) -> tuple[Any, str]:
    """A store problem ("courses[name:x].slots[1|p1-2|1-16].weekday") -> (CSV line / JSON path, field)."""
    best = ""
    for label in parsed.lines:
        if (where == label or where.startswith(label + ".")) and len(label) > len(best):
            best = label
    field_name = where[len(best) + 1:] if best else where
    return parsed.lines.get(best), field_name.split(".")[-1]


def _report_problems(parsed: Parsed, problems: list[dict[str, str]]) -> None:
    for p in problems:
        place, field_name = _locate(parsed, p["where"])
        if isinstance(place, int):
            parsed.error(p["message"], line=place, field_name=field_name)
        else:
            parsed.error(p["message"], where=place or p["where"].rsplit(".", 1)[0], field_name=field_name)


def conflicts(store: ScheduleStore, term_id: int | None) -> list[dict[str, Any]]:
    from ai.schedule.admin import term_conflicts
    return term_conflicts(store.load(), term_id) if term_id else []


def preview(store: ScheduleStore, parsed: Parsed, *, actor: str, mode: Any = None) -> dict[str, Any]:
    """Validate and compute what the import would do. Nothing is kept: the run happens in a transaction that is
    always rolled back (and nothing is logged as done). mode: see CSV_MODES (raises for a bad one)."""
    mode = resolve_mode(parsed, mode)
    out = {"file_name": parsed.file_name, "format": parsed.fmt, "sha256": parsed.sha256, "rows": parsed.rows,
           "warnings": parsed.warnings, "limits": LIMITS, "mode": mode}
    if parsed.errors:
        return {**out, "ok": False, "errors": parsed.errors}
    version = store.data_version()
    before_conflicts = None
    collected: list[dict[str, str]] = []
    result: dict[str, Any] = {}
    with quiet():
        try:
            with store.db.transaction():
                term_row = store.db.fetch_one("SELECT id FROM schedule_terms WHERE name = ?",
                                              (parsed.normalized["term"].get("name"),))
                before_conflicts = conflicts(store, term_row["id"]) if term_row else []
                import_id = store.record_import(file_name=parsed.file_name, sha256=parsed.sha256, fmt=parsed.fmt,
                                                actor=actor)
                result = store.apply_import(import_id, parsed.normalized, actor=actor, collect=collected,
                                            course_scope=_scope(mode))
                result["conflicts"] = conflicts(store, result.get("term_id"))
                result["new_term"] = term_row is None
                raise _Undo
        except _Undo:
            pass
        except ScheduleValidationError as exc:
            collected.extend({"where": k, "message": v} for k, v in exc.problems.items())
    _report_problems(parsed, collected)
    seen_before = {(c["a"], c["b"], c["weekday"], c["a_time"], c["b_time"]) for c in before_conflicts or []}
    for c in result.get("conflicts", []):
        c["new"] = (c["a"], c["b"], c["weekday"], c["a_time"], c["b_time"]) not in seen_before
    previous = store.db.fetch_one("SELECT created_at, applied_at FROM schedule_imports WHERE sha256 = ? AND "
                                  "status = 'applied' ORDER BY id DESC LIMIT 1", (parsed.sha256,))
    if previous:
        parsed.warn(f"这个文件在 {str(previous['applied_at'] or previous['created_at'])[:16].replace('T', ' ')} "
                    "导入过；再导入一次不会产生重复课程（没有手动改过的话，下面应该全是“不变”）")
    counts = {k: result.get(k, 0) for k in COUNT_KEYS}
    items = result.get("items", [])
    return {**out, "ok": not parsed.errors, "errors": parsed.errors, "warnings": parsed.warnings,
            "data_version": version, "term": parsed.normalized["term"].get("name"), "new_term": result.get("new_term"),
            "counts": counts, "items": items, "conflicts": result.get("conflicts", []),
            # what the preview's confirm button names: the courses that would be deleted
            "deleted_courses": [i["label"] for i in items if i["entity"] == "course" and i["action"] == "deleted"]}


def _expected(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not all(isinstance(value.get(k), int) and not isinstance(value.get(k), bool)
                                              for k in COUNT_KEYS):
        raise ScheduleValidationError({"expected_counts": "要带上预览给出的 counts"})
    return {k: int(value[k]) for k in COUNT_KEYS}


def commit(store: ScheduleStore, parsed: Parsed, *, sha256: str, data_version: str, actor: str, mode: Any = None,
           expected_counts: Any = None) -> dict[str, Any]:
    """Apply a previewed file. Refused (StaleImport) when the file is not the previewed one or the timetable
    changed since the preview; any other failure rolls everything back and records a failed import.

    mode as in preview(). expected_counts = the preview's counts: the import must come out exactly the same, or it is
    rolled back and refused -- so a confirm can never do something else than the preview showed (another mode, other
    deletions). Required for "replace"; checked whenever it is given."""
    mode = resolve_mode(parsed, mode)
    expected = _expected(expected_counts)
    if parsed.sha256 != sha256:
        raise StaleImport("文件和预览时的不一样，请重新预览")
    if parsed.errors:
        raise ScheduleValidationError({"file": "文件有错误，不能导入"})
    if mode == "replace" and expected is None:
        raise StaleImport("整学期替换要先预览，再按预览确认")
    try:
        with store.db.transaction():          # holds the database lock: nothing can change between check and write
            if store.data_version() != data_version:
                raise StaleImport("预览之后课表又被修改过了，请重新预览再确认")
            import_id = store.record_import(file_name=parsed.file_name, sha256=parsed.sha256, fmt=parsed.fmt,
                                            actor=actor)
            result = store.apply_import(import_id, parsed.normalized, actor=actor, course_scope=_scope(mode))
            if expected is not None and {k: result.get(k, 0) for k in COUNT_KEYS} != expected:
                raise StaleImport("导入结果和预览的不一样（导入方式或数据变了），课表没有改动，请重新预览")
    except StaleImport:
        raise
    except Exception as exc:
        problems = exc.problems if isinstance(exc, ScheduleValidationError) else {"import": exc.__class__.__name__}
        failed = store.record_import(file_name=parsed.file_name, sha256=parsed.sha256, fmt=parsed.fmt, actor=actor,
                                     summary={"failed": True}, error_count=max(1, len(problems)))
        store.db.execute("UPDATE schedule_imports SET status = 'failed' WHERE id = ?", (failed,))
        raise
    return {**{k: v for k, v in result.items() if k != "items"}, "import_id": import_id, "mode": mode,
            "changes": [i for i in result["items"] if i["action"] not in ("unchanged",)][:500]}
