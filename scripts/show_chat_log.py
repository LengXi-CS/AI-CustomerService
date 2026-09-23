"""只读查看已配置群的聊天记录库（data/chat-log.db）。

为什么需要：聊天记录是 web 前端的数据源，先得能在命令行确认"到底存进去了什么、
字段全不全、时间对不对"。本脚本只读打开（`mode=ro`），不会创建 WAL、不会改任何数据。

用法（在项目根目录执行）：
    # 1) 先看有哪些群、各有多少条
    .venv\\Scripts\\python.exe scripts/show_chat_log.py

    # 2) 看某个群最近的聊天记录（默认 30 条，按时间正序）
    .venv\\Scripts\\python.exe scripts/show_chat_log.py --chat 49195424680@chatroom

    # 3) 增量拉新（前端轮询用的同一套 id 游标）
    .venv\\Scripts\\python.exe scripts/show_chat_log.py --chat 49195424680@chatroom --after 128

    # 4) 输出 JSON 行，直接喂给前端调试
    .venv\\Scripts\\python.exe scripts/show_chat_log.py --chat 49195424680@chatroom --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path("data/chat-log.db")

ROLE_LABELS = {
    "customer": "客户",
    "staff": "客服",
    "assistant": "机器人",
}
KIND_LABELS = {
    "text": "",
    "image": "[图片]",
    "file": "[文件]",
    "voice": "[语音]",
    "video": "[视频]",
    "other": "[其他消息]",
}


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(
            f"聊天记录库不存在: {db_path}\n"
            "服务重启后才会创建；确认 .env 里 CHAT_LOG_ENABLED 未被设为 false。"
        )
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def show_summary(con: sqlite3.Connection) -> None:
    rows = con.execute(
        "SELECT chat_id, chat_name, COUNT(*) AS total, "
        "MIN(occurred_at) AS first_at, MAX(occurred_at) AS last_at "
        "FROM chat_messages GROUP BY chat_id, chat_name ORDER BY last_at DESC"
    ).fetchall()
    if not rows:
        print("库里还没有任何记录。")
        print("提示：只有 config/wechat_groups.toml 里已配置的群、且消息经过回调才会入库。")
        return
    print("群名 | 群 ID | 条数 | 最早 → 最新（本地时间）")
    print("-" * 100)
    for row in rows:
        print(
            f"{row['chat_name'] or '-'} | {row['chat_id']} | {row['total']} | "
            f"{local_time(row['first_at'])} → {local_time(row['last_at'])}"
        )


def local_time(value: str) -> str:
    """把库里的 UTC ISO 时间转成本机时区显示。"""
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "-"


def render(record: dict) -> str:
    role = ROLE_LABELS.get(str(record["sender_role"]), str(record["sender_role"]))
    when = local_time(str(record["occurred_at"]))
    if str(record["direction"]) == "outbound":
        who = "机器人"
    else:
        # GeWe 未提供昵称时 sender_name 为空，退回 wxid，绝不编造显示名。
        who = f"{role}({record['sender_name'] or record['sender_id']})"
    body = str(record["text"] or KIND_LABELS.get(str(record["kind"]), ""))
    if record["kind"] in {"image", "file", "voice", "video"} and record["media_name"]:
        body = f"{KIND_LABELS.get(str(record['kind']), '')}{record['media_name']}"

    lines = [f"[{when}] #{record['id']} {who}: {body}"]
    mentions = (record.get("metadata") or {}).get("mentions") or []
    if mentions:
        lines.append(f"      @ {', '.join(str(item) for item in mentions)}")
    if record["quoted_text"]:
        lines.append(f"      ↪ 引用({record['quoted_type'] or '-'}): {record['quoted_text']}")
    if record["media_ref"]:
        lines.append(f"      📎 media_ref={record['media_ref']}")
    if record["channel_used"]:
        lines.append(f"      channel={record['channel_used']} task={record['task_id'] or '-'}")
    return "\n".join(lines)


def show_chat(
    con: sqlite3.Connection,
    chat_id: str,
    *,
    after_id: int,
    limit: int,
    as_json: bool,
) -> None:
    rows = con.execute(
        "SELECT * FROM chat_messages WHERE chat_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
        (chat_id, after_id, limit),
    ).fetchall()
    if not rows:
        print(f"{chat_id} 没有 id > {after_id} 的记录。")
        return
    records = []
    for row in rows:
        record = dict(row)
        # dedupe_key 只是幂等用的内部键（含 \x1f 分隔符），不该出现在前端数据里。
        record.pop("dedupe_key", None)
        try:
            record["metadata"] = json.loads(record["metadata"] or "{}")
        except json.JSONDecodeError:
            record["metadata"] = {}
        records.append(record)

    if as_json:
        for record in records:
            print(json.dumps(record, ensure_ascii=False))
        return
    for record in records:
        print(render(record))
    newest = records[-1]["id"]
    print(f"\n共 {len(records)} 条；下一条请用 --after {newest}")


def main() -> int:
    parser = argparse.ArgumentParser(description="只读查看群聊天记录库")
    parser.add_argument("--chat", default="", help="群 ID，形如 49195424680@chatroom")
    parser.add_argument("--after", type=int, default=0, help="只看 id 大于该值的记录（增量拉取）")
    parser.add_argument("--limit", type=int, default=30, help="最多显示多少条，默认 30")
    parser.add_argument("--json", action="store_true", help="按 JSON 行输出，便于前端调试")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="聊天记录库路径")
    args = parser.parse_args()

    with connect_readonly(args.db) as con:
        if args.chat:
            show_chat(
                con,
                args.chat,
                after_id=max(0, args.after),
                limit=max(1, args.limit),
                as_json=args.json,
            )
        else:
            show_summary(con)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
