"""Unit tests for sliding window context buffer."""

import time

from fastgpt_assistant.orchestration.context_buffer import ContextBuffer


def test_incoming_media_sliding_window() -> None:
    buf = ContextBuffer()
    buf.push_media(
        msg_id="msg_101",
        chat_id="chat_001",
        sender_id="wx_user_1",
        kind="image",
        local_path="C:/tmp/sample.jpg",
        media_id="inm_abc123",
    )

    active = buf.get_recent_media("chat_001", "wx_user_1")
    assert len(active) == 1
    assert active[0].media_id == "inm_abc123"

    # For another user, should be empty
    other_active = buf.get_recent_media("chat_001", "wx_user_2")
    assert len(other_active) == 0

    retained = buf.get_recent_media("chat_001", "wx_user_1", window_seconds=-1)
    assert len(retained) == 1


def test_chat_history_bounded() -> None:
    buf = ContextBuffer(max_history_turns=10)
    for i in range(10):
        buf.push_message(
            chat_id="chat_001",
            sender_id="wx_user_1",
            role="user",
            content=f"question {i}",
        )
        buf.push_message(
            chat_id="chat_001",
            sender_id="wx_user_1",
            role="assistant",
            content=f"answer {i}",
        )

    # Request last 6 messages
    hist = buf.get_history("chat_001", "wx_user_1", limit=6)
    assert len(hist) == 6
    assert hist[-1]["content"] == "answer 9"

    # Clearing history
    buf.clear("chat_001", "wx_user_1")
    assert len(buf.get_history("chat_001", "wx_user_1")) == 0


def test_new_image_after_completed_turn_starts_new_media_batch() -> None:
    buf = ContextBuffer()
    buf.push_media(
        msg_id="old-image",
        chat_id="chat_001",
        sender_id="wx_user_1",
        kind="image",
        local_path="",
        media_id="inm_old",
    )
    time.sleep(0.001)
    buf.push_message("chat_001", "wx_user_1", "user", "查询上一张图片")
    time.sleep(0.001)
    buf.push_media(
        msg_id="new-image",
        chat_id="chat_001",
        sender_id="wx_user_1",
        kind="image",
        local_path="",
        media_id="inm_new",
    )

    active = buf.get_recent_media("chat_001", "wx_user_1")
    assert [item.media_id for item in active] == ["inm_new"]


def test_media_batches_are_isolated_by_conversation_task() -> None:
    buf = ContextBuffer()
    buf.push_media(
        msg_id="image-task-1",
        chat_id="chat_001",
        sender_id="chat_001",
        kind="image",
        local_path="",
        media_id="media-1",
        image_data=b"one",
        mime_type="image/jpeg",
    )
    assert buf.bind_pending_media(
        chat_id="chat_001", sender_id="chat_001", task_ids=("task-1",)
    ) == 1
    buf.push_media(
        msg_id="image-task-2",
        chat_id="chat_001",
        sender_id="chat_001",
        kind="image",
        local_path="",
        media_id="media-2",
        image_data=b"two",
        mime_type="image/jpeg",
    )
    assert buf.bind_pending_media(
        chat_id="chat_001", sender_id="chat_001", task_ids=("task-2",)
    ) == 1

    assert [
        item.media_id
        for item in buf.get_task_media(
            chat_id="chat_001", sender_id="chat_001", task_id="task-1"
        )
    ] == ["media-1"]
    assert [
        item.media_id
        for item in buf.get_task_media(
            chat_id="chat_001", sender_id="chat_001", task_id="task-2"
        )
    ] == ["media-2"]


def test_terminal_task_media_is_not_rebound_as_next_pending_batch() -> None:
    buf = ContextBuffer()
    buf.push_media(
        msg_id="completed-image",
        chat_id="chat_001",
        sender_id="chat_001",
        kind="image",
        local_path="",
        media_id="media-completed",
        image_data=b"old",
        mime_type="image/jpeg",
    )
    assert buf.bind_pending_media(
        chat_id="chat_001", sender_id="chat_001", task_ids=("task-completed",)
    ) == 1
    buf.clear_task_media("task-completed")

    buf.push_media(
        msg_id="new-image",
        chat_id="chat_001",
        sender_id="chat_001",
        kind="image",
        local_path="",
        media_id="media-new",
        image_data=b"new",
        mime_type="image/jpeg",
    )
    assert buf.bind_pending_media(
        chat_id="chat_001", sender_id="chat_001", task_ids=("task-new",)
    ) == 1
    assert [
        item.media_id
        for item in buf.get_task_media(
            chat_id="chat_001", sender_id="chat_001", task_id="task-new"
        )
    ] == ["media-new"]
    # The old item remains available for explicit quote-by-message-id recovery.
    assert [
        item.media_id
        for item in buf.get_task_media(
            chat_id="chat_001", sender_id="chat_001", task_id="task-completed"
        )
    ] == ["media-completed"]


def test_media_can_be_bound_to_task_when_pushed() -> None:
    buf = ContextBuffer()
    buf.push_media(
        msg_id="quoted-image",
        chat_id="chat_001",
        sender_id="chat_001",
        kind="image",
        local_path="",
        media_id="quoted-media",
        image_data=b"quoted",
        mime_type="image/jpeg",
        bound_task_ids=("task-quoted",),
    )

    assert [
        item.media_id
        for item in buf.get_task_media(
            chat_id="chat_001", sender_id="chat_001", task_id="task-quoted"
        )
    ] == ["quoted-media"]
    assert buf.get_task_media(
        chat_id="chat_001", sender_id="chat_001", task_id="other-task"
    ) == []


def test_quoted_original_message_can_bind_existing_media_to_new_task() -> None:
    buf = ContextBuffer()
    buf.push_media(
        msg_id="original-image-message",
        chat_id="chat_001",
        sender_id="chat_001",
        kind="image",
        local_path="",
        media_id="original-media",
        image_data=b"image",
        mime_type="image/jpeg",
    )

    assert [
        item.media_id
        for item in buf.get_media_by_message_id(
            chat_id="chat_001",
            sender_id="chat_001",
            message_id="original-image-message",
        )
    ] == ["original-media"]
    assert buf.bind_media_by_message_id(
        chat_id="chat_001",
        sender_id="chat_001",
        message_id="original-image-message",
        task_id="quoted-task",
    ) == 1
    assert [
        item.media_id
        for item in buf.get_task_media(
            chat_id="chat_001", sender_id="chat_001", task_id="quoted-task"
        )
    ] == ["original-media"]
