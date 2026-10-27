"""Phase 9.4: the user's timetable (terms, courses, weekly meetings, exceptions, exams, holidays).

Owned by Violet's own SQLite database; never copied into event memories or the profile. Layers:
  schema.py   tables (created at startup, idempotent) and the guarded removal used for a rollback
  model.py    plain data classes, the canonical week rule and validation of what may be written
  engine.py   pure computation: which classes happen on a date, term / week / parity, exceptions, holidays,
              make-up days, exams, conflicts, time zones -- no database, no clock
  store.py    the one data-access layer (transactions, revisions, source precedence), reused later by the
              violet-schedule MCP server and the Control Center
"""
