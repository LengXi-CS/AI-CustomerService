"""Contact sharing is archived context, not a demand or human takeover."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from xml.sax.saxutils import escape

import httpx
import pytest

from fastgpt_assistant.adapters.wechat.client import WechatGeweClient
from fastgpt_assistant.core.chat_log import ChatLogEntry, ChatLogStore
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.orchestration.conversation_state import ConversationStateStore
from fastgpt_assistant.orchestration.engine import AssistantEngine, InboundEvent, ReplyDeliverable
from fastgpt_assistant.orchestration.payload_vault import PayloadVault, generate_payload_vault_key
from fastgpt_assistant.server import app as app_module

ROOM = "card-test@chatroom"
START = datetime(2026, 10, 16, 0, tzinfo=UTC)
CARD = '<msg username="contact-id" nickname="测试联系人" certificate="private-contact-data" />'


@pytest.fixture
def engine(tmp_path, monkeypatch):
    result = object.__new__(AssistantEngine)
    result.settings = SimpleNamespace(gewe_app_id="bot", conversation_quiet_window_seconds=0,
                                      attachment_dispatch_interval_seconds=0)
    result.conversation_state = ConversationStateStore(tmp_path / "state.db")
    result.payload_vault = PayloadVault(generate_payload_vault_key(), tmp_path / "vault.db")
    result.chat_log = ChatLogStore(tmp_path / "chat.db")
    result.groups_config = WechatGroupsConfig.from_mapping({
        "global": {"enabled": True}, "groups": [{"group_id": ROOM, "mode": "active",
            "customer_service_ids": ["owner"], "associate_customer_demands": True}],
    })
    result.model_inputs, result.queries, result.requests = [], [], []

    def split(text):
        result.model_inputs.append(text)
        return [text]

    def query(event, **kwargs):
        result.queries.append(event)
        return ReplyDeliverable(text="收到，我来核实一下。", channel_used="soothe")

    def transport(request):
        result.requests.append(request)
        return httpx.Response(200, json={"ret": 200, "data": {"newMsgId": "900001"}})

    result.model_client = SimpleNamespace(split_demands=split, associate_demands=lambda *a, **k: [])
    result._process_text_query = query
    result.query_executor = SimpleNamespace(media_base_url="")
    result.observe_public_origin = lambda **kwargs: None
    result.wechat = WechatGeweClient(base_url="https://example.test", app_id="bot")
    result.wechat._client.close()
    result.wechat._client = httpx.Client(base_url="https://example.test",
                                        transport=httpx.MockTransport(transport))
    monkeypatch.setattr(app_module, "get_engine", lambda: result)
    yield result
    result.wechat.close()


def callback(message_id="10001", *, msg_type=66, sender="customer", private=False, text=CARD):
    return {"TypeName": "AddMsg", "received_at": START.isoformat(), "Data": {
        "NewMsgId": message_id, "MsgType": msg_type, "ActualUserName": {"string": sender},
        "FromUserName": {"string": sender if private else ROOM},
        "ToUserName": {"string": "bot"}, "AtUserList": ["bot"],
        "Content": {"string": text if private else f"{sender}:\n{text}"},
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", [42, 66])
@pytest.mark.parametrize("sender", ["customer", "owner"])
async def test_card_callbacks_do_not_create_tasks_or_takeover_even_when_mentioned(
    engine, msg_type, sender,
):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app),
                                  base_url="http://test") as client:
        for index in range(3):
            payload = callback(str(10001 + index), msg_type=msg_type, sender=sender)
            response = await client.post("/api/wechat/callback", json=payload)
            assert response.json()["reason"] == "contact_card"
    assert engine.conversation_state.count_events(ROOM) == 0
    assert engine.model_inputs == engine.requests == engine.queries == []
    rows = engine.chat_log.recent(chat_id=ROOM)
    assert len(rows) == 3 and all(row.kind == "other" for row in rows)
    rendered = engine._group_context_for(ROOM).format_for_message(
        message_id="10003", at=START + timedelta(seconds=5),
    )
    assert "名片" in rendered and "附件消息" not in rendered
    assert "private-contact-data" not in rendered
    assert engine.conversation_state.issues.pending_staff_reviews(
        [ROOM], now=START.isoformat()
    ) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", [42, 66])
async def test_private_card_does_not_enter_the_direct_query_path(engine, msg_type):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app),
                                  base_url="http://test") as client:
        response = await client.post("/api/wechat/callback", json=callback(
            msg_type=msg_type, private=True,
        ))
    assert response.json()["reason"] == "contact_card"
    assert engine.model_inputs == engine.requests == engine.queries == []


@pytest.mark.asyncio
async def test_real_text_after_cards_is_the_only_demand_and_only_reply(engine):
    text = "请核实苏州仓抵扣金额"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app),
                                  base_url="http://test") as client:
        for index in range(3):
            await client.post("/api/wechat/callback", json=callback(str(10001 + index)))
        response = await client.post("/api/wechat/callback", json=callback(
            "10004", msg_type=1, text=text,
        ))
    assert response.json()["msg"] == "queued"
    engine.reconcile_customer_issues(now=START + timedelta(seconds=5))
    tasks = engine.conversation_state.claim_ready_tasks(now=START + timedelta(seconds=10),
                                                        quiet_window_seconds=0)
    assert len(tasks) == 1
    engine.process_conversation_task(tasks[0])
    assert engine.model_inputs == [text]
    assert len(engine.queries) == len(engine.requests) == 1
    assert engine.queries[0].text == text


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", ["customer", "owner"])
async def test_card_sharing_does_not_reset_a_real_wait_or_take_over_its_issue(engine, sender):
    real, _ = legacy_task(engine, "waiting-text", msg_type=1, text="请核实苏州仓抵扣金额")
    issue = engine.conversation_state.issues.assign(real.task_id)
    payload = callback(sender=sender)
    payload["received_at"] = (START + timedelta(seconds=30)).isoformat()
    payload["Data"].pop("AtUserList")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app),
                                  base_url="http://test") as client:
        response = await client.post("/api/wechat/callback", json=payload)
    assert response.json()["reason"] == "contact_card"
    task = engine.conversation_state.get_task(real.task_id)
    assert task.state == "waiting_quiet" and task.last_staff_at is None
    assert task.latest_customer_at == START.isoformat()
    assert engine.conversation_state.issues.get(real.task_id) == issue


def legacy_task(engine, message_id, *, msg_type=66, text=CARD, seconds=0):
    at = START + timedelta(seconds=seconds)
    receipt = engine.conversation_state.record_customer_message(
        chat_id=ROOM, customer_id="customer", external_event_id=message_id,
        received_at=at, text=text,
    )
    event = InboundEvent(message_id=message_id, from_user=ROOM, to_user="bot", chat_id=ROOM,
                         user_id="customer", sender_id="customer", sender_role="customer",
                         is_group=True, msg_type=msg_type, text=text,
                         raw_payload={"received_at": at.isoformat()})
    engine.persist_pending_group_event(event, event_id=receipt.event_id)
    return receipt, event


@pytest.mark.parametrize("associated", [False, True])
def test_legacy_queued_cards_are_closed_without_planning_or_sending(engine, associated):
    receipt, event = legacy_task(engine, "old-card")
    if associated:
        engine.conversation_state.issues.assign(receipt.task_id)
        claimed = engine.conversation_state.claim_ready_tasks(now=START, quiet_window_seconds=0)
        engine.process_conversation_task(claimed[0])
    else:
        engine.reconcile_customer_issues(now=START)
    assert engine.conversation_state.get_task(receipt.task_id).state == "closed"
    assert engine.queries == engine.requests == []
    assert not engine.prepare_customer_event(event).accepted
    assert not engine.record_conversation_event(event).accepted


@pytest.mark.parametrize("card_first", [False, True])
def test_legacy_mixed_issue_keeps_the_real_request_and_its_quote_source(engine, card_first):
    text = "请核实苏州仓抵扣金额"
    card, _ = legacy_task(engine, "old-card", seconds=0 if card_first else 1)
    real, _ = legacy_task(engine, "real-text", msg_type=1, text=text,
                          seconds=1 if card_first else 0)
    issue = engine.conversation_state.issues.assign(card.task_id if card_first else real.task_id)
    engine.conversation_state.issues.assign(real.task_id if card_first else card.task_id,
                                           target_id=issue["issue_id"], relation="continue")
    for _ in range(2):
        for task in engine.conversation_state.claim_ready_tasks(
            now=START + timedelta(seconds=5), quiet_window_seconds=0,
        ):
            engine.process_conversation_task(task)
    assert engine.conversation_state.get_task(card.task_id).state == "closed"
    assert engine.conversation_state.get_task(real.task_id).state == "responded"
    assert len(engine.queries) == len(engine.requests) == 1
    assert engine.queries[0].text == text
    assert engine.queries[0].message_id == "real-text"


def test_answered_legacy_cards_cannot_consume_a_later_real_demand_by_deduplication(engine):
    old, _ = legacy_task(engine, "answered-card")
    issue = engine.conversation_state.issues.assign(old.task_id)
    engine.conversation_state.set_state(old.task_id, "responded")
    with engine.conversation_state._connection() as connection:
        connection.execute("UPDATE conversation_issues SET soothe_status='sent' WHERE issue_id=?",
                           (issue["issue_id"],))
    real, _ = legacy_task(engine, "new-text", msg_type=1, text="请核实苏州仓抵扣金额", seconds=10)
    engine.reconcile_customer_issues(now=START + timedelta(seconds=15))
    assert engine.conversation_state.issues.get(real.task_id)["issue_id"] != issue["issue_id"]
    assert engine.conversation_state.get_task(real.task_id).state == "waiting_quiet"


def test_historical_archive_cards_are_labeled_even_when_old_kind_was_text(engine):
    engine.chat_log.record(ChatLogEntry(
        chat_id=ROOM, chat_name="测试群", message_id="historical-card", direction="inbound",
        sender_id="customer", sender_role="customer", kind="text", msg_type=66,
        text=CARD, occurred_at=START,
    ))
    rendered = engine._group_context_for(ROOM).format_for_message(
        message_id="historical-card", at=START + timedelta(seconds=5),
    )
    assert "[企业微信名片]" in rendered
    assert "附件消息" not in rendered and "private-contact-data" not in rendered


def test_pending_legacy_card_is_not_recreated_as_a_demand_after_restart(engine):
    parsed = engine.parse_gewe_callback(callback())
    receipt = engine.conversation_state.begin_customer_message(
        chat_id=ROOM, customer_id="customer", external_event_id=parsed.message_id,
        received_at=START, text=CARD,
    )
    engine.persist_pending_group_event(parsed, event_id=receipt.event_id)
    engine.conversation_state = ConversationStateStore(engine.conversation_state.db_path)
    engine.recover_pending_customer_events()
    assert engine.conversation_state.get_open_task(ROOM) is None
    assert not engine.conversation_state.has_pending_customer_events(ROOM)
    assert engine.requests == engine.queries == []


@pytest.mark.asyncio
async def test_explicit_text_quoting_a_card_still_creates_a_real_demand(engine):
    text = "请联系这位负责人核实苏州仓抵扣金额"
    quote = ('<msg><appmsg><title>' + text + '</title><type>57</type>'
             '<refermsg><type>66</type><svrid>10001</svrid><displayname>客户</displayname>'
             '<content>' + escape(CARD) + '</content></refermsg></appmsg></msg>')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app),
                                  base_url="http://test") as client:
        response = await client.post("/api/wechat/callback", json=callback(
            "10002", msg_type=49, text=quote,
        ))
    assert response.json()["msg"] == "queued"
    engine.reconcile_customer_issues(now=START + timedelta(seconds=5))
    task = engine.conversation_state.claim_ready_tasks(now=START + timedelta(seconds=5),
                                                       quiet_window_seconds=0)[0]
    engine.process_conversation_task(task)
    assert engine.model_inputs == [text]
    assert engine.queries[0].text == text
    assert engine.queries[0].quoted_text == "[企业微信名片]"
    assert len(engine.requests) == 1
