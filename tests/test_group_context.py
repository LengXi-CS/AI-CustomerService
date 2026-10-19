from datetime import UTC, datetime, timedelta

import pytest

from fastgpt_assistant.core.chat_log import ChatLogEntry, ChatLogStore
from fastgpt_assistant.orchestration.group_context import GroupContextWindow
from fastgpt_assistant.orchestration.payload_vault import (
    PayloadVault,
    generate_payload_vault_key,
)


def _window(tmp_path) -> GroupContextWindow:
    vault = PayloadVault(
        key=generate_payload_vault_key(),
        db_path=tmp_path / "payload-vault.db",
    )
    return GroupContextWindow(vault, bot_id="bot-wxid", group_id="room@chatroom")


def test_group_context_keeps_all_messages_of_the_day(tmp_path) -> None:
    window = _window(tmp_path)
    start = datetime(2026, 9, 19, tzinfo=UTC)
    for index in range(65):
        window.append(
            message_id=f"message-{index}",
            text=f"第 {index} 条",
            received_at=(start + timedelta(seconds=index)).isoformat(),
            actor_id="member-a" if index % 2 == 0 else "member-b",
        )

    entries = window.read(at=start + timedelta(seconds=65))
    assert len(entries) == 65
    assert entries[0]["message_id"] == "message-0"
    assert entries[-1]["message_id"] == "message-64"


def test_quoted_message_adds_its_five_message_neighborhood(tmp_path) -> None:
    window = _window(tmp_path)
    start = datetime(2026, 9, 19, tzinfo=UTC)
    for index in range(60):
        window.append(
            message_id=f"message-{index}",
            text=(f"第 {index} 条 " + "内容 " * 500),
            received_at=(start + timedelta(seconds=index)).isoformat(),
            actor_id="member-a",
        )
    window.append(
        message_id="current",
        text="这两款检测报告麻烦提供下",
        received_at=(start + timedelta(seconds=60)).isoformat(),
        actor_id="member-a",
        reply_to_message_id="message-30",
        quoted_text="[图片]",
    )

    rendered = window.format_for_message(message_id="current", at=start + timedelta(seconds=60))

    assert "message-25" in rendered
    assert "message-35" in rendered
    assert "引用消息ID=message-30" in rendered
    assert "message_id=message-5 actor_id=" not in rendered
    assert "message-30" in rendered


def test_daily_context_uses_beijing_midnight_and_excludes_future_messages(tmp_path):
    window = _window(tmp_path)
    for msg, at in [
        ("yesterday", "2026-10-15T15:59:59+00:00"),
        ("midnight", "2026-10-15T16:00:00+00:00"),
        ("future", "2026-10-16T10:00:00+00:00"),
    ]:
        window.append(message_id=msg, text=msg, actor_id="customer", received_at=at)
    entries = window.read(at="2026-10-16T09:00:00+00:00")
    assert [entry["message_id"] for entry in entries] == ["midnight"]
    assert [entry["message_id"] for entry in window.read(at="2026-10-15T16:00:00+00:00")] == [
        "midnight"
    ]


def test_daily_context_survives_restart_and_deduplicates_out_of_order_callbacks(tmp_path):
    window = _window(tmp_path)
    for index in [3, 1, 2, 1]:
        window.append(message_id=str(index), text=f"私密原话{index}", actor_id="customer",
                      received_at=f"2026-10-16T01:00:0{index}+00:00")
    restarted = GroupContextWindow(window.vault, bot_id="bot-wxid", group_id="room@chatroom")
    entries = restarted.read(at="2026-10-16T02:00:00+00:00")
    assert [entry["message_id"] for entry in entries] == ["1", "2", "3"]
    assert "私密原话".encode() not in window.vault.db_path.read_bytes()
    other = GroupContextWindow(window.vault, bot_id="bot-wxid", group_id="other@chatroom")
    assert other.read(at="2026-10-16T02:00:00+00:00") == ()


