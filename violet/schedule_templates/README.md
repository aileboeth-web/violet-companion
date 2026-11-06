# Timetable import: templates and how to fill them in

**English** | [简体中文](README.zh-CN.md)

In the control panel: Timetable → Import / Export. Two file types are supported: **CSV** (only courses and their weekly class times, imported into the term selected on the page) and **JSON** (terms, periods, courses, rescheduled and cancelled classes, exams and holidays can all go in). Save Excel files as "CSV (UTF-8)" first.

An import has five steps: choose the file → preview (lists format errors row by row, what will be added / changed / deleted, time conflicts, and records you changed by hand that will not be overwritten) → confirm → written in one database transaction. **The preview does not change the timetable**; at confirmation, if any single item fails, nothing is changed. If the timetable was changed after the preview (for example on another page), the confirmation is refused and you need to preview again.

## CSV (`schedule_template.csv`)

- UTF-8 encoding, column names in the first row, and each row is **one weekly class time of one course**. A course with several class times has several rows.
- Column names (Chinese column names are accepted too):

| Column | Required | How to write it |
|---|---|---|
| course | yes | Course name, up to 60 characters |
| code | | Up to 40 characters |
| kind | | lecture / lab / seminar / pe / other (default lecture) |
| teacher | | Up to 40 characters |
| weekday | yes | 1–7, or mon…sun |
| periods | one of the two | `1-2`, `3`; to use periods, first set the time of each period under Periods |
| start / end | one of the two | `08:00` / `09:40`, on the same day, end later than start |
| weeks | yes | `1-16`, `1-16 odd`, `2-16 even`, `1-8,10-16` (skips week 9), `1,3,5`; when it contains a comma, wrap the whole cell in double quotes |
| location | | Up to 60 characters |
| notes | | Up to 500 characters |
| course_key | | Optional: a fixed ID you give the course |
| key | | Optional: a fixed ID you give this class time |

In the rows of one course, code, teacher and notes must either be the same or left empty; differences are reported as errors.

## JSON (`schedule_template.json`)

The top level is an object: `format` (`violet-schedule`), `version` (1), `term`, `periods`, `courses`, `exceptions`, `exams`, `holidays`.

- `term`: `name`, `start_date`, `end_date`, `week1_monday` (may be omitted = the Monday of the week the term starts), `timezone` (an IANA name such as `Asia/Shanghai`). If the term name already exists, the import goes into that term (dates can be left out); if a term is selected on the page, the term name in the file must be the same, or `term` must be left out.
- `periods`: `{"no": 1, "start": "08:00", "end": "08:45"}`, up to 20 periods. When given, it replaces the term's whole period table; when left out, the table is not touched.
- `courses[].slots[]`: `weekday`, `weeks`, `periods` or `start` / `end`, `location`, optional `valid_from` / `valid_until`.
- `exceptions[]`: `course` (course name or course key), `kind` (cancel / move / time / room / extra), `date` (the original class day; for an extra class, the day of the extra class), then depending on the kind `new_date`, `new_start` / `new_end` or `new_periods`, `new_location`, and optionally `reason`. When the course meets more than once that day, name the class time with `slot` (the preview lists the IDs to choose from). **Without `exceptions`, existing rescheduled and cancelled classes are not touched**; with it (even an empty list), ones imported earlier that are not in the file are undone.
- `exams[]`: `name`, `date`, `kind` (final / midterm / quiz / other), optional `start` / `end`, `course` (course name, code or key), `location`, `seat`, `notes`. Without `course` the link between exam and course is not changed; only `"course": null` removes it.
- `holidays[]`: `kind` (holiday / no_class / makeup), `name`, `start_date`, `end_date` (may be omitted = one day); a make-up day takes `follows_date` (which day's timetable it follows).

## How "the same course" is recognised (importing again creates no duplicates)

- Term: the term name.
- Course: the course key if there is one; otherwise the course code if there is one; otherwise the course name (ignoring spaces, full-width / half-width and case). **Write the same course the same way in different files** (always with the code, or always without).
- Class time: the key if there is one; otherwise weekday + periods (or times) + weeks. Changing only the location / teacher counts as a "change"; changing the weekday, time or weeks makes it a different class time (the old one is deleted and a new one added).
- Exam: name + date; holiday: kind + start date; rescheduled / cancelled class: kind + course + class time + date.

Importing the same file again should show "unchanged" for everything in the preview.

## The two ways to import a CSV

- **Add / update (default, recommended)**: courses in the file are added or updated, and the term's other courses **stay as they are**. So you can import in several files (for example maths and English first, then physics). For a course that is in the file, the file wins: its class times that are not in the file are deleted (for example, changing Monday to Wednesday deletes the old one and adds the new one, rather than leaving an extra one).
- **Replace the whole term (you have to choose it)**: courses imported earlier into this term that are not in the file are deleted. The preview lists "N courses will be deleted" with their names separately, and the confirmation dialog repeats it; cancel and nothing changes. Only the term being imported into is affected.

Neither way overwrites or deletes courses entered or changed by hand. Confirmation runs the way chosen at preview time: if you switch the way after previewing, preview again; if the result differs from the preview, it is refused and the timetable is not changed.

JSON has no choice: each part the file contains (courses, exams, holidays, exceptions) replaces that part as a whole — items imported earlier that are not in that part are marked for deletion (visible in the preview); parts that are not in the file are not touched.

## Changes by hand come first

- Imported records you changed on the page are **never overwritten, restored or deleted** by later imports; the preview marks them "changed by hand".
- When a course with the same name was entered by hand, the import **does not create another one** and does not change it (nor its class times and rescheduled / cancelled classes); the preview marks it.
- The same goes for a hand-entered exam with the same name and date, or a holiday / make-up day with the same kind and start date: the hand-entered one is kept and no new one is created.

## Limits

Files up to 256 KB; CSV up to 1000 rows; up to 500 characters per cell; JSON nested up to 6 levels; up to 200 courses, 30 class times per course, 500 rescheduled / cancelled classes, 200 exams and 100 holidays. Excel, ICS and other timetable apps' formats are not supported.

## Export

Under Import / Export a term can be exported as CSV or JSON (same format as the import; importing an export again changes nothing). You can choose to leave out teachers, locations, seat numbers and notes. In CSV, content starting with `=`, `+`, `-` or `@` gets a `'` in front, so spreadsheet software does not run it as a formula; it is removed again on import. Export files contain personal information, so keep them safe.
