"""Regressions for quoted staff replies in groups with several customers."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fastgpt_assistant.core.chat_log import ChatLogStore
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.orchestration.conversation_state import ConversationStateStore
from fastgpt_assistant.server import app as app_module

ROOM = "quote-takeover@chatroom"
START = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def customer(store, message_id, *, sender="customer-b", seconds=0, chat=ROOM, count=1):
    return store.record_customer_message(
        chat_id=chat,
        customer_id=sender,
        external_event_id=message_id,
        received_at=START + timedelta(seconds=seconds),
        text=message_id,
        demand_count=count,
    )


def staff(store, quote, *, mentioned_bot=False):
    return store.record_staff_message(
        chat_id=ROOM,
        staff_id="staff",
        external_event_id="staff-reply",
        received_at=START + timedelta(seconds=120),
        text="订单金额8656.7，一致",
        mentioned_bot=mentioned_bot,
        quoted_msg_id=quote,
    )


def test_quoted_order_callback_takes_over_customer_despite_other_human_task(
    monkeypatch, tmp_path
):
    """Replay the order file + verification text + quoted staff reply incident."""
    store = ConversationStateStore(tmp_path / "state.db")
    other = customer(store, "licence-request", sender="customer-a")
    store.set_state(other.task_id, "human_active")
    order = customer(store, "order-file", seconds=60)
    verification = customer(store, "verify-order", seconds=61)
    # The same takeover must also invalidate work already claimed by the poller.
    store.set_state(verification.task_id, "processing")
    newer = customer(store, "new-demand", seconds=180)

    engine = app_module.get_engine()
    monkeypatch.setattr(engine, "conversation_state", store)
    monkeypatch.setattr(engine, "payload_vault", None)
    monkeypatch.setattr(engine, "chat_log", ChatLogStore(tmp_path / "chat-log.db"))
    monkeypatch.setattr(
        engine,
        "_groups_override",
        WechatGroupsConfig.from_mapping(
            {
                "global": {"enabled": True},
                "groups": [{"group_id": ROOM, "mode": "active", "owner_id": "staff"}],
            }
        ),
    )
    monkeypatch.setattr(
        "fastgpt_assistant.orchestration.engine.event_occurred_at",
        lambda event: START + timedelta(seconds=120),
    )
    payload = {
        "TypeName": "AddMsg",
        "Data": {
            "NewMsgId": "staff-reply",
            "FromUserName": {"string": ROOM},
            "ActualUserName": {"string": "staff"},
            "ToUserName": {"string": "bot"},
            "MsgType": 49,
            "MsgSource": {"string": "<msgsource><test>original</test></msgsource>"},
            "Content": {
                "string": "<msg><appmsg><title>订单金额8656.7，一致</title><type>57</type>"
                "<refermsg><type>0</type><svrid>order-file</svrid>"
                "<displayname>凌爱爱</displayname></refermsg></appmsg></msg>"
            },
        },
    }
    client = TestClient(app_module.app)
    response = client.post("/api/wechat/callback", json=payload)
    assert response.json()["reason"] == "staff_not_mentioned"
    archived = engine.chat_log.recent(chat_id=ROOM)[0]
    assert archived.text == "订单金额8656.7，一致"
    assert archived.metadata["raw_quote_xml"] == payload["Data"]["Content"]["string"]
    assert archived.metadata["raw_quote_msg_source"] == payload["Data"]["MsgSource"]["string"]
    for receipt in (order, verification):
        task = store.get_task(receipt.task_id)
        assert task.state == "human_active"
        assert task.last_staff_at == (START + timedelta(seconds=120)).isoformat()
    assert store.get_task(other.task_id).last_staff_at is None
    assert store.get_task(newer.task_id).state == "waiting_quiet"

    version = store.get_task(order.task_id).version
    client.post("/api/wechat/callback", json=payload)
    assert store.get_task(order.task_id).version == version
    claimed = store.claim_ready_tasks(
        now=START + timedelta(hours=1), quiet_window_seconds=1800
    )
    assert [task.task_id for task in claimed] == [newer.task_id]


@pytest.mark.parametrize("quote_kind", ["missing", "other_room", "closed", "staff"])
def test_unresolved_quote_never_takes_over_the_only_unrelated_customer(tmp_path, quote_kind):
    store = ConversationStateStore(tmp_path / "state.db")
    unrelated = customer(store, "unrelated", sender="customer-a")
    if quote_kind == "other_room":
        customer(store, "quoted", chat="another@chatroom")
    elif quote_kind == "closed":
        quoted = customer(store, "quoted")
        store.set_state(quoted.task_id, "closed")
    elif quote_kind == "staff":
        store.record_staff_message(
            chat_id=ROOM,
            staff_id="staff",
            external_event_id="quoted",
            received_at=START,
            text="纯告知",
            mentioned_bot=False,
            quoted_msg_id="missing",
        )
    receipt = staff(store, "quoted")
    assert receipt.task_ids == ()
    assert store.get_task(unrelated.task_id).state == "waiting_quiet"


@pytest.mark.parametrize("count", [1, 2])
def test_quote_with_bot_mention_requires_one_referenced_demand(tmp_path, count):
    store = ConversationStateStore(tmp_path / "state.db")
    quoted = customer(store, "quoted", count=count)
    sibling = customer(store, "sibling")
    other = customer(store, "other", sender="customer-a")
    for task_id in (*quoted.task_ids, sibling.task_id, other.task_id):
        store.set_state(task_id, "human_active")

    receipt = staff(store, "quoted", mentioned_bot=True)
    assert receipt.task_ids == (quoted.task_ids if count == 1 else ())
    for task_id in quoted.task_ids:
        expected = "waiting_quiet" if count == 1 else "human_active"
        assert store.get_task(task_id).state == expected
    assert store.get_task(sibling.task_id).state == "human_active"
    assert store.get_task(other.task_id).state == "human_active"


def test_ordinary_quote_takes_over_all_demand_slots_without_affecting_other_customer(tmp_path):
    store = ConversationStateStore(tmp_path / "state.db")
    quoted = customer(store, "quoted", count=2)
    other = customer(store, "other", sender="customer-a")
    receipt = staff(store, "quoted")
    assert set(receipt.task_ids) == set(quoted.task_ids)
    assert all(store.get_task(task_id).state == "human_active" for task_id in quoted.task_ids)
    assert store.get_task(other.task_id).state == "waiting_quiet"
