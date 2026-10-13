"""Integration tests for FastAPI application endpoints."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fastgpt_assistant.core.chat_log import ChatLogStore
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.orchestration.conversation_state import ConversationStateStore
from fastgpt_assistant.orchestration.engine import ReplyDeliverable
from fastgpt_assistant.server import app as app_module
from fastgpt_assistant.server.app import app

client = TestClient(app)

ARCHIVE_GROUP = "19990001122@chatroom"


def test_health_endpoint() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "database" in data
    assert data["database"]["exists"] is True


def test_health_endpoint_reports_the_chat_archive() -> None:
    payload = client.get("/health").json()

    assert payload["chat_log"]["enabled"] is True
    assert payload["chat_log"]["path"].endswith("chat-log.db")


def test_wechat_callback_ignored_when_not_add_msg() -> None:
    payload = {
        "TypeName": "OtherEvent",
        "Data": {},
    }
    response = client.post("/api/wechat/callback", json=payload)
    assert response.status_code == 200
    assert response.json()["msg"] == "ignored"


def test_wechat_callback_buffers_image() -> None:
    payload = {
        "TypeName": "AddMsg",
        "AppId": "wx_test_app",
        "Data": {
            "MsgId": 998877,
            "NewMsgId": 9988776655,
            "FromUserName": {"string": "user_wechat_123"},
            "ToUserName": {"string": "bot_wechat_id"},
            "MsgType": 3,  # Image
            "Content": {"string": ""},
        },
    }
    response = client.post("/api/wechat/callback", json=payload)
    assert response.status_code == 200
    assert response.json()["msg"] == "image_buffered"


def test_a_configured_group_message_is_archived_at_the_webhook(
    monkeypatch, tmp_path: Path
) -> None:
    """Wiring, not just the engine: the callback archives *before* it routes.

    The group requires an @-mention and the message has none, so the event is
    dropped — which conveniently means this test writes nothing into the running
    service's conversation-state or payload-vault databases. The one thing it
    asserts on is the archive row that a dropped message must still produce.
    """
    engine = app_module.get_engine()
    store = ChatLogStore(tmp_path / "chat-log.db")
    monkeypatch.setattr(engine, "chat_log", store, raising=False)
    # ``_groups_override`` pins a policy without touching the on-disk TOML, and
    # monkeypatch restores it afterwards.
    monkeypatch.setattr(
        engine,
        "_groups_override",
        WechatGroupsConfig.from_mapping(
            {
                "global": {"enabled": True},
                "groups": [
                    {
                        "group_id": ARCHIVE_GROUP,
                        "group_name": "归档测试群",
                        "mode": "active",
                        "require_customer_mention": True,
                    }
                ],
            }
        ),
        raising=False,
    )
    payload = {
        "TypeName": "AddMsg",
        "Data": {
            "NewMsgId": "archive-msg-1",
            "FromUserName": {"string": ARCHIVE_GROUP},
            "ActualUserName": {"string": "customer-xyz"},
            "ToUserName": {"string": "bot_wechat_id"},
            "MsgType": 1,
            "Content": {"string": "查一下 SKU"},
        },
    }

    response = client.post("/api/wechat/callback", json=payload)

    assert response.json()["reason"] == "customer_not_mentioned"
    rows = store.recent(chat_id=ARCHIVE_GROUP)
    assert [(row.direction, row.text, row.kind) for row in rows] == [
        ("inbound", "查一下 SKU", "text")
    ]
    assert rows[0].chat_name == "归档测试群"


def test_revoke_callback_cancels_work_without_creating_a_system_task(
    monkeypatch, tmp_path: Path
) -> None:
    engine = app_module.get_engine()
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    original = store.record_customer_message(
        chat_id=ARCHIVE_GROUP,
        customer_id="customer-xyz",
        external_event_id="6647819121452690899",
        received_at="2026-09-28T09:02:48+00:00",
        text="经理审下单",
    )
    archive = ChatLogStore(tmp_path / "chat-log.db")
    monkeypatch.setattr(engine, "conversation_state", store)
    monkeypatch.setattr(engine, "chat_log", archive)
    monkeypatch.setattr(
        engine,
        "_groups_override",
        WechatGroupsConfig.from_mapping(
            {"global": {"enabled": True}, "groups": [{"group_id": ARCHIVE_GROUP}]}
        ),
    )
    payload = {
        "TypeName": "AddMsg",
        "Data": {
            "NewMsgId": "revoke-1",
            "FromUserName": {"string": ARCHIVE_GROUP},
            "ToUserName": {"string": "bot_wechat_id"},
            "MsgType": 10002,
            "Content": {
                "string": '<sysmsg type="revokemsg"><revokemsg>'
                '<newmsgid>6647819121452690899</newmsgid>'
                "</revokemsg></sysmsg>"
            },
        },
    }

    response = client.post("/api/wechat/callback", json=payload)

    assert response.json()["reason"] == "system_message"
    assert store.get_task(original.task_id).state == "closed"
    assert store.count_events(ARCHIVE_GROUP) == 1
    assert archive.recent(chat_id=ARCHIVE_GROUP) == []


def test_staff_mentioning_customer_takes_over_related_open_tasks(
    monkeypatch, tmp_path: Path
) -> None:
    engine = app_module.get_engine()
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    first = store.record_customer_message(
        chat_id=ARCHIVE_GROUP,
        customer_id="customer-xyz",
        external_event_id="order-1",
        received_at="2026-09-28T09:02:47+00:00",
        text="杭州仓开下单",
    )
    second = store.record_customer_message(
        chat_id=ARCHIVE_GROUP,
        customer_id="customer-xyz",
        external_event_id="review-1",
        received_at="2026-09-28T09:02:56+00:00",
        text="经理审下单",
    )
    monkeypatch.setattr(engine, "conversation_state", store)
    monkeypatch.setattr(engine, "chat_log", ChatLogStore(tmp_path / "chat-log.db"))
    monkeypatch.setattr(
        engine,
        "_groups_override",
        WechatGroupsConfig.from_mapping(
            {
                "global": {"enabled": True},
                "groups": [
                    {
                        "group_id": ARCHIVE_GROUP,
                        "mode": "active",
                        "owner_id": "staff-xyz",
                        "bot_aliases": ["瀚思商品助理"],
                    }
                ],
            }
        ),
    )
    payload = {
        "TypeName": "AddMsg",
        "Data": {
            "NewMsgId": "staff-review-1",
            "FromUserName": {"string": ARCHIVE_GROUP},
            "ActualUserName": {"string": "staff-xyz"},
            "ToUserName": {"string": "bot_wechat_id"},
            "MsgType": 1,
            "Content": {"string": "@刘英豪\u2005无误已审核"},
            "AtUserNames": ["刘英豪"],
        },
    }

    response = client.post("/api/wechat/callback", json=payload)

    assert response.json()["reason"] == "staff_not_mentioned"
    assert store.get_task(first.task_id).state == "human_active"
    assert store.get_task(second.task_id).state == "human_active"
    assert store.claim_ready_tasks(
        now="2026-09-28T10:00:00+00:00", quiet_window_seconds=120
    ) == []


@pytest.mark.parametrize("ambiguous_human_tasks", [False, True])
def test_staff_mentioning_bot_answers_immediately_without_unique_task(
    monkeypatch, tmp_path: Path, ambiguous_human_tasks: bool
) -> None:
    engine = app_module.get_engine()
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    customer_task_ids: list[str] = []
    if ambiguous_human_tasks:
        for index in range(2):
            receipt = store.record_customer_message(
                chat_id=ARCHIVE_GROUP,
                customer_id="customer-xyz",
                external_event_id=f"customer-{index}",
                received_at=f"2026-09-29T05:00:0{index}+00:00",
                text=f"客户需求 {index}",
            )
            customer_task_ids.append(receipt.task_id)
        store.record_staff_message(
            chat_id=ARCHIVE_GROUP,
            staff_id="staff-xyz",
            external_event_id="staff-takeover",
            received_at="2026-09-29T05:01:00+00:00",
            text="我来处理",
            mentioned_bot=False,
        )

    monkeypatch.setattr(engine, "conversation_state", store)
    monkeypatch.setattr(engine, "chat_log", ChatLogStore(tmp_path / "chat-log.db"))
    monkeypatch.setattr(
        engine,
        "_groups_override",
        WechatGroupsConfig.from_mapping(
            {
                "global": {"enabled": True, "reply_to_staff_when_mentioned": True},
                "groups": [
                    {
                        "group_id": ARCHIVE_GROUP,
                        "mode": "active",
                        "owner_id": "staff-xyz",
                        "bot_aliases": ["瀚思商品助理"],
                        "reply_to_staff_when_mentioned": True,
                    }
                ],
            }
        ),
    )
    processed: list[tuple[str, str]] = []
    sent: list[str] = []

    def process(event):
        processed.append((event.text, event.conversation_task_id))
        return ReplyDeliverable(text="这是商品与工厂信息", channel_used="structured")

    def dispatch(*, to_wxid, reply):
        sent.append(to_wxid)
        return None

    monkeypatch.setattr(engine, "process_text_query", process)
    monkeypatch.setattr(engine, "dispatch_outbound", dispatch)
    monkeypatch.setattr(engine, "log_group_outbound", lambda **kwargs: None)
    payload = {
        "TypeName": "AddMsg",
        "Data": {
            "NewMsgId": "staff-ask-1",
            "FromUserName": {"string": ARCHIVE_GROUP},
            "ActualUserName": {"string": "staff-xyz"},
            "ToUserName": {"string": "bot_wechat_id"},
            "MsgType": 1,
            "Content": {
                "string": "@瀚思商品助理\u2005查询商品 6976070461130 的信息与关联工厂"
            },
            "AtUserNames": ["瀚思商品助理"],
        },
    }

    response = client.post("/api/wechat/callback", json=payload)
    duplicate = client.post("/api/wechat/callback", json=payload)

    assert response.json()["msg"] == "replied"
    assert processed == [("查询商品 6976070461130 的信息与关联工厂", "")]
    assert sent == [ARCHIVE_GROUP]
    assert duplicate.json()["reason"] == "duplicate_event"
    assert all(store.get_task(task_id).state == "human_active" for task_id in customer_task_ids)
