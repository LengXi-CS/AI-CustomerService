"""Native quotes must survive delayed processing and keep per-message identity."""

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import httpx
import pytest

from fastgpt_assistant.adapters.wechat.client import WechatGeweClient
from fastgpt_assistant.core.chat_log import ChatLogStore
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.core.wechat_quote import TextQuote
from fastgpt_assistant.orchestration.conversation_state import ConversationStateStore
from fastgpt_assistant.orchestration.engine import AssistantEngine, ReplyDeliverable
from fastgpt_assistant.orchestration.payload_vault import PayloadVault, generate_payload_vault_key

ROOM = "native-test@chatroom"
MESSAGE = "3233375625312251145"


def callback(message_id=MESSAGE, *, str_id="per-message-A", text="请核实订单甲是否漏发"):
    return {
        "TypeName": "AddMsg",
        "Data": {
            "NewMsgId": message_id,
            "MsgType": 1,
            "FromUserName": {"string": ROOM},
            "ToUserName": {"string": "bot"},
            "ActualUserName": {"string": "456@openim"},
            "CreateTime": 1791852530,
            "Content": {"string": f"456@openim:\n{text}"},
            "MsgSource": {
                "string": f"<msgsource><strid>{str_id}</strid>"
                "<signature>original-signature</signature><future>retain</future></msgsource>"
            },
        },
    }


def engine_for(tmp_path, key, *, enabled=True, transport=None, channel="soothe"):
    engine = object.__new__(AssistantEngine)
    engine.settings = SimpleNamespace(gewe_app_id="bot", attachment_dispatch_interval_seconds=0)
    engine.conversation_state = ConversationStateStore(tmp_path / "state.db")
    engine.payload_vault = PayloadVault(key, tmp_path / "vault.db")
    engine.chat_log = ChatLogStore(tmp_path / "chat-log.db")
    engine.groups_config = WechatGroupsConfig.from_mapping({
        "global": {"enabled": True},
        "groups": [{"group_id": ROOM, "mode": "active", "native_quote_replies": enabled}],
    })
    engine._process_text_query = lambda event, **kwargs: ReplyDeliverable(
        text="收到，已看到您反馈的问题。", channel_used=channel
    )
    if transport:
        engine.wechat = WechatGeweClient(base_url="https://example.test", app_id="bot")
        engine.wechat._client.close()
        engine.wechat._client = httpx.Client(
            base_url="https://example.test", transport=httpx.MockTransport(transport)
        )
    return engine


def test_fresh_callback_uses_its_own_source_and_preserves_full_original_text(tmp_path):
    engine = engine_for(tmp_path, generate_payload_vault_key())
    first = engine.parse_gewe_callback(callback(text="@李微\u2005请核实订单甲是否漏发"))
    second = engine.parse_gewe_callback(callback("8233375625312251145", str_id="per-message-B"))
    assert first.reply_source.content == "@李微\u2005请核实订单甲是否漏发"
    assert first.text == "请核实订单甲是否漏发"
    assert first.reply_source.native_ready
    assert second.reply_source.native_ready
    assert first.reply_source.str_id != second.reply_source.str_id
    assert first.reply_source.message_id != second.reply_source.message_id
    assert first.reply_source.imunion_sender_id == "456@openim"
    engine.log_group_inbound(first)
    source = engine.chat_log.recent(chat_id=ROOM)[0].metadata["reply_source"]
    assert TextQuote.from_mapping(source) == first.reply_source


def test_complete_template_escapes_text_and_keeps_opaque_metadata(tmp_path):
    engine = engine_for(tmp_path, generate_payload_vault_key())
    source = engine.parse_gewe_callback(callback(text="订单 <甲> & 乙")).reply_source
    root = ET.fromstring(source.appmsg_xml("回复 <甲> & 乙"))
    refer = root.find("refermsg")
    assert root.findtext("title") == "回复 <甲> & 乙"
    assert refer.findtext("content") == source.content
    assert refer.findtext("svrid") == MESSAGE
    assert refer.findtext("fromusr") == ROOM
    assert refer.findtext("chatusr") == "456@openim"
    assert refer.findtext("fromusername_to_imunion") == "456@openim"
    assert refer.findtext("createtime") == "1791852530"
    assert refer.findtext("strid") == source.str_id
    assert refer.findtext("msgsource") == source.msg_source
    assert root.findtext("action") == ""
    assert root.find("template_id") is not None
    assert root.find("appattach/aeskey") is not None


