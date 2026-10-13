import sqlite3
from datetime import UTC, datetime, timedelta

from fastgpt_assistant.orchestration.conversation_state import (
    ConversationStateStore,
    content_digest,
)


def test_customer_messages_create_independent_tasks(tmp_path) -> None:
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    first_at = datetime(2026, 9, 18, 1, 0, tzinfo=UTC)
    second_at = first_at + timedelta(seconds=30)

    first = store.record_customer_message(
        chat_id="room@chatroom",
        customer_id="customer-a",
        external_event_id="msg-1",
        received_at=first_at,
        text="查询 SKU",
    )
    second = store.record_customer_message(
        chat_id="room@chatroom",
        customer_id="customer-a",
        external_event_id="msg-2",
        received_at=second_at,
        text="再看一下工厂",
    )

    task = store.get_task(first.task_id)
    assert first.accepted is True
    assert second.accepted is True
    assert second.task_id != first.task_id
    assert task is not None
    assert task.state == "waiting_quiet"
    assert task.latest_customer_event_id == first.event_id
    assert task.latest_customer_at == first_at.isoformat()
    second_task = store.get_task(second.task_id)
    assert second_task is not None
    assert second_task.latest_customer_event_id == second.event_id
    assert second_task.latest_customer_at == second_at.isoformat()
    assert store.count_events("room@chatroom") == 2


def test_one_message_can_create_independent_demand_slots(tmp_path) -> None:
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    receipt = store.record_customer_message(
        chat_id="room@chatroom",
        customer_id="customer-a",
        external_event_id="msg-split",
        received_at="2026-09-18T01:00:00+00:00",
        text="查询商品；再查工厂",
        demand_count=2,
    )

    assert len(receipt.task_ids) == 2
    assert receipt.task_id == receipt.task_ids[0]
    assert receipt.task_ids[0] != receipt.task_ids[1]
    assert store.list_customer_event_refs(store.get_task(receipt.task_ids[0])) == [
        (receipt.event_id, 0)
    ]
    assert store.list_customer_event_refs(store.get_task(receipt.task_ids[1])) == [
        (receipt.event_id, 1)
    ]


def test_staff_message_takes_over_multiple_tasks_from_one_customer(tmp_path) -> None:
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    first = store.record_customer_message(
        chat_id="room@chatroom",
        customer_id="customer-a",
        external_event_id="msg-1",
        received_at="2026-09-18T01:00:00+00:00",
        text="第一个需求",
    )
    second = store.record_customer_message(
        chat_id="room@chatroom",
        customer_id="customer-a",
        external_event_id="msg-2",
        received_at="2026-09-18T01:00:01+00:00",
        text="第二个需求",
    )

    staff = store.record_staff_message(
        chat_id="room@chatroom",
        staff_id="staff-a",
        external_event_id="staff-ambiguous",
        received_at="2026-09-18T01:00:30+00:00",
        text="我来看一下",
        mentioned_bot=False,
    )

    assert staff.task_id is None
    assert set(staff.task_ids) == {first.task_id, second.task_id}
    assert store.get_task(first.task_id).state == "human_active"
    assert store.get_task(second.task_id).state == "human_active"
    assert store.claim_ready_tasks(
        now="2026-09-18T01:10:00+00:00", quiet_window_seconds=120
    ) == []


def test_staff_message_does_not_guess_across_multiple_customers(tmp_path) -> None:
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    first = store.record_customer_message(
        chat_id="room@chatroom",
        customer_id="customer-a",
        external_event_id="msg-1",
        received_at="2026-09-18T01:00:00+00:00",
        text="第一个需求",
    )
    second = store.record_customer_message(
        chat_id="room@chatroom",
        customer_id="customer-b",
        external_event_id="msg-2",
        received_at="2026-09-18T01:00:01+00:00",
        text="第二个需求",
    )

    staff = store.record_staff_message(
        chat_id="room@chatroom",
        staff_id="staff-a",
        external_event_id="staff-ambiguous",
        received_at="2026-09-18T01:00:30+00:00",
        text="我来看一下",
        mentioned_bot=False,
    )

    assert staff.task_id is None
    assert staff.task_ids == ()
    assert store.get_task(first.task_id).state == "waiting_quiet"
    assert store.get_task(second.task_id).state == "waiting_quiet"


def test_revoked_customer_message_closes_all_its_pending_demand_slots(tmp_path) -> None:
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    original = store.record_customer_message(
        chat_id="room@chatroom",
        customer_id="customer-a",
        external_event_id="message-1",
        received_at="2026-09-18T01:00:00+00:00",
        text="查询商品；查询工厂",
        demand_count=2,
    )
    unrelated = store.record_customer_message(
        chat_id="room@chatroom",
        customer_id="customer-a",
        external_event_id="message-2",
        received_at="2026-09-18T01:00:01+00:00",
        text="另一条消息",
    )

    assert store.close_revoked_message(
        chat_id="other@chatroom", external_event_id="message-1"
    ) == ()
    assert set(
        store.close_revoked_message(
            chat_id="room@chatroom", external_event_id="message-1"
        )
    ) == set(original.task_ids)
    assert store.close_revoked_message(
        chat_id="room@chatroom", external_event_id="message-1"
    ) == ()
    assert all(store.get_task(task_id).state == "closed" for task_id in original.task_ids)
    assert [
        task.task_id
        for task in store.claim_ready_tasks(
            now="2026-09-18T01:10:00+00:00", quiet_window_seconds=120
        )
    ] == [unrelated.task_id]


