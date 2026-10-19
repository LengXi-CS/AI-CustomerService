"""Replay archived contact-card callbacks into temp stores and mocked WeChat."""
import asyncio
import gc
import json
import runpy
import sqlite3
import tempfile
from pathlib import Path

import httpx
from pytest import MonkeyPatch

from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.server import app as app_module


async def main():
    room = "49195424680@chatroom"
    archive = Path("data/chat-log.db").resolve()
    with sqlite3.connect(archive.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute(
            "SELECT message_id,sender_id,msg_type,text,occurred_at FROM chat_messages "
            "WHERE chat_id=? AND direction='inbound' AND msg_type IN (42,66) "
            "AND occurred_at>=? AND occurred_at<? ORDER BY occurred_at,id",
            (room, "2026-10-16T06:55:00+00:00", "2026-10-16T06:58:00+00:00"),
        )]
    assert len(rows) == 7
    test = runpy.run_path("tests/test_contact_cards.py")
    with tempfile.TemporaryDirectory(prefix="contact-replay-") as directory, MonkeyPatch.context() as patch:
        fixture = test["engine"].__wrapped__(Path(directory), patch)
        engine = next(fixture)
        try:
            engine.groups_config = WechatGroupsConfig.from_mapping({
                "global": {"enabled": True}, "groups": [{"group_id": room, "mode": "active",
                    "associate_customer_demands": True}],
            })
            reasons = []
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app),
                                          base_url="http://replay") as client:
                for row in rows:
                    response = await client.post("/api/wechat/callback", json={
                        "TypeName": "AddMsg", "received_at": row["occurred_at"], "Data": {
                            "NewMsgId": row["message_id"], "MsgType": row["msg_type"],
                            "ActualUserName": {"string": row["sender_id"]},
                            "FromUserName": {"string": room}, "ToUserName": {"string": "bot"},
                            "Content": {"string": row["sender_id"] + ":\n" + row["text"]},
                        },
                    })
                    reasons.append(response.json().get("reason"))
            engine.reconcile_customer_issues(now="2026-10-16T07:00:00+00:00")
            ready = engine.conversation_state.claim_ready_tasks(
                now="2026-10-16T07:00:00+00:00", quiet_window_seconds=0,
            )
            result = {"group": "AI客服测试（外部）", "contact_callbacks": len(rows),
                      "all_classified_as_cards": reasons == ["contact_card"] * len(rows),
                      "archived_messages": engine.chat_log.count(chat_id=room),
                      "conversation_events": engine.conversation_state.count_events(room),
                      "model_calls": len(engine.model_inputs) + len(engine.queries),
                      "automatic_tasks": len(ready), "simulated_wechat_requests": len(engine.requests),
                      "real_wechat_sends": 0}
            assert result["all_classified_as_cards"]
            assert result["conversation_events"] == result["model_calls"] == result["automatic_tasks"] == 0
            assert result["archived_messages"] == 7
            Path("outputs/contact-card-replay.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            try:
                next(fixture)
            except StopIteration:
                pass
            gc.collect()


if __name__ == "__main__":
    asyncio.run(main())
