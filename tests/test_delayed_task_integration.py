from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastgpt_assistant.core.chat_log import ChatLogStore
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.orchestration.conversation_state import ConversationStateStore
from fastgpt_assistant.orchestration.engine import (
    AssistantEngine,
    DispatchOutcome,
    InboundEvent,
    ReplyDeliverable,
)
from fastgpt_assistant.orchestration.payload_vault import (
    PayloadVault,
    generate_payload_vault_key,
)


def test_delayed_task_restores_encrypted_text_and_completes(tmp_path) -> None:
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    vault = PayloadVault(
        key=generate_payload_vault_key(),
        db_path=tmp_path / "payload-vault.db",
    )
    settings = SimpleNamespace(gewe_app_id="bot-wxid")
    engine = object.__new__(AssistantEngine)
    engine.settings = settings
    engine.conversation_state = store
    engine.payload_vault = vault
    engine.groups_config = WechatGroupsConfig.from_mapping(
        {
            "global": {"enabled": True},
            "groups": [{"group_id": "room@chatroom", "mode": "active"}],
        }
    )
    captured: list[tuple[str, str, str, str, str]] = []
    outbound: list[str] = []
    def fake_process(event: InboundEvent) -> ReplyDeliverable:
        captured.append(
            (
                event.text,
                event.quoted_text,
                event.quoted_type,
                event.quoted_media_xml,
                event.quoted_msg_id,
            )
        )
        return ReplyDeliverable(text="已处理")

    engine.process_text_query = fake_process

    def fake_dispatch(*, to_wxid, reply) -> DispatchOutcome:
        outbound.append(f"{to_wxid}:{reply.text}")
        return DispatchOutcome(sent_text=True)

    engine.dispatch_outbound = fake_dispatch
    engine.chat_log = ChatLogStore(tmp_path / "chat-log.db")

    event = InboundEvent(
        message_id="wechat-msg-1",
        from_user="room@chatroom",
        to_user="bot-wxid",
        chat_id="room@chatroom",
        user_id="customer-a",
        sender_id="customer-a",
        sender_role="customer",
        is_group=True,
        msg_type=1,
        text="查询 SKU 6976070460164",
        quoted_text="这两款检测报告麻烦提供下",
        quoted_type="image",
        quoted_media_xml="<img>encrypted-quote-payload</img>",
        quoted_msg_id="quoted-wechat-msg-1",
    )
    receipt = engine.record_conversation_event(event)
    assert engine.persist_pending_group_event(event, event_id=receipt.event_id) is True

    task = store.claim_ready_tasks(
        now=datetime.now(UTC) + timedelta(seconds=121),
        quiet_window_seconds=120,
    )[0]
    assert task.task_id == receipt.task_id

    engine.process_conversation_task(task)

    assert captured == [
        (
            "查询 SKU 6976070460164",
            "这两款检测报告麻烦提供下",
            "image",
            "<img>encrypted-quote-payload</img>",
            "quoted-wechat-msg-1",
        )
    ]
    assert outbound == ["room@chatroom:已处理"]
    assert store.get_task(task.task_id).state == "responded"
    assert vault.retrieve_text(receipt.event_id) is not None
    # The delayed path must also leave the sent reply in the group's chat log,
    # so a replay shows the same conversation the group actually saw.
    assert [
        (row.direction, row.sender_role, row.text)
        for row in engine.chat_log.recent(chat_id="room@chatroom")
    ] == [("outbound", "assistant", "已处理")]


def test_demand_bundle_queries_each_independent_demand_once() -> None:
    engine = object.__new__(AssistantEngine)
    captured: list[str] = []

    def fake_process(event: InboundEvent) -> ReplyDeliverable:
        captured.append(event.text)
        return ReplyDeliverable(text=f"答复：{event.text}", channel_used="structured")

    engine.process_text_query = fake_process
    event = InboundEvent(
        message_id="msg-1",
        from_user="room@chatroom",
        to_user="bot-wxid",
        chat_id="room@chatroom",
        user_id="customer-a",
        sender_id="customer-a",
        is_group=True,
        msg_type=1,
    )

    reply = engine.process_demand_bundle(event, ["查 SKU 甲；查 SKU 乙", "还有这个产品的工厂"])

    assert captured == ["查 SKU 甲", "查 SKU 乙\n还有这个产品的工厂"]
    assert reply.channel_used == "multi"
    assert "答复：查 SKU 甲" in reply.text
    assert "答复：查 SKU 乙\n还有这个产品的工厂" in reply.text
