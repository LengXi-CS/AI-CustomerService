"""Inspect a captured native quote; optionally export its XML for comparison."""

from __future__ import annotations

import argparse
import json
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

from fastgpt_assistant.orchestration.inbound import normalize_media_xml


def main() -> int:
    parser = argparse.ArgumentParser(description="只读检查最近的完整引用回调")
    parser.add_argument("--db", type=Path, default=Path("data/chat-log.db"))
    parser.add_argument("--chat", default="49195424680@chatroom")
    parser.add_argument("--text", default="", help="按回复正文筛选，例如手工引用测试")
    parser.add_argument("--output", type=Path, help="将完整引用 XML 保存到指定本地文件")
    args = parser.parse_args()
    if not args.db.exists():
        print("聊天记录库不存在。")
        return 1
    with sqlite3.connect(f"{args.db.resolve().as_uri()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT message_id, text, metadata, occurred_at FROM chat_messages "
            "WHERE chat_id = ? AND direction = 'inbound' AND quoted_msg_id <> '' "
            "ORDER BY id DESC",
            (args.chat,),
        )
        for row in rows:
            if args.text and args.text not in row["text"]:
                continue
            metadata = json.loads(row["metadata"])
            raw_xml = metadata.get("raw_quote_xml")
            if not isinstance(raw_xml, str) or not raw_xml:
                continue
            xml = normalize_media_xml(raw_xml, is_group=True)
            try:
                root = ET.fromstring(xml)
            except ET.ParseError:
                print("已采集原始引用 XML，但不能完整解析；请保留原文排查。")
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(raw_xml, encoding="utf-8")
                return 1
            appmsg = root if root.tag == "appmsg" else root.find("appmsg")
            if appmsg is None:
                print("已采集内容缺少 appmsg 节点。")
                return 1
            refer = appmsg.find("refermsg")
            print(
                json.dumps(
                    {
                        "message_id": row["message_id"],
                        "occurred_at": row["occurred_at"],
                        "appmsg_fields": [item.tag for item in appmsg],
                        "refermsg_fields": [item.tag for item in refer]
                        if refer is not None
                        else [],
                        "has_callback_msg_source": bool(metadata.get("raw_quote_msg_source")),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(raw_xml, encoding="utf-8")
                print(f"完整原文已保存到：{args.output.resolve()}")
            return 0
    print("尚未找到完整引用回调。需要先重启服务加载采集代码，再手工引用一次。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
