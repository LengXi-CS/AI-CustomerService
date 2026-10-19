"""Scratch: read-only look at conversation-state.db before a restart."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def main() -> None:
    db = Path("data/conversation-state.db")
    if not db.exists():
        Path("_state_now.txt").write_text("状态库不存在", encoding="utf-8")
        return
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    lines: list[str] = ["-- 任务状态分布 --"]
    for row in con.execute(
        "SELECT state, COUNT(*) n FROM conversation_tasks GROUP BY state ORDER BY n DESC"
    ):
        lines.append(f"  {row['state']}: {row['n']}")

    lines.append("-- 非终态任务明细（排除已 responded / blocked）--")
    pending = list(
        con.execute(
            "SELECT task_id, chat_id, state, updated_at FROM conversation_tasks "
            "WHERE state NOT IN ('responded','blocked') ORDER BY updated_at DESC LIMIT 20"
        )
    )
    if pending:
        lines.extend(
            f"  {r['task_id']} | {r['chat_id']} | {r['state']} | {r['updated_at']}"
            for r in pending
        )
    else:
        lines.append("  无")

    lines.append("-- 最近 8 条事件 --")
    for row in con.execute(
        "SELECT chat_id, sender_role, received_at FROM conversation_events "
        "ORDER BY received_at DESC LIMIT 8"
    ):
        lines.append(f"  {row['chat_id']} {row['sender_role']} {row['received_at']}")
    con.close()
    Path("_state_now.txt").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
