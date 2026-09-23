from datetime import UTC, datetime, timedelta

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


def test_group_context_keeps_exactly_the_latest_60_messages(tmp_path) -> None:
    window = _window(tmp_path)
    start = datetime(2026, 9, 19, tzinfo=UTC)
    for index in range(65):
        window.append(
            message_id=f"message-{index}",
            text=f"第 {index} 条",
            received_at=(start + timedelta(seconds=index)).isoformat(),
            actor_id="member-a" if index % 2 == 0 else "member-b",
        )

    entries = window.read()
    assert len(entries) == 60
    assert entries[0]["message_id"] == "message-5"
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

    rendered = window.format_for_message(message_id="current")

    assert "message-25" in rendered
    assert "message-35" in rendered
    assert "引用消息ID=message-30" in rendered
    assert "message_id=message-5 actor_id=" not in rendered
    assert "message-30" in rendered
