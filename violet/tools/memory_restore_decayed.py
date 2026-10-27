"""Find (and optionally restore) event memories that the old nightly decay marked 'deleted' (phase 9.1, A1).

Before 9.1, decay_event_memories() moved weak memories forgotten -> deleted after about 2-4 weeks. That is the same
status as a deletion by the user, but a user / dashboard deletion always writes a 'retract' row to memory_revisions;
decay never wrote one. So a deleted row WITHOUT a retract revision was not deleted by the user (it may also be a
deletion from the pre-2.0 code, which kept no revisions either -- the list cannot tell those apart).

    python -m tools.memory_restore_decayed --db <copy of violet.db>                 list only, changes nothing
    python -m tools.memory_restore_decayed --db <copy> --apply [--ids 3,8,13]         restore to 'forgotten'

Restored rows become 'forgotten' (searchable, labelled 印象很模糊) with a 'restore' revision; content is unchanged.
--db is required (no default): back up the database first and stop Violet while applying. Rows deleted by the user
(with a retract revision) and rows removed by a personal-data purge (no longer in the table) are never touched.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

CANDIDATES_SQL = """
SELECT e.id, e.kind, e.progress, e.content, e.created_at, e.updated_at FROM event_memories e
WHERE e.status = 'deleted' AND NOT EXISTS (
    SELECT 1 FROM memory_revisions r WHERE r.memory_table = 'event_memories' AND r.memory_id = e.id AND r.action = 'retract')
ORDER BY e.id
"""


def candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(CANDIDATES_SQL).fetchall()


def restore(conn: sqlite3.Connection, ids: list[int], now: str) -> int:
    restored = 0
    with conn:
        for memory_id in ids:
            cursor = conn.execute(
                "UPDATE event_memories SET status = 'forgotten', updated_at = ? WHERE id = ? AND status = 'deleted' "
                "AND NOT EXISTS (SELECT 1 FROM memory_revisions r WHERE r.memory_table = 'event_memories' "
                "AND r.memory_id = event_memories.id AND r.action = 'retract')", (now, memory_id))
            if cursor.rowcount:
                conn.execute(
                    "INSERT INTO memory_revisions (memory_table, memory_id, action, reason, source, created_at) "
                    "VALUES ('event_memories', ?, 'restore', ?, 'restore_decayed', ?)",
                    (memory_id, "deleted by nightly decay (no retract revision); restored to forgotten", now))
                restored += 1
    return restored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.memory_restore_decayed", description=__doc__.split("\n")[0])
    parser.add_argument("--db", required=True, help="path of the SQLite database (work on a backup copy first)")
    parser.add_argument("--apply", action="store_true", help="restore the listed rows to 'forgotten'")
    parser.add_argument("--ids", default="", help="comma-separated ids to restore (default: every candidate)")
    args = parser.parse_args(argv)
    path = Path(args.db)
    if not path.is_file():
        print(f"数据库不存在：{path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = candidates(conn)
        print(f"没有用户删除记录、却是 deleted 的事件记忆：{len(rows)} 条")
        for r in rows:
            text = (r["content"] or "").replace("\n", " ")
            print(f"- id={r['id']} {r['kind']}/{r['progress']} 创建 {r['created_at'][:16]} 删除 {r['updated_at'][:16]}："
                  f"{text[:30]}{'…' if len(text) > 30 else ''}")
        if not args.apply:
            print("只列出，没有修改。确认后加 --apply（可用 --ids 只恢复其中几条）。")
            return 0
        wanted = {r["id"] for r in rows}
        if args.ids.strip():
            try:
                chosen = [int(x) for x in args.ids.split(",") if x.strip()]
            except ValueError:
                print("--ids 只能是逗号分隔的数字", file=sys.stderr)
                return 2
            unknown = [i for i in chosen if i not in wanted]
            if unknown:
                print(f"这些 id 不在候选里，没有修改任何数据：{unknown}", file=sys.stderr)
                return 2
        else:
            chosen = sorted(wanted)
        now = datetime.now().astimezone().isoformat()
        print(f"已恢复为 forgotten：{restore(conn, chosen, now)} 条（每条都写了 restore 修订记录）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
