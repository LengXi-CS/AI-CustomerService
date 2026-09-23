"""Configured-group chat archive: what it keeps, and what it must never break.

Two contracts are under test here, and the second one is the important one:

* the archive is complete enough to **replay a group's conversation** in a web
  frontend — who said what, quoting what, with which attachment, and why the bot
  answered the way it did;
* archiving is **best-effort**: it covers only configured groups, and no storage
  failure may ever stop the assistant from answering a customer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from fastgpt_assistant.core.chat_log import ChatLogEntry, ChatLogStore, message_kind
from fastgpt_assistant.core.models import Deliverable
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.orchestration.engine import (
    AssistantEngine,
    InboundEvent,
    ReplyDeliverable,
)

ROOM = "49195424680@chatroom"
OTHER_ROOM = "46079713295@chatroom"
PRIVATE = "wxid_customer"
CUSTOMER = "customer-a"
STAFF = "wxid_owner"
BOT = "bot-wxid"
GROUP_NAME = "AI客服测试（外部）"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _store(tmp_path: Path, *, enabled: bool = True) -> ChatLogStore:
    return ChatLogStore(tmp_path / "chat-log.db", enabled=enabled)


def _engine(
    tmp_path: Path,
    *,
    chat_log: ChatLogStore | None = None,
    require_mention: bool = False,
) -> AssistantEngine:
    """Engine wired only to what archiving needs, as the other suites do."""
    engine = object.__new__(AssistantEngine)
    engine.settings = SimpleNamespace(gewe_app_id=BOT)
    engine.chat_log = chat_log if chat_log is not None else _store(tmp_path)
    engine.groups_config = WechatGroupsConfig.from_mapping(
        {
            "global": {"enabled": True},
            "groups": [
                {
                    "group_id": ROOM,
                    "group_name": GROUP_NAME,
                    "mode": "active",
                    "require_customer_mention": require_mention,
                    "owner_id": STAFF,
                }
            ],
        }
    )
    return engine


def _payload(
    content: str,
    *,
    message_id: str = "msg-1",
    group: str = ROOM,
    msg_type: int = 1,
) -> dict[str, object]:
    return {
        "TypeName": "AddMsg",
        "Data": {
            "NewMsgId": message_id,
            "FromUserName": {"string": group},
            "ToUserName": {"string": BOT},
            "MsgType": msg_type,
            "Content": {"string": content},
        },
    }


def _entry(**overrides) -> ChatLogEntry:
    base = {
        "chat_id": ROOM,
        "chat_name": GROUP_NAME,
        "direction": "inbound",
        "sender_id": CUSTOMER,
        "sender_role": "customer",
        "kind": "text",
        "text": "查一下 SKU",
        "message_id": "msg-1",
    }
    base.update(overrides)
    return ChatLogEntry(**base)


# --------------------------------------------------------------------------- #
# store: append, idempotency, read-back
# --------------------------------------------------------------------------- #
def test_a_message_round_trips_with_every_replay_field(tmp_path: Path) -> None:
    store = _store(tmp_path)

    row_id = store.record(
        _entry(
            sender_name="袋鼠",
            msg_type=1,
            quoted_msg_id="quoted-1",
            quoted_type="text",
            quoted_text="这两款检测报告麻烦提供下",
            metadata={"mentions": ["AI客服"], "mentioned_bot": True},
        )
    )

    assert row_id is not None
    rows = store.recent()
    assert len(rows) == 1
    row = rows[0]
    assert row.chat_id == ROOM
    assert row.chat_name == GROUP_NAME
    assert row.sender_name == "袋鼠"
    assert row.sender_role == "customer"
    assert row.quoted_msg_id == "quoted-1"
    assert row.quoted_type == "text"
    assert row.quoted_text == "这两款检测报告麻烦提供下"
    assert row.metadata["mentions"] == ["AI客服"]
    assert row.metadata["mentioned_bot"] is True
    assert row.occurred_at


def test_a_redelivered_platform_message_is_stored_once(tmp_path: Path) -> None:
    """GeWe retries the callback; the archive must not grow a duplicate bubble."""
    store = _store(tmp_path)

    assert store.record(_entry()) is not None
    assert store.record(_entry()) is None

    assert store.count() == 1


def test_outbound_rows_are_never_deduplicated(tmp_path: Path) -> None:
    """An outbound row has no platform id, so each send is its own event."""
    store = _store(tmp_path)

    store.record(_entry(direction="outbound", sender_role="assistant", message_id=""))
    store.record(_entry(direction="outbound", sender_role="assistant", message_id=""))

    assert store.count() == 2


def test_the_same_platform_id_in_two_chats_stays_two_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.record(_entry(message_id="msg-1"))
    store.record(_entry(chat_id=OTHER_ROOM, message_id="msg-1"))

    assert store.count() == 2


def test_a_disabled_store_writes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path, enabled=False)

    assert store.record(_entry()) is None
    assert store.count() == 0


def test_a_database_failure_is_swallowed_not_raised(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def broken() -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    store._get_connection = broken  # type: ignore[method-assign]

    assert store.record(_entry()) is None


def test_list_messages_pages_forward_for_incremental_replay(tmp_path: Path) -> None:
    store = _store(tmp_path)
    row_ids = [
        store.record(_entry(message_id=f"msg-{index}", text=f"第 {index} 条"))
        for index in range(3)
    ]

    first_page = store.list_messages(ROOM, limit=2)
    second_page = store.list_messages(ROOM, after_id=row_ids[1] or 0, limit=2)

    assert [row.text for row in first_page] == ["第 0 条", "第 1 条"]
    # Keyset paging on the autoincrement id: a viewer that polls with the last id
    # it rendered re-sees nothing and skips nothing.
    assert [row.text for row in second_page] == ["第 2 条"]
    assert store.list_messages(ROOM, after_id=row_ids[2] or 0) == []


def test_summarize_reports_one_line_per_chat(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_entry())
    store.record(_entry(chat_id=OTHER_ROOM, message_id="msg-2"))

    summary = store.summarize()

    assert {item["chat_id"] for item in summary} == {ROOM, OTHER_ROOM}
    assert all(item["total"] == 1 for item in summary)
    assert all(item["last_at"] for item in summary)


def test_as_dict_is_json_ready_for_a_frontend(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_entry(metadata={"mentions": ["AI客服"]}))

    payload = store.recent()[0].as_dict()

    assert payload["sender_role"] == "customer"
    assert payload["metadata"] == {"mentions": ["AI客服"]}
    assert isinstance(payload["occurred_at"], str)
    assert set(payload) >= {"chat_id", "direction", "kind", "text", "quoted_msg_id"}


@pytest.mark.parametrize(
    ("msg_type", "expected"),
    [(3, "image"), (34, "voice"), (43, "video"), (49, "file")],
)
def test_platform_message_types_map_to_render_kinds(msg_type: int, expected: str) -> None:
    assert message_kind(msg_type) == expected


def test_an_unknown_type_without_text_is_still_typed(tmp_path: Path) -> None:
    assert message_kind(10001) == "other"
    assert message_kind(10001, has_text=True) == "text"
    # The parser's image verdict wins over the raw type.
    assert message_kind(10001, is_image=True) == "image"


# --------------------------------------------------------------------------- #
# engine: which messages reach the archive
# --------------------------------------------------------------------------- #
def test_inbound_customer_message_is_archived(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    event = engine.parse_gewe_callback(_payload(f"{CUSTOMER}:\n查一下 SKU"))
    assert event is not None

    engine.log_group_inbound(event)

    rows = engine.chat_log.recent()
    assert len(rows) == 1
    assert rows[0].text == "查一下 SKU"
    assert rows[0].sender_id == CUSTOMER
    assert rows[0].sender_role == "customer"
    assert rows[0].kind == "text"
    assert rows[0].message_id == "msg-1"
    assert rows[0].msg_type == 1
    assert rows[0].chat_name == GROUP_NAME


def test_a_message_the_policy_drops_is_still_archived(tmp_path: Path) -> None:
    """ "为什么这条没回" is answered by exactly the rows the policy rejected."""
    engine = _engine(tmp_path, require_mention=True)
    event = engine.parse_gewe_callback(_payload(f"{CUSTOMER}:\n内部闲聊一句"))
    assert event is not None

    engine.log_group_inbound(event)

    assert engine.route_group_event(event).allowed is False
    assert engine.chat_log.count(ROOM) == 1


def test_staff_message_is_archived_with_the_staff_role(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    event = engine.parse_gewe_callback(_payload(f"{STAFF}:\n内部进展同步"))
    assert event is not None

    engine.log_group_inbound(event)

    assert engine.chat_log.recent()[0].sender_role == "staff"


def test_image_message_is_archived_with_a_media_handle(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    event = engine.parse_gewe_callback(_payload(f"{CUSTOMER}:\n<img/>", msg_type=3))
    assert event is not None

    engine.log_group_inbound(event)

    row = engine.chat_log.recent()[0]
    assert row.kind == "image"
    assert row.text == ""
    assert row.media_ref == "inm_msg-1"


def test_unconfigured_group_is_not_archived(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    event = engine.parse_gewe_callback(_payload(f"{CUSTOMER}:\n你好", group=OTHER_ROOM))
    assert event is not None

    engine.log_group_inbound(event)

    assert engine.chat_log.count() == 0


def test_private_message_is_not_archived(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    event = engine.parse_gewe_callback(_payload("你好", group=PRIVATE))
    assert event is not None

    engine.log_group_inbound(event)

    assert engine.chat_log.count() == 0


def test_a_retried_callback_does_not_duplicate_the_bubble(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    event = engine.parse_gewe_callback(_payload(f"{CUSTOMER}:\n查一下 SKU"))
    assert event is not None

    engine.log_group_inbound(event)
    engine.log_group_inbound(event)

    assert engine.chat_log.count(ROOM) == 1


def test_mention_survives_the_planner_stripping_it(tmp_path: Path) -> None:
    """``strip_group_mention`` removes 昵称 from ``text``; replay still needs it."""
    engine = _engine(tmp_path)
    event = engine.parse_gewe_callback(_payload(f"{CUSTOMER}:\n@AI客服 查一下 SKU"))
    assert event is not None

    engine.log_group_inbound(event)

    row = engine.chat_log.recent()[0]
    assert row.text == "查一下 SKU"
    assert row.metadata["mentions"] == ["AI客服"]


def test_quoted_message_is_archived_for_replay(tmp_path: Path) -> None:
    quoted = (
        "<msg><appmsg><title>查一下 SKU</title>"
        "<refermsg><type>1</type><svrid>quoted-1</svrid>"
        "<content>这两款检测报告麻烦提供下</content></refermsg></appmsg></msg>"
    )
    engine = _engine(tmp_path)
    event = engine.parse_gewe_callback(_payload(f"{CUSTOMER}:\n{quoted}"))
    assert event is not None

    engine.log_group_inbound(event)

    row = engine.chat_log.recent()[0]
    assert row.text == "查一下 SKU"
    assert row.quoted_msg_id == "quoted-1"
    assert row.quoted_type == "1"
    assert row.quoted_text == "这两款检测报告麻烦提供下"


def test_outbound_reply_is_archived_with_channel_and_task(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    reply = ReplyDeliverable(
        text="收到，我们加急处理",
        channel_used="soothe",
        deliverables=[
            Deliverable(
                media_id="media-1",
                filename="检测报告.pdf",
                mime_type="application/pdf",
                size_bytes=1234,
            )
        ],
    )

    engine.log_group_outbound(to_wxid=ROOM, reply=reply, task_id="task-1")

    rows = engine.chat_log.recent()
    assert [row.kind for row in rows] == ["text", "file"]
    text_row, file_row = rows
    assert text_row.direction == "outbound"
    assert text_row.sender_id == BOT
    assert text_row.sender_role == "assistant"
    assert text_row.channel_used == "soothe"
    assert text_row.task_id == "task-1"
    assert file_row.media_ref == "media-1"
    assert file_row.media_name == "检测报告.pdf"
    assert file_row.media_mime == "application/pdf"
    assert file_row.media_size == 1234


def test_an_image_attachment_is_archived_as_an_image(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    reply = ReplyDeliverable(
        text="",
        channel_used="structured",
        deliverables=[Deliverable(media_id="media-2", filename="主图.JPG")],
    )

    engine.log_group_outbound(to_wxid=ROOM, reply=reply)

    assert engine.chat_log.recent()[0].kind == "image"


def test_outbound_to_a_private_or_unknown_chat_is_not_archived(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    reply = ReplyDeliverable(text="普通回复", channel_used="direct")

    engine.log_group_outbound(to_wxid=PRIVATE, reply=reply)
    engine.log_group_outbound(to_wxid=OTHER_ROOM, reply=reply)

    assert engine.chat_log.count() == 0


def test_an_empty_reply_writes_no_row(tmp_path: Path) -> None:
    engine = _engine(tmp_path)

    engine.log_group_outbound(
        to_wxid=ROOM, reply=ReplyDeliverable(text="", channel_used="silent")
    )

    assert engine.chat_log.count() == 0


class _BrokenStore(ChatLogStore):
    """A store whose every write fails, to prove the engine stays standing."""

    def record(self, entry: ChatLogEntry) -> int | None:  # type: ignore[override]
        raise sqlite3.OperationalError("disk I/O error")


def test_a_storage_failure_never_breaks_message_handling(tmp_path: Path) -> None:
    engine = _engine(tmp_path, chat_log=_BrokenStore(tmp_path / "broken.db"))
    event = engine.parse_gewe_callback(_payload(f"{CUSTOMER}:\n查一下 SKU"))
    assert event is not None

    engine.log_group_inbound(event)  # must not raise
    engine.log_group_outbound(
        to_wxid=ROOM, reply=ReplyDeliverable(text="回复", channel_used="direct")
    )


def test_an_engine_without_a_store_is_silent(tmp_path: Path) -> None:
    """Partially built engines in other suites have no ``chat_log`` attribute."""
    engine = _engine(tmp_path)
    engine.chat_log = None
    event = InboundEvent(
        message_id="msg-1",
        from_user=ROOM,
        to_user=BOT,
        chat_id=ROOM,
        user_id=CUSTOMER,
        msg_type=1,
        text="查一下 SKU",
        sender_id=CUSTOMER,
        is_group=True,
    )

    engine.log_group_inbound(event)
    engine.log_group_outbound(
        to_wxid=ROOM, reply=ReplyDeliverable(text="回复", channel_used="direct")
    )