@pytest.mark.parametrize("channel", ["soothe", "clarify"])
def test_delayed_quote_survives_engine_restart_and_archives_platform_receipt(tmp_path, channel):
    key = generate_payload_vault_key()
    original = engine_for(tmp_path, key)
    event = original.parse_gewe_callback(callback())
    receipt = original.record_conversation_event(event)
    assert original.persist_pending_group_event(event, event_id=receipt.event_id)
    sent = []

    def transport(request):
        sent.append(request)
        return httpx.Response(200, json={"ret": 200, "data": {"newMsgId": "9233375625312251145"}})

    restarted = engine_for(tmp_path, key, transport=transport, channel=channel)
    task = restarted.conversation_state.claim_ready_tasks(
        now=datetime.now(UTC) + timedelta(minutes=31), quiet_window_seconds=1800
    )[0]
    restarted.process_conversation_task(task)
    restarted.wechat.close()
    assert len(sent) == 1
    assert sent[0].url.path.endswith("postAppMsg")
    xml = ET.fromstring(json.loads(sent[0].content)["appmsg"])
    assert xml.findtext("refermsg/svrid") == MESSAGE
    assert xml.findtext("refermsg/svrid") != receipt.event_id
    assert xml.findtext("refermsg/strid") == "per-message-A"
    assert xml.findtext("refermsg/content") == event.reply_source.content
    row = restarted.chat_log.recent(chat_id=ROOM)[0]
    assert row.quoted_msg_id == MESSAGE
    assert row.message_id == "9233375625312251145"
    assert row.metadata["text_format"] == "native_quote"
    assert restarted.conversation_state.get_task(task.task_id).state == "responded"
    with sqlite3.connect(tmp_path / "state.db") as conn:
        delivery = conn.execute(
            "SELECT text_status, message_id, reply_to_message_id "
            "FROM conversation_delivery_attempts"
        ).fetchone()
        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation_events)")}
    assert delivery == ("sent", "9233375625312251145", MESSAGE)
    assert "content" not in columns


@pytest.mark.parametrize("missing", ["msg_source", "str_id", "created_at", "message_id"])
def test_incomplete_metadata_falls_back_before_sending_once(tmp_path, missing):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"ret": 200, "data": {"newMsgId": "123"}})

    engine = engine_for(tmp_path, generate_payload_vault_key(), transport=transport)
    event = engine.parse_gewe_callback(callback())
    source = replace(event.reply_source, **{missing: 0 if missing == "created_at" else ""})
    reply = ReplyDeliverable(text="收到", channel_used="soothe", reply_source=source)
    outcome = engine.dispatch_outbound(ROOM, reply)
    engine.log_group_outbound(to_wxid=ROOM, reply=reply, outcome=outcome)
    engine.wechat.close()
    assert len(requests) == 1
    assert requests[0].url.path.endswith("postText")
    body = json.loads(requests[0].content)["content"]
    assert "请核实订单甲是否漏发" in body
    assert body.endswith("收到")
    row = engine.chat_log.recent(chat_id=ROOM)[0]
    assert row.text == body
    assert row.quoted_msg_id == ""


def test_timeout_does_not_send_a_second_reply_or_archive_an_unconfirmed_bubble(tmp_path):
    calls = []

    def transport(request):
        calls.append(request)
        raise httpx.ReadTimeout("response lost", request=request)

    engine = engine_for(tmp_path, generate_payload_vault_key(), transport=transport)
    event = engine.parse_gewe_callback(callback())
    reply = engine.process_text_query(event)
    outcome = engine.dispatch_outbound(ROOM, reply)
    engine.log_group_outbound(to_wxid=ROOM, reply=reply, outcome=outcome)
    engine.wechat.close()
    assert len(calls) == 1
    assert outcome.text_status == "unknown"
    assert not outcome.sent_text
    assert engine.chat_log.count(ROOM) == 0
    with sqlite3.connect(tmp_path / "state.db") as conn:
        assert conn.execute(
            "SELECT text_status FROM conversation_delivery_attempts"
        ).fetchone() == ("unknown",)


def test_disabled_group_uses_original_plain_reply(tmp_path):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"ret": 200})

    engine = engine_for(tmp_path, generate_payload_vault_key(), enabled=False, transport=transport)
    reply = engine.process_text_query(engine.parse_gewe_callback(callback()))
    engine.dispatch_outbound(ROOM, reply)
    engine.wechat.close()
    assert len(calls) == 1
    assert calls[0].url.path.endswith("postText")
    assert json.loads(calls[0].content)["content"] == reply.text
