from types import SimpleNamespace

from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.orchestration.engine import AssistantEngine

GROUP_ID = "room@chatroom"
OWNER_ID = "staff-wxid"


def _engine() -> AssistantEngine:
    engine = object.__new__(AssistantEngine)
    engine.settings = SimpleNamespace(gewe_app_id="bot-wxid")
    engine.groups_config = WechatGroupsConfig.from_mapping(
        {
            "global": {"enabled": True},
            "groups": [
                {
                    "group_id": GROUP_ID,
                    "mode": "active",
                    "owner_id": OWNER_ID,
                    "bot_aliases": ["AI客服"],
                }
            ],
        }
    )
    return engine


def _message(content: str, *, group_id: str = GROUP_ID) -> dict[str, object]:
    return {
        "TypeName": "AddMsg",
        "Data": {
            "NewMsgId": "msg-001",
            "FromUserName": {"string": group_id},
            "ToUserName": {"string": "bot-wxid"},
            "MsgType": 1,
            "Content": {"string": content},
        },
    }


def test_group_event_uses_sender_prefix_and_recognizes_staff_mention() -> None:
    event = _engine().parse_gewe_callback(
        _message(f"{OWNER_ID}:\n@AI客服\u2005查一下 SKU")
    )

    assert event is not None
    assert event.is_group is True
    assert event.chat_id == GROUP_ID
    assert event.sender_id == OWNER_ID
    assert event.user_id == OWNER_ID
    assert event.sender_role == "staff"
    assert event.mentioned_bot is True
    assert event.text == "查一下 SKU"


def test_staff_without_mention_is_silent_but_staff_mention_is_allowed() -> None:
    engine = _engine()
    ordinary = engine.parse_gewe_callback(_message(f"{OWNER_ID}:\n内部进展"))
    explicit = engine.parse_gewe_callback(_message(f"{OWNER_ID}:\n@AI客服 查一下 SKU"))

    assert ordinary is not None and explicit is not None
    assert engine.route_group_event(ordinary).allowed is False
    assert engine.route_group_event(ordinary).reason == "staff_not_mentioned"
    assert engine.route_group_event(explicit).allowed is True


def test_unconfigured_group_fails_closed() -> None:
    event = _engine().parse_gewe_callback(
        _message("customer-wxid:\n查一下 SKU", group_id="other@chatroom")
    )

    assert event is not None
    route = _engine().route_group_event(event)
    assert route.allowed is False
    assert route.reason == "group_not_configured"


def test_private_message_keeps_legacy_route() -> None:
    event = _engine().parse_gewe_callback(_message("查询", group_id="customer-wxid"))

    assert event is not None
    assert event.is_group is False
    assert event.sender_id == "customer-wxid"
    assert event.chat_id == "customer-wxid"
    assert _engine().route_group_event(event).allowed is True
