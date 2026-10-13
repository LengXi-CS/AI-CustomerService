"""Validate one newly archived test-group text; --send explicitly sends one reply."""

from __future__ import annotations

import argparse
import json
import sqlite3

from fastgpt_assistant.adapters.wechat.client import WechatGeweClient
from fastgpt_assistant.core.chat_log import ChatLogStore
from fastgpt_assistant.core.config import settings
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.core.wechat_quote import TextQuote
from fastgpt_assistant.orchestration.conversation_state import ConversationStateStore
from fastgpt_assistant.orchestration.engine import AssistantEngine, ReplyDeliverable

TEST_ROOM = "49195424680@chatroom"


def main() -> int:
    parser = argparse.ArgumentParser(description="检查新文本消息的自动引用信息；默认不发送")
    parser.add_argument("--text", required=True, help="测试群中新发的消息正文，须完整匹配")
    parser.add_argument("--send", action="store_true", help="向测试群发送一条自动引用验证回复")
    args = parser.parse_args()
    database_uri = f"{settings.chat_log_db_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM chat_messages WHERE chat_id = ? AND direction = 'inbound' "
            "AND msg_type = 1 AND text = ? ORDER BY id DESC LIMIT 1",
            (TEST_ROOM, args.text),
        ).fetchone()
    if row is None:
        print("未找到测试群里的对应文本消息，未发送。")
        return 1
    source = TextQuote.from_mapping(json.loads(row["metadata"]).get("reply_source"))
    if not source or not source.native_ready or (
        source.message_id != row["message_id"] or source.chat_id != TEST_ROOM
        or source.sender_id != row["sender_id"]
    ):
        print("该消息的完整引用信息尚不可用，未发送；请重启加载采集代码后发一条新文本。")
        return 1
    config = WechatGroupsConfig.load(settings.wechat_groups_config_path)
    policy = config.get(TEST_ROOM)
    if (
        not config.enabled or not policy or policy.mode != "active"
        or not policy.native_quote_replies
    ):
        print("测试群的原生引用开关未启用，未发送。")
        return 1
    print(json.dumps({
        "chat_name": row["chat_name"], "message_id": source.message_id,
        "occurred_at": row["occurred_at"], "native_ready": True, "send": args.send,
    }, ensure_ascii=False))
    if not args.send:
        return 0
    engine = object.__new__(AssistantEngine)
    engine.settings = settings
    engine.groups_config = config
    engine.chat_log = ChatLogStore(settings.chat_log_db_path, enabled=settings.chat_log_enabled)
    engine.conversation_state = ConversationStateStore(settings.conversation_state_db_path)
    reply = ReplyDeliverable(
        text="新消息自动引用验证：已收到这条测试文本。",
        channel_used="soothe", reply_source=source,
    )
    with WechatGeweClient(
        base_url=settings.gewe_api_url, token=settings.gewe_token, app_id=settings.gewe_app_id
    ) as client:
        engine.wechat = client
        outcome = engine.dispatch_outbound(TEST_ROOM, reply)
        engine.log_group_outbound(to_wxid=TEST_ROOM, reply=reply, outcome=outcome)
    print(json.dumps({
        "text_status": outcome.text_status, "message_id": outcome.message_id,
        "native_quote": bool(outcome.text_quote), "sent": outcome.sent_text,
    }, ensure_ascii=False))
    return 0 if outcome.sent_text else 1


if __name__ == "__main__":
    raise SystemExit(main())
