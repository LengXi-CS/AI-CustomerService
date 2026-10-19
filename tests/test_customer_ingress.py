"""Durable arrival fences and revocation across segmentation and restarts."""
from datetime import UTC, datetime, timedelta

import pytest

from fastgpt_assistant.orchestration.conversation_state import ConversationStateStore

ROOM = "ingress@chatroom"
START = datetime(2026, 10, 16, 1, tzinfo=UTC)


def arrival(store, message_id="incoming", *, seconds=0, customer="customer", room=ROOM):
    return store.begin_customer_message(
        chat_id=room, customer_id=customer, external_event_id=message_id,
        received_at=START + timedelta(seconds=seconds), text="请核实苏州仓抵扣金额",
    )


def commit(store, message_id="incoming", *, seconds=0, count=1, room=ROOM):
    return store.record_customer_message(
        chat_id=room, customer_id="customer", external_event_id=message_id,
        received_at=START + timedelta(seconds=seconds), text="请核实苏州仓抵扣金额",
        demand_count=count,
    )


def test_arrival_preserves_identity_and_original_time_without_storing_text(tmp_path):
    store = ConversationStateStore(tmp_path / "state.db")
    begun = arrival(store)
    assert begun.accepted and store.get_open_task(ROOM) is None
    assert store.has_pending_customer_events(ROOM, at=START.isoformat())
    store = ConversationStateStore(store.db_path)
    result = commit(store, seconds=99, count=2)
    assert result.event_id == begun.event_id and len(result.task_ids) == 2
    assert store.get_task(result.task_id).latest_customer_at == START.isoformat()
    assert not store.has_pending_customer_events(ROOM)
    assert arrival(store).duplicate
    with store._connection() as connection:
        row = dict(connection.execute("SELECT * FROM conversation_events").fetchone())
    assert "请核实苏州仓抵扣金额" not in str(row)


@pytest.mark.parametrize("moment", ["before_arrival", "during_split", "after_commit"])
def test_revocation_survives_restart_and_blocks_redelivery(tmp_path, moment):
    store = ConversationStateStore(tmp_path / "state.db")
    original = None
    if moment != "before_arrival":
        arrival(store)
    if moment == "after_commit":
        original = commit(store, count=2)
    closed = store.close_revoked_message(chat_id=ROOM, external_event_id="incoming")
    assert set(closed) == (set(original.task_ids) if original else set())
    store = ConversationStateStore(store.db_path)
    for result in (arrival(store), commit(store)):
        assert not result.accepted and result.reason == "revoked_event"
    assert not store.has_pending_customer_events(ROOM)
    if original:
        assert all(store.get_task(task_id).state == "closed" for task_id in original.task_ids)
    assert commit(store, room="other@chatroom").accepted


@pytest.mark.parametrize("targets", ["empty", "known"])
def test_review_cannot_finish_while_earlier_arrival_is_pending(tmp_path, targets):
    store = ConversationStateStore(tmp_path / "state.db")
    known = commit(store, "known", seconds=-1)
    arrival(store)
    staff = store.record_staff_message(
        chat_id=ROOM, staff_id="owner", external_event_id="staff", text="我来核实",
        received_at=START + timedelta(seconds=1), mentioned_bot=False, precise_issue_target=True,
    )
    selected = {known.task_id} if targets == "known" else set()
    assert store.issues.apply_staff_review(staff.event_id, selected) == ()
    assert len(store.issues.pending_staff_reviews([ROOM], now=START.isoformat())) == 1
    later = commit(store)
    assert store.issues.apply_staff_review(staff.event_id, {later.task_id}) == (later.task_id,)
    assert store.get_task(later.task_id).state == "human_active"


def test_future_customer_arrival_does_not_hold_an_older_staff_review(tmp_path):
    store = ConversationStateStore(tmp_path / "state.db")
    task = commit(store)
    staff = store.record_staff_message(
        chat_id=ROOM, staff_id="owner", external_event_id="staff", text="我来核实",
        received_at=START + timedelta(seconds=1), mentioned_bot=False, precise_issue_target=True,
    )
    arrival(store, "future", seconds=2)
    assert store.issues.apply_staff_review(staff.event_id, {task.task_id}) == (task.task_id,)


@pytest.mark.parametrize("customer,room,allowed", [
    ("customer", ROOM, False), ("other", ROOM, True),
    ("customer", "other@chatroom", True),
])
def test_pending_arrival_fences_claim_and_send_only_for_its_customer(
    tmp_path, customer, room, allowed,
):
    store = ConversationStateStore(tmp_path / "state.db")
    task = commit(store, "existing")
    issue = store.issues.assign(task.task_id)
    arrival(store, seconds=2, customer=customer, room=room)
    assert store.issues.can_send(issue["issue_id"], issue["version"]) is allowed
    claimed = store.claim_ready_tasks(now=START + timedelta(hours=1))
    assert (task.task_id in {item.task_id for item in claimed}) is allowed


def test_equal_time_image_after_arrival_belongs_to_the_next_text(tmp_path):
    store = ConversationStateStore(tmp_path / "state.db")
    arrival(store)
    store.record_customer_media(chat_id=ROOM, customer_id="customer", external_event_id="image",
                                received_at=START)
    first = commit(store)
    second = commit(store, "next", seconds=1)
    assert store.issues.message_ids(first.task_id) == []
    assert store.issues.message_ids(second.task_id) == ["image"]


def test_revocation_before_image_arrival_cannot_resurrect_pending_media(tmp_path):
    store = ConversationStateStore(tmp_path / "state.db")
    store.close_revoked_message(chat_id=ROOM, external_event_id="image")
    store.record_customer_media(chat_id=ROOM, customer_id="customer", external_event_id="image",
                                received_at=START)
    assert store.pending_customer_media_ids(ROOM) == set()