def test_duplicate_external_event_does_not_extend_window(tmp_path) -> None:
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    received_at = datetime(2026, 9, 18, 1, 0, tzinfo=UTC)
    first = store.record_customer_message(
        chat_id="private-user",
        customer_id="customer-a",
        external_event_id="msg-1",
        received_at=received_at,
        text="查询",
    )
    duplicate = store.record_customer_message(
        chat_id="private-user",
        customer_id="customer-a",
        external_event_id="msg-1",
        received_at=received_at + timedelta(minutes=5),
        text="查询（重复投递）",
    )

    task = store.get_task(first.task_id)
    assert duplicate.duplicate is True
    assert duplicate.accepted is False
    assert duplicate.event_id == first.event_id
    assert task is not None
    assert task.latest_customer_at == received_at.isoformat()
    assert store.count_events("private-user") == 1


def test_staff_event_updates_suppression_marker_without_storing_text(tmp_path) -> None:
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    customer = store.record_customer_message(
        chat_id="room@chatroom",
        customer_id="customer-a",
        external_event_id="customer-1",
        received_at="2026-09-18T01:00:00+00:00",
        text="查 SKU",
    )
    staff = store.record_staff_message(
        chat_id="room@chatroom",
        staff_id="staff-a",
        external_event_id="staff-1",
        received_at="2026-09-18T01:00:30+00:00",
        text="我来跟进",
        mentioned_bot=False,
    )

    task = store.get_task(customer.task_id)
    assert staff.task_id == customer.task_id
    assert task is not None
    assert task.last_staff_at == "2026-09-18T01:00:30+00:00"
    assert task.state == "human_active"

    with sqlite3.connect(tmp_path / "conversation-state.db") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation_events)")}
        values = [row[0] for row in conn.execute("SELECT * FROM conversation_events")]
    assert "text" not in columns
    assert all("我来跟进" not in str(value) for value in values)


def test_ordinary_staff_message_enters_human_active_state(tmp_path) -> None:
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    customer = store.record_customer_message(
        chat_id="room@chatroom",
        customer_id="customer-a",
        external_event_id="customer-1",
        received_at="2026-09-18T01:00:00+00:00",
        text="查 SKU",
    )
    store.record_staff_message(
        chat_id="room@chatroom",
        staff_id="staff-a",
        external_event_id="staff-1",
        received_at="2026-09-18T01:00:30+00:00",
        text="我来跟进",
        mentioned_bot=False,
    )
    store.record_customer_message(
        chat_id="room@chatroom",
        customer_id="customer-a",
        external_event_id="customer-2",
        received_at="2026-09-18T01:01:00+00:00",
        text="好的",
    )

    task = store.get_task(customer.task_id)
    assert task is not None
    assert task.state == "human_active"
    assert task.last_staff_at == "2026-09-18T01:00:30+00:00"


def test_digest_is_deterministic_without_retaining_plaintext() -> None:
    digest, length = content_digest("查询 SKU")

    assert len(digest) == 64
    assert length == 6


def test_human_takeover_keeps_the_bot_silent_indefinitely(tmp_path) -> None:
    """负责人决策（2026-09-18）：人工介入后 bot 对该需求静默。

    ``claim_ready_tasks`` only ever selects ``state = 'waiting_quiet'``, so a
    ``human_active`` task is structurally unreachable by the poller — no timer,
    no threshold, no waking up later. A staff reply without an @-mention takes
    the task there and this test pins that it stays there even after the quiet
    window has long elapsed and the customer keeps talking.
    """
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    customer = store.record_customer_message(
        chat_id="room@chatroom",
        customer_id="customer-a",
        external_event_id="customer-1",
        received_at="2026-09-18T01:00:00+00:00",
        text="查 SKU",
    )
    store.record_staff_message(
        chat_id="room@chatroom",
        staff_id="staff-a",
        external_event_id="staff-1",
        received_at="2026-09-18T01:00:30+00:00",
        text="我来跟进",
        mentioned_bot=False,
    )
    # The customer speaks again, well past the 2-minute quiet window.  This is
    # a new demand task; it must not wake the old human-active task.
    follow_up = store.record_customer_message(
        chat_id="room@chatroom",
        customer_id="customer-a",
        external_event_id="customer-2",
        received_at="2026-09-18T01:30:00+00:00",
        text="请问好了吗",
    )

    claimed = store.claim_ready_tasks(
        now="2026-09-18T02:00:00+00:00", quiet_window_seconds=120
    )

    assert [task.task_id for task in claimed] == [follow_up.task_id]
    task = store.get_task(customer.task_id)
    assert task is not None
    assert task.state == "human_active"


def test_staff_mentioning_the_bot_hands_the_demand_back(tmp_path) -> None:
    """The one documented way out of ``human_active``.

    会议 00:04 区域：人工澄清后，可主动发标准化 prompt 告知 agent 处理。That is
    modelled as a staff message which @-mentions the bot → back to
    ``waiting_quiet``, so the demand resumes through the normal link.
    """
    store = ConversationStateStore(tmp_path / "conversation-state.db")
    customer = store.record_customer_message(
        chat_id="room@chatroom",
        customer_id="customer-a",
        external_event_id="customer-1",
        received_at="2026-09-18T01:00:00+00:00",
        text="查 SKU",
    )
    store.record_staff_message(
        chat_id="room@chatroom",
        staff_id="staff-a",
        external_event_id="staff-1",
        received_at="2026-09-18T01:00:30+00:00",
        text="我来跟进",
        mentioned_bot=False,
    )
    store.record_staff_message(
        chat_id="room@chatroom",
        staff_id="staff-a",
        external_event_id="staff-2",
        received_at="2026-09-18T01:01:00+00:00",
        text="@客服 你来看下",
        mentioned_bot=True,
    )

    task = store.get_task(customer.task_id)
    assert task is not None
    assert task.state == "waiting_quiet"
