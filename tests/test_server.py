"""Integration tests for FastAPI application endpoints."""

from pathlib import Path

from fastapi.testclient import TestClient

from fastgpt_assistant.core.chat_log import ChatLogStore
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
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
