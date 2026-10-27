"""Phase 9.1.2: list facts that contradict each other across the memory stores -- READ-ONLY, nothing is changed.

    python -m tools.memory_conflicts --db <a COPY of violet.db> [--json]

Corrections made before 9.1.1 / 9.1.2 can have left
  duplicate_active_facts  two active profile facts of one key differing by one short part (豆豆 / 豆包)
  stale_copy              a corrected value still in an active event memory, core memory or profile fact
  pending_conflict        a conflict recorded at correction time that nobody has resolved yet
The database is opened read-only (sqlite URI mode=ro); fix what it lists in the Control Center (纠正 / 停用 / 删除).
Exit code: 0 nothing found, 1 something found, 2 cannot open.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

LABELS = {"duplicate_active_facts": "同一 key 两条有效档案", "stale_copy": "已纠正的旧值还在", "pending_conflict": "待确认冲突"}


class ReadOnlyDB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def fetch_all(self, sql: str, params=()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def close(self) -> None:
        self.conn.close()


def main(argv: list[str] | None = None, out=print) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.memory_conflicts", description=__doc__.splitlines()[0])
    p.add_argument("--db", required=True, help="数据库副本（只读打开）")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    args = p.parse_args(argv)
    path = Path(args.db)
    if not path.is_file():
        out(f"找不到数据库：{path}")
        return 2
    from ai.memory_consistency import find_conflicts
    db = ReadOnlyDB(path)
    try:
        found = find_conflicts(db)
    except sqlite3.Error as exc:
        out(f"读取失败：{exc}")
        return 2
    finally:
        db.close()
    if args.json:
        out(json.dumps(found, ensure_ascii=False, indent=1))
    else:
        for f in found:
            where = f"档案 {f['fact_ids']}" if "fact_ids" in f else f"{f['table']} #{f['id']}"
            out(f"{LABELS[f['kind']]}：{where}：“{f['old']}” → “{f['new']}”")
        out(f"共 {len(found)} 处；没有修改任何数据")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
