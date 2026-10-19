from datetime import UTC, datetime, timedelta

import pytest

from fastgpt_assistant.orchestration.conversation_state import ConversationStateStore

ROOM = "media@chatroom"
START = datetime(2026, 10, 16, 1, tzinfo=UTC)


def image(store, message_id="picture", *, actor="a", seconds=0, room=ROOM):
    store.record_customer_media(chat_id=room, customer_id=actor, external_event_id=message_id,
                                received_at=START + timedelta(seconds=seconds))


def text(store, message_id="text", *, actor="a", seconds=1, count=1, room=ROOM):
    return store.record_customer_message(
        chat_id=room, customer_id=actor, external_event_id=message_id, text="核实图中金额",
        received_at=START + timedelta(seconds=seconds), demand_count=count,
    )


def test_pending_image_binds_atomically_after_restart_and_can_be_quoted_by_staff(tmp_path):
    store = ConversationStateStore(tmp_path / "state.db")
    image(store)
    assert store.get_open_task(ROOM) is None  # An image alone is not a new demand.
    store = ConversationStateStore(store.db_path)
    receipt = text(store)
    assert store.issues.referenced_tasks(ROOM, "picture") == {receipt.task_id}
    refs = store.list_customer_event_refs(store.get_task(receipt.task_id))
    assert refs == [(receipt.event_id, 0)]
    store = ConversationStateStore(store.db_path)
    takeover = store.record_staff_message(
        chat_id=ROOM, staff_id="owner", external_event_id="staff", text="核实无误",
        received_at=START + timedelta(seconds=30), mentioned_bot=False,
        quoted_msg_id="picture", precise_issue_target=True,
    )
    assert takeover.task_ids == (receipt.task_id,)
    assert store.get_task(receipt.task_id).state == "human_active"


def test_pending_media_respects_actor_room_and_text_boundaries(tmp_path):
    store = ConversationStateStore(tmp_path / "state.db")
    image(store, "a-image")
    image(store, "b-image", actor="b")
    image(store, "other-room-image", room="other@chatroom")
    other = text(store, "b-text", actor="b")
    first = text(store, "a-text")
    second = text(store, "a-next", seconds=2)
    assert store.issues.message_ids(first.task_id) == ["a-image"]
    assert store.issues.message_ids(other.task_id) == ["b-image"]
    assert store.issues.message_ids(second.task_id) == []
    assert store.pending_customer_media_ids("other@chatroom") == {"other-room-image"}


def test_duplicate_callback_does_not_consume_fresh_image_batch(tmp_path):
    store = ConversationStateStore(tmp_path / "state.db")
    first = text(store)
    image(store, seconds=2)
    duplicate = text(store)
    assert duplicate.duplicate and duplicate.task_ids == first.task_ids
    assert store.issues.message_ids(first.task_id) == []
    next_turn = text(store, "next", seconds=3)
    assert store.issues.message_ids(next_turn.task_id) == ["picture"]


@pytest.mark.parametrize("sequence", ["image-first", "text-first"])
def test_equal_timestamp_uses_persisted_arrival_order(tmp_path, sequence):
    store = ConversationStateStore(tmp_path / "state.db")
    if sequence == "image-first":
        image(store)
        receipt = text(store, seconds=0)
        assert store.issues.message_ids(receipt.task_id) == ["picture"]
    else:
        receipt = text(store, seconds=0)
        image(store)
        next_turn = text(store, "next", seconds=1)
        assert store.issues.message_ids(receipt.task_id) == []
        assert store.issues.message_ids(next_turn.task_id) == ["picture"]


def test_late_old_image_cannot_attach_to_next_independent_request(tmp_path):
    store = ConversationStateStore(tmp_path / "state.db")
    text(store, seconds=10)
    image(store, seconds=5)  # A delayed callback from before the previous text.
    next_turn = text(store, "next", seconds=20)
    assert store.issues.message_ids(next_turn.task_id) == []
    assert store.pending_customer_media_ids(ROOM) == set()


def test_image_received_during_text_segmentation_belongs_to_the_next_text(tmp_path):
    store = ConversationStateStore(tmp_path / "state.db")
    image(store, seconds=2)
    earlier_text = text(store, seconds=1)  # Splitting finished after the image arrived.
    later_text = text(store, "next", seconds=3)
    assert store.issues.message_ids(earlier_text.task_id) == []
    assert store.issues.message_ids(later_text.task_id) == ["picture"]


def test_revoked_pending_image_is_not_consumed_by_later_text(tmp_path):
    store = ConversationStateStore(tmp_path / "state.db")
    image(store)
    store.close_revoked_message(chat_id=ROOM, external_event_id="picture")
    image(store)  # A duplicate callback cannot resurrect the revoked image.
    receipt = text(store)
    assert store.issues.message_ids(receipt.task_id) == []


def test_media_batch_binds_all_slots_once_and_survives_task_completion(tmp_path):
    store = ConversationStateStore(tmp_path / "state.db")
    image(store, "one")
    image(store, "two")
    receipt = text(store, count=2)
    for task_id in receipt.task_ids:
        assert set(store.issues.message_ids(task_id)) == {"one", "two"}
        store.set_state(task_id, "closed")
    image(store, "one")
    followup = text(store, "next", seconds=5)
    assert store.issues.message_ids(followup.task_id) == []
