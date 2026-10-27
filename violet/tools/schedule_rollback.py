"""Phase 9.4.1: remove the timetable tables from a database (the destructive half of a rollback).

    python -m tools.schedule_rollback --db PATH                 drop them only if they are all empty
    python -m tools.schedule_rollback --db PATH --confirm       also when they hold rows: a JSON backup of every
                                                                row is written first (--backup-dir, default next
                                                                to the database)

Nothing else in the database is touched. Older code ignores these tables anyway (migrations/phase94_down.sql),
so this is only needed to really get rid of them. Run it on a backed-up copy."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.schedule_rollback")
    ap.add_argument("--db", required=True, help="the SQLite database file")
    ap.add_argument("--confirm", action="store_true", help="drop even when there are rows (backup first)")
    ap.add_argument("--backup-dir", help="where the JSON backup goes (default: next to the database)")
    args = ap.parse_args(argv)
    path = Path(args.db)
    if not path.is_file():
        print(f"不存在：{path}")
        return 2
    from database import VioletDatabase
    from ai.schedule.schema import RollbackRefused, drop_schedule_tables, row_counts
    db = VioletDatabase(path, initialize=False)
    try:
        counts = row_counts(db)
        print("课表表里的行数：" + (", ".join(f"{k}={v}" for k, v in counts.items()) or "（没有课表表）"))
        try:
            done = drop_schedule_tables(db, confirm=args.confirm, backup_dir=args.backup_dir)
        except RollbackRefused as exc:
            print(f"没有删除：{exc}")
            return 1
        print(f"已删除 {len(done['dropped'])} 张表" + (f"；备份：{done['backup']}" if done["backup"] else ""))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
