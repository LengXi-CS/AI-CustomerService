from datetime import UTC, datetime, timedelta

import pytest

from fastgpt_assistant.core.wechat_groups import WechatGroupPolicy
from fastgpt_assistant.orchestration.conversation_scheduler import ConversationPoller
from fastgpt_assistant.orchestration.conversation_state import ConversationStateStore

START = datetime(2026, 10, 16, 1, tzinfo=UTC)


@pytest.mark.parametrize("value", [-1, 86401, True, 1.5, "30"])
def test_group_wait_rejects_invalid_file_values(value):
    with pytest.raises(ValueError):
        WechatGroupPolicy.from_mapping({"group_id": "room@chatroom",
                                       "conversation_quiet_window_seconds": value})


def test_legacy_policy_inherits_and_zero_is_an_explicit_override():
    legacy = WechatGroupPolicy.from_mapping({"group_id": "room@chatroom"})
    assert legacy.conversation_quiet_window_seconds is None
    policy = WechatGroupPolicy.from_mapping({"group_id": "room@chatroom",
                                            "conversation_quiet_window_seconds": 0})
    assert policy.conversation_quiet_window_seconds == 0


def message(store, chat, message_id="request", *, seconds=0):
    return store.record_customer_message(
        chat_id=chat, customer_id="customer", external_event_id=message_id,
        text="请核实", received_at=START + timedelta(seconds=seconds),
    )


@pytest.mark.asyncio
async def test_poller_uses_each_group_wait_and_reads_updates_each_poll(tmp_path):
    store = ConversationStateStore(tmp_path / "state.db")
    fast = message(store, "fast@chatroom")
    slow = message(store, "slow@chatroom")
    inherited = message(store, "default@chatroom")
    waits = {"fast@chatroom": 0, "slow@chatroom": 300}
    poller = ConversationPoller(store, lambda task: None, quiet_window_seconds=120,
                                quiet_windows_provider=lambda: waits)
    assert await poller.poll_once(now=START) == [fast.task_id]
    assert await poller.poll_once(now=START + timedelta(seconds=30)) == []
    waits["slow@chatroom"] = 30
    assert await poller.poll_once(now=START + timedelta(seconds=30)) == [slow.task_id]
    assert await poller.poll_once(now=START + timedelta(seconds=120)) == [inherited.task_id]
    restored = message(store, "slow@chatroom", "after-reset", seconds=200)
    waits.pop("slow@chatroom")
    assert await poller.poll_once(now=START + timedelta(seconds=230)) == []
    assert await poller.poll_once(now=START + timedelta(seconds=320)) == [restored.task_id]


def test_fast_group_is_not_starved_by_older_slow_tasks_or_association_batch_limit(tmp_path):
    store = ConversationStateStore(tmp_path / "state.db")
    for index in range(45):
        message(store, "slow@chatroom", str(index))
    fast = message(store, "fast@chatroom", seconds=1)
    now = START + timedelta(seconds=10)
    cutoffs = {"fast@chatroom": now.isoformat(), "slow@chatroom":
               (START - timedelta(seconds=120)).isoformat()}
    pending = store.issues.unassigned(list(cutoffs), now.isoformat(), limit=1,
                                     cutoffs_by_chat=cutoffs)
    assert [row["task_id"] for row in pending] == [fast.task_id]
    claimed = store.claim_ready_tasks(now=now, quiet_window_seconds=120, limit=1,
                                      quiet_windows_by_chat={"fast@chatroom": 0,
                                                             "slow@chatroom": 300})
    assert [task.task_id for task in claimed] == [fast.task_id]


def test_issue_wait_starts_from_its_latest_supplement(tmp_path):
    store = ConversationStateStore(tmp_path / "state.db")
    first = message(store, "room@chatroom")
    issue = store.issues.assign(first.task_id)
    second = message(store, "room@chatroom", "supplement", seconds=40)
    store.issues.assign(second.task_id, target_id=issue["issue_id"], relation="continue")
    for seconds in (60, 99):
        assert store.claim_ready_tasks(now=START + timedelta(seconds=seconds),
                                       quiet_windows_by_chat={"room@chatroom": 60}) == []
    assert len(store.claim_ready_tasks(now=START + timedelta(seconds=100),
                                       quiet_windows_by_chat={"room@chatroom": 60})) == 1