def test_daily_context_recovers_earlier_staff_and_bot_messages_from_archive(tmp_path):
    window = _window(tmp_path)
    archive = ChatLogStore(tmp_path / "archive.db")
    for role in ["customer", "staff", "assistant"]:
        archive.record(ChatLogEntry(
            chat_id="room@chatroom", chat_name="测试群", message_id=role,
            direction="outbound" if role == "assistant" else "inbound",
            sender_id=role, sender_role=role, kind="text", text=f"{role}原话",
            occurred_at="2026-10-16T01:00:00+00:00",
        ))
    window.archive = archive
    entries = window.read(at="2026-10-16T02:00:00+00:00")
    assert {entry["sender_role"] for entry in entries} == {"customer", "staff", "assistant"}
    assert "staff原话" in window.format_for_message(
        message_id="customer", at="2026-10-16T02:00:00+00:00"
    )


def test_cross_midnight_pending_quote_keeps_only_referenced_previous_day_message(tmp_path):
    window = _window(tmp_path)
    for msg in ["unrelated", "pending"]:
        window.append(message_id=msg, text=msg, actor_id="customer",
                      received_at="2026-10-15T15:59:59+00:00")
    window.append(message_id="staff", text="我继续核实", actor_id="staff", sender_role="staff",
                  received_at="2026-10-15T16:01:00+00:00", reply_to_message_id="pending")
    rendered = window.format_for_message(message_id="staff", at="2026-10-15T16:02:00+00:00")
    assert "message_id=pending " in rendered
    assert "message_id=unrelated " not in rendered


def test_related_demand_precedes_unrelated_quotes_when_full_day_exceeds_model_budget(tmp_path):
    window = _window(tmp_path)
    start = datetime(2026, 10, 16, 1, tzinfo=UTC)
    for index in range(100):
        window.append(message_id=f"unrelated-{index}", text="无关长消息" * 800,
                      actor_id="other", received_at=(start + timedelta(seconds=index)).isoformat(),
                      reply_to_message_id=f"unrelated-{max(0, index - 1)}")
    window.append(message_id="demand", text="苏州仓核对金额", actor_id="customer",
                  received_at=(start + timedelta(seconds=101)).isoformat())
    window.append(message_id="staff", text="我正在核对", actor_id="staff", sender_role="staff",
                  received_at=(start + timedelta(seconds=102)).isoformat())
    rendered = window.format_for_message(
        message_id="staff", related_message_ids=("demand",), at=start + timedelta(seconds=102),
    )
    assert "message_id=demand " in rendered and "message_id=staff " in rendered
    assert "共102条" in rendered
    assert len(window.read(at=start + timedelta(seconds=102))) == 102


@pytest.mark.parametrize("ids", [("",), ("   ",), ("", None, " ", "")])
def test_empty_related_ids_never_load_unrelated_previous_day_messages(tmp_path, ids):
    window = _window(tmp_path)
    window.append(message_id="yesterday", text="前一天无关消息", actor_id="customer",
                  received_at="2026-10-15T15:59:59+00:00")
    window.append(message_id="today", text="当天消息", actor_id="customer",
                  received_at="2026-10-15T16:01:00+00:00")
    rendered = window.format_for_message(message_id="today", related_message_ids=ids,
                                         at="2026-10-15T16:02:00+00:00")
    assert "message_id=yesterday " not in rendered
    assert "message_id=today " in rendered


def test_context_vault_rejects_unbounded_reads_and_empty_id_never_expands_scope(tmp_path):
    window = _window(tmp_path)
    window.append(message_id="one", text="原话", actor_id="customer",
                  received_at="2026-10-16T01:00:00+00:00")
    vault = window.vault
    with pytest.raises(ValueError, match="time interval"):
        vault.retrieve_group_messages(window.session_id)
    assert vault.retrieve_group_messages(window.session_id, message_id="") == []
    assert vault.retrieve_group_messages(window.session_id, message_id="   ") == []
    assert vault.retrieve_group_messages(window.session_id, start="2026-10-16", end="2026-10-17",
                                          message_id="") == []
    assert vault.retrieve_group_messages(window.session_id, message_id="one")[0]["text"] == "原话"
