from pathlib import Path
from types import SimpleNamespace

import pytest

from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig, WechatGroupsProvider
from fastgpt_assistant.orchestration.engine import AssistantEngine
from fastgpt_assistant.orchestration.inbound import revoked_message_id

GROUP_ID = "room@chatroom"
OWNER_ID = "staff-wxid"


def _toml(
    *group_ids: str,
    enabled: bool = True,
    soothe_mode: str | None = None,
) -> str:
    """Build a minimal group policy file for the reload tests."""
    header = f"[global]\nenabled = {'true' if enabled else 'false'}\n"
    if soothe_mode is not None:
        header += f'soothe_mode = "{soothe_mode}"\n'
    blocks = "".join(
        f'[[groups]]\ngroup_id = "{group_id}"\nmode = "active"\n' for group_id in group_ids
    )
    return header + blocks


def _file_backed_engine(path: Path) -> AssistantEngine:
    """Engine wired to a real file so reload behaviour can be exercised."""
    engine = object.__new__(AssistantEngine)
    engine.settings = SimpleNamespace(gewe_app_id="bot-wxid")
    engine._groups_override = None
    engine._groups_provider = WechatGroupsProvider(path)
    return engine


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


def test_staff_mentioning_a_customer_does_not_handoff_to_the_bot() -> None:
    engine = _engine()
    payload = _message(f"{OWNER_ID}:\n@刘英豪\u2005无误已审核")
    payload["Data"]["AtUserNames"] = ["刘英豪"]

    event = engine.parse_gewe_callback(payload)

    assert event is not None
    assert event.text == "无误已审核"
    assert event.mentions == ("刘英豪",)
    assert event.mentioned_bot is False
    assert engine.route_group_event(event).reason == "staff_not_mentioned"


def test_revoke_system_message_identifies_the_original_event() -> None:
    xml = (
        '<sysmsg type="revokemsg"><revokemsg><newmsgid>6647819121452690899</newmsgid>'
        '</revokemsg></sysmsg>'
    )

    assert revoked_message_id(xml) == "6647819121452690899"
    assert revoked_message_id("<sysmsg>") == ""
    assert revoked_message_id('<sysmsg type="other"/>') == ""


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


def test_provider_picks_up_a_newly_added_group(tmp_path: Path) -> None:
    path = tmp_path / "wechat_groups.toml"
    path.write_text(_toml("room-a@chatroom"), encoding="utf-8")
    provider = WechatGroupsProvider(path)

    assert set(provider.current().groups or {}) == {"room-a@chatroom"}

    path.write_text(_toml("room-a@chatroom", "room-b@chatroom"), encoding="utf-8")

    assert set(provider.current().groups or {}) == {"room-a@chatroom", "room-b@chatroom"}


def test_provider_reuses_the_parsed_config_while_the_file_is_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wechat_groups.toml"
    path.write_text(_toml("room-a@chatroom"), encoding="utf-8")
    provider = WechatGroupsProvider(path)

    assert provider.current() is provider.current()


def test_provider_keeps_last_good_config_when_the_file_is_broken(tmp_path: Path) -> None:
    path = tmp_path / "wechat_groups.toml"
    path.write_text(_toml("room-a@chatroom"), encoding="utf-8")
    provider = WechatGroupsProvider(path)
    good = provider.current()

    path.write_text("[[groups]]\ngroup_id = \n", encoding="utf-8")

    assert provider.current() is good
    assert set(provider.current().groups or {}) == {"room-a@chatroom"}


def test_provider_fails_closed_when_the_file_is_absent(tmp_path: Path) -> None:
    provider = WechatGroupsProvider(tmp_path / "missing.toml")

    config = provider.current()

    assert config.enabled is False
    assert config.groups == {}
    assert provider.current() is config


def test_engine_groups_config_follows_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "wechat_groups.toml"
    path.write_text(_toml("room-a@chatroom"), encoding="utf-8")
    engine = _file_backed_engine(path)

    assert engine.groups_config.get("room-a@chatroom") is not None
    assert engine.groups_config.get("room-b@chatroom") is None

    path.write_text(_toml("room-a@chatroom", "room-b@chatroom"), encoding="utf-8")

    assert engine.groups_config.get("room-b@chatroom") is not None


def test_group_added_at_runtime_is_routed_without_a_restart(tmp_path: Path) -> None:
    other_group = "other@chatroom"
    path = tmp_path / "wechat_groups.toml"
    path.write_text(_toml(GROUP_ID), encoding="utf-8")
    engine = _file_backed_engine(path)

    event = engine.parse_gewe_callback(_message("customer-wxid:\n查一下 SKU", group_id=other_group))
    assert event is not None
    assert engine.route_group_event(event).reason == "group_not_configured"

    path.write_text(_toml(GROUP_ID, other_group), encoding="utf-8")

    assert engine.route_group_event(event).allowed is True


def test_assigning_groups_config_pins_the_supplied_configuration(tmp_path: Path) -> None:
    path = tmp_path / "wechat_groups.toml"
    path.write_text(_toml("room-a@chatroom"), encoding="utf-8")
    engine = _file_backed_engine(path)

    pinned = WechatGroupsConfig.from_mapping({"global": {"enabled": True}})
    engine.groups_config = pinned

    assert engine.groups_config is pinned


# --------------------------------------------------------------------------- #
# soothing-wording mode
# --------------------------------------------------------------------------- #
def test_soothe_mode_defaults_to_model_generated_wording() -> None:
    """An existing deployment must not silently switch wording on upgrade."""
    config = WechatGroupsConfig()

    assert config.soothe_mode == "model"
    assert config.soothe_fixed_reply == "收到，我们加急处理"


def test_soothe_mode_and_wording_are_read_from_the_global_section() -> None:
    config = WechatGroupsConfig.from_mapping(
        {
            "global": {
                "enabled": True,
                "soothe_mode": "fixed",
                "soothe_fixed_reply": "收到，我们加急处理",
            }
        }
    )

    assert config.soothe_mode == "fixed"
    assert config.soothe_fixed_reply == "收到，我们加急处理"


def test_an_unknown_soothe_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        WechatGroupsConfig.from_mapping({"global": {"soothe_mode": "sometimes"}})


def test_an_empty_fixed_wording_falls_back_to_the_default_sentence() -> None:
    config = WechatGroupsConfig.from_mapping(
        {"global": {"soothe_mode": "fixed", "soothe_fixed_reply": "   "}}
    )

    assert config.soothe_fixed_reply == "收到，我们加急处理"


def test_soothe_mode_is_hot_reloaded_with_the_group_file(tmp_path: Path) -> None:
    path = tmp_path / "wechat_groups.toml"
    path.write_text(_toml(GROUP_ID, soothe_mode="model"), encoding="utf-8")
    provider = WechatGroupsProvider(path)

    assert provider.current().soothe_mode == "model"

    path.write_text(_toml(GROUP_ID, soothe_mode="fixed"), encoding="utf-8")

    assert provider.current().soothe_mode == "fixed"


def test_a_broken_file_keeps_the_last_good_soothe_mode(tmp_path: Path) -> None:
    path = tmp_path / "wechat_groups.toml"
    path.write_text(_toml(GROUP_ID, soothe_mode="fixed"), encoding="utf-8")
    provider = WechatGroupsProvider(path)
    good = provider.current()

    path.write_text("[global]\nsoothe_mode = \n", encoding="utf-8")

    assert provider.current() is good
    assert provider.current().soothe_mode == "fixed"
