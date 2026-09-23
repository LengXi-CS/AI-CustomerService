from datetime import UTC, datetime, timedelta

import pytest

from fastgpt_assistant.orchestration.conversation_scheduler import ConversationPoller
from fastgpt_assistant.orchestration.conversation_state import ConversationStateStore


def _customer(store: ConversationStateStore, *, chat_id: str, event_id: str, at: datetime):
    return store.record_customer_message(
        chat_id=chat_id,
        customer_id="customer-a",
        external_event_id=event_id,
        received_at=at,
        text="查询商品",
    )


@pytest.mark.asyncio
async def test_poller_waits_until_last_customer_message_is_quiet(tmp_path) -> None:
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    first_at = datetime(2026, 9, 18, 1, 0, tzinfo=UTC)
    receipt = _customer(store, chat_id="room@chatroom", event_id="msg-1", at=first_at)
    handled: list[str] = []
    poller = ConversationPoller(
        store,
        lambda task: handled.append(task.task_id),
        quiet_window_seconds=120,
        poll_interval_seconds=30,
    )

    assert await poller.poll_once(now=first_at + timedelta(seconds=119)) == []
    assert handled == []
    assert await poller.poll_once(now=first_at + timedelta(seconds=120)) == [receipt.task_id]
    assert handled == [receipt.task_id]
    assert store.get_task(receipt.task_id).state == "processing"


@pytest.mark.asyncio
async def test_each_customer_message_has_an_independent_quiet_window(tmp_path) -> None:
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    first_at = datetime(2026, 9, 18, 1, 0, tzinfo=UTC)
    receipt = _customer(store, chat_id="room@chatroom", event_id="msg-1", at=first_at)
    second = _customer(
        store,
        chat_id="room@chatroom",
        event_id="msg-2",
        at=first_at + timedelta(seconds=60),
    )
    poller = ConversationPoller(store, lambda task: None, quiet_window_seconds=120)

    assert await poller.poll_once(now=first_at + timedelta(seconds=120)) == [receipt.task_id]
    assert await poller.poll_once(now=first_at + timedelta(seconds=180)) == [second.task_id]


@pytest.mark.asyncio
async def test_staff_message_blocks_only_the_target_task(tmp_path) -> None:
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    first_at = datetime(2026, 9, 18, 1, 0, tzinfo=UTC)
    receipt = _customer(store, chat_id="room@chatroom", event_id="msg-1", at=first_at)
    store.record_staff_message(
        chat_id="room@chatroom",
        staff_id="staff-a",
        external_event_id="staff-1",
        received_at=first_at + timedelta(seconds=60),
        text="我来处理",
        mentioned_bot=False,
    )
    poller = ConversationPoller(store, lambda task: None, quiet_window_seconds=120)

    assert await poller.poll_once(now=first_at + timedelta(seconds=300)) == []
    second = _customer(
        store,
        chat_id="room@chatroom",
        event_id="msg-2",
        at=first_at + timedelta(seconds=301),
    )
    assert await poller.poll_once(now=first_at + timedelta(seconds=421)) == [second.task_id]
    assert store.get_task(receipt.task_id).state == "human_active"


@pytest.mark.asyncio
async def test_handler_failure_returns_task_to_waiting_state(tmp_path) -> None:
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    first_at = datetime(2026, 9, 18, 1, 0, tzinfo=UTC)
    receipt = _customer(store, chat_id="room@chatroom", event_id="msg-1", at=first_at)

    async def fail(_task) -> None:
        raise RuntimeError("transient failure")

    poller = ConversationPoller(store, fail, quiet_window_seconds=120)

    assert await poller.poll_once(now=first_at + timedelta(seconds=120)) == []
    assert store.get_task(receipt.task_id).state == "waiting_quiet"
