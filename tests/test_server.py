"""Integration tests for FastAPI application endpoints."""

from fastapi.testclient import TestClient

from fastgpt_assistant.server.app import app

client = TestClient(app)


def test_health_endpoint() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "database" in data
    assert data["database"]["exists"] is True


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
