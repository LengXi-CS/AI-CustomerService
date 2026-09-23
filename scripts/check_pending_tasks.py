"""只读检查会话状态库：重启服务前确认没有未终态任务。

为什么需要：poller 启动后会立刻执行第一次轮询，若状态库里有到期任务，
它会真的往微信外发。改代码重启前必须先确认状态库干净。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe scripts/check_pending_tasks.py

只读打开（`mode=ro`），不会修改任何数据。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

TERMINAL_STATES = ("responded", "blocked")
DEFAULT_DB = Path("data/conversation-state.db")


def report(db_path: Path) -> int:
    if not db_path.exists():
        print(f"状态库不存在: {db_path}")
        return 0

    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        print("-- 任务状态分布 --")
        for row in con.execute(
            "SELECT state, COUNT(*) n FROM conversation_tasks GROUP BY state ORDER BY n DESC"
        ):
            print(f"  {row['state']}: {row['n']}")

        print("-- 未终态任务（重启后首次轮询可能立即处理）--")
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        pending = list(
            con.execute(
                f"SELECT task_id, chat_id, state, updated_at FROM conversation_tasks "
                f"WHERE state NOT IN ({placeholders}) ORDER BY updated_at DESC LIMIT 30",
                TERMINAL_STATES,
            )
        )
        if pending:
            for row in pending:
                print(
                    f"  {row['task_id']} | {row['chat_id']} | "
                    f"{row['state']} | {row['updated_at']}"
                )
            print(f"  => 共 {len(pending)} 条未终态任务，请确认是否可安全重启")
        else:
            print("  无 —— 可安全重启，不会误发微信")

        print("-- 最近 5 条事件 --")
        for row in con.execute(
            "SELECT chat_id, sender_role, received_at FROM conversation_events "
            "ORDER BY received_at DESC LIMIT 5"
        ):
            print(f"  {row['chat_id']} {row['sender_role']} {row['received_at']}")
    finally:
        con.close()
    return 0


def main(argv: list[str]) -> int:
    db_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_DB
    return report(db_path)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
