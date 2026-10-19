"""Replay customer bursts through durable association, claims and actual dispatch."""

import asyncio
import json
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import httpx
import pytest

from fastgpt_assistant.adapters.wechat.client import WechatGeweClient
from fastgpt_assistant.core.chat_log import ChatLogStore
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.core.wechat_quote import TextQuote
from fastgpt_assistant.orchestration.context_buffer import ContextBuffer
from fastgpt_assistant.orchestration.conversation_scheduler import ConversationPoller
from fastgpt_assistant.orchestration.conversation_state import ConversationStateStore
from fastgpt_assistant.orchestration.demand_association import association_text, validated_links
from fastgpt_assistant.orchestration.engine import AssistantEngine, InboundEvent, ReplyDeliverable
from fastgpt_assistant.orchestration.inbound_media_store import InboundMediaStore
from fastgpt_assistant.orchestration.payload_vault import PayloadVault, generate_payload_vault_key

ROOM = "issue-test@chatroom"
START = datetime(2026, 10, 13, 0, 0, tzinfo=UTC)


@pytest.fixture
def ingress_callbacks(engine, monkeypatch):
    from fastgpt_assistant.server import app as app_module

    def parse(body):
        kind = body["kind"]
        return InboundEvent(
            message_id=body.get("id", {"customer": "10000001", "staff": "staff",
                                       "revoke": "revoke"}[kind]),
            from_user=ROOM, to_user="bot", chat_id=ROOM,
            user_id="owner" if kind == "staff" else "customer@openim",
            sender_id="owner" if kind == "staff" else "customer@openim",
            sender_role="staff" if kind == "staff" else "customer", is_group=True,
            msg_type=10002 if kind == "revoke" else 1,
            text={"customer": "请核实苏州仓抵扣金额", "staff": "这笔我来核实",
                  "revoke": '<sysmsg type="revokemsg"><revokemsg><newmsgid>10000001</newmsgid>'
                            '</revokemsg></sysmsg>'}[kind],
            quoted_msg_id="10000001" if kind == "staff" and body.get("quoted", True) else "",
            raw_payload={"received_at": (START + timedelta(
                seconds=body.get("seconds", 0 if kind == "customer" else 1))).isoformat()},
        )

    engine.query_executor = SimpleNamespace(media_base_url="")
    engine.observe_public_origin = lambda **kwargs: None
    engine.parse_gewe_callback = parse
    engine.model_client.match_staff_takeover = lambda staff, items, context: [
        {"task_id": item["task_id"], "confidence": 1,
         "evidence_message_ids": [item["message_id"]]} for item in items
    ]
    monkeypatch.setattr(app_module, "get_engine", lambda: engine)
    return engine, app_module.app


@pytest.fixture
def engine(tmp_path):
    result = object.__new__(AssistantEngine)
    result.settings = SimpleNamespace(
        gewe_app_id="bot",
        attachment_dispatch_interval_seconds=0,
        conversation_quiet_window_seconds=1800,
    )
    result.conversation_state = ConversationStateStore(tmp_path / "state.db")
    result.payload_vault = PayloadVault(generate_payload_vault_key(), tmp_path / "vault.db")
    result.chat_log = ChatLogStore(tmp_path / "chat.db")
    result.groups_config = WechatGroupsConfig.from_mapping(
        {
            "global": {"enabled": True},
            "groups": [
                {
                    "group_id": ROOM,
                    "mode": "active",
                    "native_quote_replies": True,
                    "associate_customer_demands": True,
                }
            ],
        }
    )
    result.model_client = SimpleNamespace(associate_demands=lambda items, candidates, **kwargs: [])
    result.queries = []
    result.requests = []

    def query(event, **kwargs):
        result.queries.append(event)
        return ReplyDeliverable(text="收到，已看到您反馈的问题。", channel_used="soothe")

    def transport(request):
        result.requests.append(request)
        return httpx.Response(200, json={"ret": 200, "data": {"newMsgId": "90000001"}})

    result._process_text_query = query
    result.wechat = WechatGeweClient(base_url="https://example.test", app_id="bot")
    result.wechat._client.close()
    result.wechat._client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(transport)
    )
    yield result
    result.wechat.close()


def enqueue(engine, text, *, seconds=0, sender="customer@openim", demands=None, quote=""):
    store = engine.conversation_state
    with store._connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM conversation_events").fetchone()[0]
    message_id = str(10000000 + count)
    at = START + timedelta(seconds=seconds)
    source = TextQuote(
        message_id=message_id,
        chat_id=ROOM,
        sender_id=sender,
        content=text,
        created_at=int(at.timestamp()),
        str_id=f"original-{message_id}",
        msg_source=f"<msgsource><strid>original-{message_id}</strid></msgsource>",
        imunion_sender_id=sender,
    )
    event = InboundEvent(
        message_id=message_id,
        from_user=ROOM,
        to_user="bot",
        chat_id=ROOM,
        user_id=sender,
        sender_id=sender,
        sender_role="customer",
        is_group=True,
        msg_type=1,
        text=text,
        reply_source=source,
        quoted_msg_id=quote,
        raw_payload={"received_at": at.isoformat()},
    )
    receipt = engine.record_conversation_event(event, demands=demands)
    assert engine.persist_pending_group_event(event, event_id=receipt.event_id, demands=demands)
    return receipt, message_id


@pytest.mark.asyncio
@pytest.mark.parametrize("action,quoted", [("staff", True), ("staff", False), ("revoke", False)])
async def test_intervention_during_segmentation_never_sends_an_automatic_reply(
    ingress_callbacks, action, quoted,
):
    engine, app = ingress_callbacks
    started, release = threading.Event(), threading.Event()

    def split(text):
        started.set()
        assert release.wait(10)
        return [text]

    engine.model_client.split_demands = split
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        customer = asyncio.create_task(c.post("/api/wechat/callback", json={"kind": "customer"}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await c.post("/api/wechat/callback", json={"kind": action, "quoted": quoted})
            await asyncio.to_thread(reconcile, engine, 2)
            assert engine.conversation_state.get_open_task(ROOM) is None
            if action == "staff":
                assert len(engine.conversation_state.issues.pending_staff_reviews(
                    [ROOM], now=(START + timedelta(seconds=2)).isoformat()
                )) == 1
        finally:
            release.set()
        response = (await customer).json()
        repeated = (await c.post("/api/wechat/callback", json={"kind": "customer"})).json()
    reconcile(engine)
    if action == "staff":
        assert repeated["reason"] == "duplicate_event"
        task = engine.conversation_state.get_task(response["task_id"])
        assert task.state == "human_active" and task.human_owner_id == "owner"
    else:
        assert response["reason"] == repeated["reason"] == "revoked_event"
        assert engine.conversation_state.get_open_task(ROOM) is None
    assert claim(engine) == [] and engine.requests == []
    assert engine._active_customer_events == {}


@pytest.mark.asyncio
async def test_cancelled_duplicate_does_not_release_or_recover_another_live_callback(
    ingress_callbacks,
):
    engine, app = ingress_callbacks
    started, both_started, release = threading.Event(), threading.Event(), threading.Event()
    count_lock, count = threading.Lock(), 0

    def split(text):
        nonlocal count
        with count_lock:
            count += 1
            started.set()
            if count == 2:
                both_started.set()
        assert release.wait(10)
        return [text]

    engine.model_client.split_demands = split
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        first = asyncio.create_task(c.post("/api/wechat/callback", json={"kind": "customer"}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            duplicate = asyncio.create_task(c.post("/api/wechat/callback",
                                                   json={"kind": "customer"}))
            assert await asyncio.to_thread(both_started.wait, 5)
            duplicate.cancel()
            with pytest.raises(asyncio.CancelledError):
                await duplicate
            await asyncio.to_thread(reconcile, engine, 2)
            assert engine.conversation_state.get_open_task(ROOM) is None
            assert list(engine._active_customer_events.values()) == [1]
        finally:
            release.set()
        assert (await first).json()["msg"] == "queued"
    assert engine._active_customer_events == {}
    assert len(engine.conversation_state.list_customer_event_refs(
        engine.conversation_state.get_open_task(ROOM)
    )) == 1


@pytest.mark.asyncio
async def test_cancelled_callback_is_recovered_before_staff_review(ingress_callbacks):
    engine, app = ingress_callbacks
    started, release = threading.Event(), threading.Event()

    def split(text):
        started.set()
        assert release.wait(10)
        return [text]

    engine.model_client.split_demands = split
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        pending = asyncio.create_task(c.post("/api/wechat/callback", json={"kind": "customer"}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await c.post("/api/wechat/callback", json={"kind": "staff"})
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            release.set()
    assert engine._active_customer_events == {}
    reconcile(engine)
    task = engine.conversation_state.get_open_task(ROOM)
    assert task.state == "human_active" and task.human_owner_id == "owner"
    assert not engine.conversation_state.has_pending_customer_events(ROOM)
    assert claim(engine) == [] and engine.requests == []


@pytest.mark.parametrize("damage", ["none", "missing_payload", "revoked"])
def test_pending_original_recovers_safely_after_restart(engine, damage):
    event = InboundEvent(
        message_id="before-restart", from_user=ROOM, to_user="bot", chat_id=ROOM,
        user_id="customer@openim", sender_id="customer@openim", sender_role="customer",
        is_group=True, msg_type=1, text="请核实苏州仓抵扣金额",
        raw_payload={"received_at": START.isoformat()},
    )
    receipt = engine.prepare_customer_event(event)
    assert receipt.accepted
    if damage == "missing_payload":
        engine.payload_vault.retrieve_text = lambda event_id: None
    elif damage == "revoked":
        engine.conversation_state.close_revoked_message(chat_id=ROOM,
                                                       external_event_id=event.message_id)
    engine.conversation_state = ConversationStateStore(engine.conversation_state.db_path)
    engine._active_customer_events = {}  # Process-local ownership disappears on restart.
    engine.recover_pending_customer_events()
    engine.recover_pending_customer_events()  # Recovery is idempotent.
    task = engine.conversation_state.get_open_task(ROOM)
    if damage == "none":
        assert task.latest_customer_at == START.isoformat()
        assert engine._association_item(task)["text"] == event.text
    else:
        assert task is None
    assert not engine.conversation_state.has_pending_customer_events(ROOM)
    assert engine.requests == []


@pytest.mark.parametrize("fail_at", ["arrival", "commit"])
def test_unavailable_ingress_payload_never_exposes_sendable_tasks(engine, monkeypatch, fail_at):
    event = InboundEvent(
        message_id="unavailable", from_user=ROOM, to_user="bot", chat_id=ROOM,
        user_id="customer@openim", sender_id="customer@openim", sender_role="customer",
        is_group=True, msg_type=1, text="请核实苏州仓抵扣金额",
        raw_payload={"received_at": START.isoformat()},
    )
    if fail_at == "arrival":
        monkeypatch.setattr(engine, "persist_pending_group_event", lambda *args, **kwargs: False)
    receipt = engine.prepare_customer_event(event)
    if fail_at == "commit":
        monkeypatch.setattr(engine, "persist_pending_group_event", lambda *args, **kwargs: False)
        receipt = engine.complete_customer_event(
            event, event_id=receipt.event_id, demands=[event.text]
        )
        engine.release_customer_event(receipt.event_id)
    assert not receipt.accepted and receipt.reason == "payload_unavailable"
    assert engine.conversation_state.get_open_task(ROOM) is None
    assert not engine.conversation_state.has_pending_customer_events(ROOM)
    assert engine._active_customer_events == {}


def test_duplicate_completion_cannot_overwrite_verified_demand_slots(engine):
    parts = ["请核实苏州仓抵扣金额", "另一个订单请开发票"]
    event = InboundEvent(
        message_id="duplicate-slots", from_user=ROOM, to_user="bot", chat_id=ROOM,
        user_id="customer@openim", sender_id="customer@openim", sender_role="customer",
        is_group=True, msg_type=1, text="；".join(parts),
        raw_payload={"received_at": START.isoformat()},
    )
    first = engine.prepare_customer_event(event)
    duplicate = engine.prepare_customer_event(event)
    completed = engine.complete_customer_event(event, event_id=first.event_id, demands=parts)
    engine.release_customer_event(first.event_id)
    repeated = engine.complete_customer_event(
        event, event_id=duplicate.event_id, demands=[event.text]
    )
    engine.release_customer_event(duplicate.event_id)
    assert repeated.duplicate and repeated.task_ids == completed.task_ids
    assert len(completed.task_ids) == 2
    assert json.loads(engine.payload_vault.retrieve_text(first.event_id))["demands"] == parts
    assert engine._active_customer_events == {}


def test_recovery_waits_for_earlier_live_customer_arrival(engine):
    def event(message_id, seconds):
        return InboundEvent(
            message_id=message_id, from_user=ROOM, to_user="bot", chat_id=ROOM,
            user_id="customer@openim", sender_id="customer@openim", sender_role="customer",
            is_group=True, msg_type=1, text="请核实苏州仓抵扣金额",
            raw_payload={"received_at": (START + timedelta(seconds=seconds)).isoformat()},
        )

    first = engine.prepare_customer_event(event("earlier", 0))
    second = engine.prepare_customer_event(event("cancelled-later", 1))
    engine.release_customer_event(second.event_id)
    engine.recover_pending_customer_events()
    assert engine.conversation_state.get_open_task(ROOM) is None
    engine.release_customer_event(first.event_id)
    engine.recover_pending_customer_events()
    assert not engine.conversation_state.has_pending_customer_events(ROOM)
    assert engine.conversation_state.count_events(ROOM) == 2
    assert engine._active_customer_events == {}


@pytest.mark.asyncio
async def test_zero_group_wait_applies_to_both_association_and_poller(engine):
    config = engine.groups_config
    engine.groups_config = replace(config, groups={ROOM: replace(
        config.get(ROOM), conversation_quiet_window_seconds=0
    )})
    receipt, _ = enqueue(engine, "请核实苏州仓抵扣金额")
    handled = []
    poller = ConversationPoller(
        engine.conversation_state, lambda task: handled.append(task.task_id),
        quiet_window_seconds=1800, reconciler=engine.reconcile_customer_issues,
        quiet_windows_provider=engine.conversation_quiet_windows,
    )
    assert await poller.poll_once(now=START) == [receipt.task_id]
    assert handled == [receipt.task_id]


@pytest.mark.parametrize("associated", [True, False])
def test_increased_group_wait_during_generation_is_checked_before_sending(engine, associated):
    config = engine.groups_config
    engine.groups_config = replace(config, groups={ROOM: replace(
        config.get(ROOM), conversation_quiet_window_seconds=0
    )})
    event = InboundEvent(
        message_id="recent-request", from_user=ROOM, to_user="bot", chat_id=ROOM,
        user_id="customer@openim", sender_id="customer@openim", sender_role="customer",
        is_group=True, msg_type=1, text="请核实苏州仓抵扣金额",
        raw_payload={"received_at": (datetime.now(UTC) - timedelta(seconds=20)).isoformat()},
    )
    receipt = engine.record_conversation_event(event)
    engine.persist_pending_group_event(event, event_id=receipt.event_id)
    if associated:
        engine.conversation_state.issues.assign(receipt.task_id)
    claimed = engine.conversation_state.claim_ready_tasks(quiet_window_seconds=0)
    assert len(claimed) == 1

    def generate(event, **kwargs):
        # The operator increases this group's wait while the model is generating.
        engine.groups_config = replace(config, groups={ROOM: replace(
            config.get(ROOM), conversation_quiet_window_seconds=300
        )})
        return ReplyDeliverable(text="收到，我来核实一下。", channel_used="soothe")

    engine._process_text_query = generate
    engine.process_conversation_task(claimed[0])
    assert engine.requests == []
    assert engine.conversation_state.get_task(receipt.task_id).state == "waiting_quiet"


def propose(engine, *, relation="continue", confidence=1):
    def associate(items, candidates, **kwargs):
        target = candidates[0]["issue_id"] if candidates else items[0]["task_id"]
        return [
            {
                "task_id": item["task_id"],
                "target_id": target,
                "relation": relation,
                "confidence": confidence,
            }
            for item in (items if candidates else items[1:])
        ]

    engine.model_client.associate_demands = associate


def reconcile(engine, seconds=150):
    engine.reconcile_customer_issues(now=START + timedelta(seconds=seconds))


def claim(engine, seconds=4000):
    return engine.conversation_state.claim_ready_tasks(
        now=START + timedelta(seconds=seconds),
        quiet_window_seconds=1800,
    )


def test_missing_item_and_followup_produce_one_native_reply_after_latest_quiet_window(engine):
    first, original = enqueue(engine, "87662609290266订单的1702357数量有900，签收面单上没有")
    second, _ = enqueue(engine, "请核实是不是漏发了", seconds=17)
    propose(engine)
    reconcile(engine)
    assert claim(engine, 1800) == []
    tasks = claim(engine, 1817)
    assert len(tasks) == 1
    engine.process_conversation_task(tasks[0])
    assert len(engine.requests) == 1
    assert len(engine.queries) == 1
    event = engine.queries[0]
    assert "数量有900" in event.text and "是不是漏发了" in event.text
    assert event.customer_messages == (
        "87662609290266订单的1702357数量有900，签收面单上没有", "请核实是不是漏发了"
    )
    assert set(event.related_task_ids) == {first.task_id, second.task_id}
    xml = ET.fromstring(json.loads(engine.requests[0].content)["appmsg"])
    assert xml.findtext("refermsg/svrid") == original
    assert engine.conversation_state.issues.get(first.task_id)["soothe_status"] == "sent"
    assert engine.conversation_state.get_task(second.task_id).state == "responded"

    # Reopening the database and a duplicate callback cannot produce another acknowledgement.
    restarted = ConversationStateStore(engine.conversation_state.db_path)
    restarted.issues.recover_interrupted()
    duplicate = restarted.record_customer_message(
        chat_id=ROOM,
        customer_id="customer@openim",
        external_event_id=original,
        received_at=START,
        text="重复回调",
    )
    assert duplicate.duplicate
    assert restarted.claim_ready_tasks(now=START + timedelta(hours=3)) == []
    with restarted._connection() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(conversation_issues)")}
    assert "text" not in columns and "content" not in columns


@pytest.mark.parametrize(
    "reason", ["different_order", "different_customer", "uncertain", "same_event"]
)
def test_invalid_or_ambiguous_association_preserves_independent_tasks(engine, reason):
    first_text = "订单87662609290266请核实漏发"
    if reason == "same_event":
        first, _ = enqueue(
            engine, first_text + "；这笔订单还需要发票", demands=[first_text, "这笔订单还需要发票"]
        )
        ids = first.task_ids
    else:
        first, _ = enqueue(engine, first_text)
        second, _ = enqueue(
            engine,
            "订单87662609290267请核实漏发" if reason == "different_order" else "请核实是不是漏发了",
            seconds=17,
            sender="another@openim" if reason == "different_customer" else "customer@openim",
        )
        ids = (first.task_id, second.task_id)
    propose(engine, confidence=0.8 if reason == "uncertain" else 1)
    reconcile(engine)
    issues = [engine.conversation_state.issues.get(task_id)["issue_id"] for task_id in ids]
    assert len(set(issues)) == 2
    assert len(claim(engine)) == 2


def test_semantically_independent_same_order_and_new_relation_with_target_stay_independent(engine):
    first, original = enqueue(engine, "订单87662609290266请核实漏发")
    second, _ = enqueue(engine, "订单87662609290266需要开票", seconds=17)
    propose(engine, relation="new")
    reconcile(engine)
    assert (
        engine.conversation_state.issues.get(first.task_id)["issue_id"]
        != (engine.conversation_state.issues.get(second.task_id)["issue_id"])
    )


@pytest.mark.parametrize("relation", ["ack", "resolved"])
def test_polite_ack_does_not_extend_wait_or_mean_resolved(engine, relation):
    first, original = enqueue(engine, "订单87662609290266请核实漏发")
    reconcile(engine)
    followup, _ = enqueue(
        engine,
        "谢谢" if relation == "ack" else "已经收到，问题解决了",
        seconds=200,
        quote=original,
    )
    propose(engine, relation=relation)
    reconcile(engine, 400)
    issue = engine.conversation_state.issues.get(first.task_id)
    assert issue["latest_customer_at"] == START.isoformat()
    assert engine.conversation_state.get_task(followup.task_id).state == "closed"
    assert len(claim(engine, 1800)) == (1 if relation == "ack" else 0)
    assert issue["state"] == ("open" if relation == "ack" else "closed")


def test_staff_quote_takes_over_only_related_issue_and_later_supplement_inherits_it(engine):
    first, original = enqueue(engine, "订单87662609290266请核实漏发")
    second, _ = enqueue(engine, "请核实是不是漏发了", seconds=17)
    propose(engine)
    reconcile(engine)
    independent, _ = enqueue(engine, "订单87662609290267需要发票", seconds=20)
    # Do not ask the model to merge the independent order.
    engine.model_client.associate_demands = lambda items, candidates, **kwargs: []
    reconcile(engine, 200)
    receipt = engine.conversation_state.record_staff_message(
        chat_id=ROOM,
        staff_id="staff",
        external_event_id="staff-reply",
        received_at=START + timedelta(seconds=210),
        text="我来核实",
        mentioned_bot=False,
        quoted_msg_id=original,
        precise_issue_target=True,
    )
    assert set(receipt.task_ids) == {first.task_id, second.task_id}
    assert engine.conversation_state.get_task(independent.task_id).state == "waiting_quiet"
    supplement, _ = enqueue(engine, "再帮忙核实一下数量", seconds=220, quote=original)
    target = engine.conversation_state.issues.get(first.task_id)["issue_id"]
    engine.model_client.associate_demands = lambda items, candidates, **kwargs: [
        {"task_id": items[0]["task_id"], "relation": "renew", "target_id": target, "confidence": 1}
    ]
    reconcile(engine, 400)
    assert engine.conversation_state.get_task(supplement.task_id).state == "human_active"
    assert [task.task_id for task in claim(engine)] == [independent.task_id]


@pytest.mark.parametrize("relation", ["continue", "renew"])
def test_supplement_after_reply_is_suppressed_but_explicit_new_round_is_allowed(engine, relation):
    first, original = enqueue(engine, "订单87662609290266请核实漏发")
    reconcile(engine)
    engine.process_conversation_task(claim(engine)[0])
    followup, _ = enqueue(engine, "订单87662609290266再次请求核实", seconds=4000, quote=original)
    propose(engine, relation=relation)
    reconcile(engine, 4200)
    tasks = claim(engine, 6000)
    assert len(tasks) == (1 if relation == "renew" else 0)
    assert (
        engine.conversation_state.issues.get(first.task_id)["issue_id"]
        == engine.conversation_state.issues.get(followup.task_id)["issue_id"]
    ) == (relation == "continue")


def test_service_restart_during_send_never_replays_uncertain_acknowledgement(engine):
    first, _ = enqueue(engine, "请核实漏发")
    reconcile(engine)
    claim(engine)
    issues = engine.conversation_state.issues
    issue = issues.get(first.task_id)
    assert issues.can_send(issue["issue_id"], issue["version"], soothe=True)
    restarted = ConversationStateStore(engine.conversation_state.db_path)
    restarted.issues.recover_interrupted()
    assert restarted.issues.get(first.task_id)["soothe_status"] == "unknown"
    assert restarted.get_task(first.task_id).state == "responded"
    assert restarted.claim_ready_tasks(now=START + timedelta(hours=3)) == []


def test_service_restart_before_send_releases_claims(engine):
    first, _ = enqueue(engine, "请核实漏发")
    reconcile(engine)
    claim(engine)
    engine.conversation_state.issues.recover_interrupted()
    assert engine.conversation_state.get_task(first.task_id).state == "waiting_quiet"
    assert len(claim(engine)) == 1


@pytest.mark.parametrize("mutation", ["staff", "revoke", "new_callback"])
def test_change_during_planning_invalidates_outbound_without_leaving_stuck_claims(engine, mutation):
    first, original = enqueue(engine, "订单87662609290266请核实漏发")
    second, _ = enqueue(engine, "请核实是不是漏发了", seconds=17)
    propose(engine)
    reconcile(engine)
    task = claim(engine)[0]

    def query(event, **kwargs):
        if mutation == "staff":
            engine.conversation_state.record_staff_message(
                chat_id=ROOM,
                staff_id="staff",
                external_event_id="staff-reply",
                received_at=START + timedelta(seconds=4000),
                text="我处理",
                mentioned_bot=False,
                quoted_msg_id=original,
                precise_issue_target=True,
            )
        elif mutation == "revoke":
            engine.conversation_state.close_revoked_message(
                chat_id=ROOM, external_event_id=original
            )
        else:
            enqueue(engine, "还有签收照片", seconds=4000)
        return ReplyDeliverable(text="收到", channel_used="soothe")

    engine._process_text_query = query
    engine.process_conversation_task(task)
    assert engine.requests == []
    assert engine.conversation_state.get_task(second.task_id).state != "processing"
    assert engine.conversation_state.issues.get(first.task_id)["soothe_status"] == "none"


def test_attachment_xml_is_described_without_transport_secrets_or_losing_plain_text():
    xml = "<msg><appmsg><title>签收照片</title><aeskey>private-key</aeskey></appmsg></msg>"
    assert association_text(xml) == "[附件：签收照片]"
    assert association_text("长文本" * 1000, limit=None) == "长文本" * 1000


def test_duplicate_model_links_and_nonfinite_confidence_are_rejected():
    value = [
        {"task_id": "a", "relation": "continue", "target_id": "b", "confidence": 0.2},
        {"task_id": "a", "relation": "continue", "target_id": "b", "confidence": 1},
        {"task_id": "c", "relation": "continue", "target_id": "b", "confidence": float("nan")},
    ]
    assert validated_links(value, {"a", "c"}) == {}


def test_unknown_send_and_late_log_failure_do_not_retry(engine):
    enqueue(engine, "请核实漏发")
    reconcile(engine)
    task = claim(engine)[0]

    def broken_log(**kwargs):
        raise sqlite3.OperationalError("disk full after platform accepted")

    engine.log_group_outbound = broken_log
    engine.process_conversation_task(task)
    assert len(engine.requests) == 1
    assert engine.conversation_state.issues.get(task.task_id)["soothe_status"] == "unknown"
    assert claim(engine) == []


def test_associated_attachment_is_restored_after_restart_and_not_transport_xml(engine, tmp_path):
    first, original = enqueue(engine, "订单87662609290266请核实是不是漏发了")
    xml = "<msg><appmsg><title>签收照片</title><aeskey>secret-key</aeskey></appmsg></msg>"
    enqueue(engine, xml, seconds=17, quote=original)
    engine.context_buffer = ContextBuffer()
    engine.inbound_media_store = InboundMediaStore(tmp_path / "media", tmp_path / "media.db")
    engine.inbound_media_store.stage(
        media_id="picture",
        message_id="10000001",
        chat_id=ROOM,
        sender_id="customer@openim",
        kind="image",
        mime_type="image/png",
        content=b"image-bytes",
    )
    propose(engine)
    reconcile(engine)

    def query(event, **kwargs):
        media = engine.context_buffer.get_task_media(
            chat_id=ROOM,
            sender_id=ROOM,
            task_id=first.task_id,
        )
        assert len(media) == 1 and media[0].image_data == b"image-bytes"
        assert "签收照片" in event.text and "secret-key" not in event.text
        return ReplyDeliverable(text="收到", channel_used="soothe")

    engine._process_text_query = query
    engine.process_conversation_task(claim(engine)[0])
    assert len(engine.requests) == 1
    assert engine.conversation_state.get_task(first.task_id).state == "responded"


def test_unassigned_backlog_cannot_bypass_association_claim_gate(engine):
    enqueue(engine, "请核实漏发")
    assert (
        engine.conversation_state.claim_ready_tasks(
            now=START + timedelta(hours=1),
            require_issue_chats=(ROOM,),
        )
        == []
    )
    reconcile(engine)
    assert (
        len(
            engine.conversation_state.claim_ready_tasks(
                now=START + timedelta(hours=1),
                require_issue_chats=(ROOM,),
            )
        )
        == 1
    )


def test_timeout_records_unknown_once_and_blocks_later_supplement(engine):
    first, original = enqueue(engine, "订单87662609290266请核实漏发")
    reconcile(engine)

    def transport(request):
        engine.requests.append(request)
        raise httpx.ReadTimeout("response lost", request=request)

    engine.wechat._client.close()
    engine.wechat._client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(transport)
    )
    engine.process_conversation_task(claim(engine)[0])
    assert len(engine.requests) == 1
    assert engine.conversation_state.issues.get(first.task_id)["soothe_status"] == "unknown"
    enqueue(engine, "订单87662609290266补充了照片", seconds=4000, quote=original)
    propose(engine)
    reconcile(engine, 4200)
    assert claim(engine, 6000) == []


def test_explicit_staff_handback_reopens_entire_issue_without_sending_for_other_order(engine):
    first, original = enqueue(engine, "订单87662609290266请核实漏发")
    second, _ = enqueue(engine, "请核实是不是漏发了", seconds=17)
    propose(engine)
    reconcile(engine)
    for task_id in (first.task_id, second.task_id):
        engine.conversation_state.set_state(task_id, "human_active")
    receipt = engine.conversation_state.record_staff_message(
        chat_id=ROOM,
        staff_id="staff",
        external_event_id="staff-handback",
        received_at=START + timedelta(seconds=160),
        text="@机器人 请协助核实",
        mentioned_bot=True,
        quoted_msg_id=original,
        precise_issue_target=True,
    )
    assert set(receipt.task_ids) == {first.task_id, second.task_id}
    assert engine.conversation_state.issues.get(first.task_id)["state"] == "open"
    # Existing staff-priority gate still waits for a new customer message.
    assert claim(engine) == []


def test_repeated_urging_before_first_response_does_not_start_a_second_round(engine):
    first, _ = enqueue(engine, "订单87662609290266请核实漏发")
    second, _ = enqueue(engine, "请再帮忙催一下", seconds=17)
    propose(engine, relation="renew")
    reconcile(engine)
    assert (
        engine.conversation_state.issues.get(first.task_id)["issue_id"]
        == (engine.conversation_state.issues.get(second.task_id)["issue_id"])
    )
    assert len(claim(engine)) == 1


def staff_event(engine, text, *, seconds, quote="", message_id="staff-event"):
    return engine.record_conversation_event(InboundEvent(
        message_id=message_id, from_user=ROOM, to_user="bot", chat_id=ROOM,
        user_id="staff", sender_id="staff", sender_role="staff", is_group=True,
        msg_type=1, text=text, quoted_msg_id=quote,
        raw_payload={"received_at": (START + timedelta(seconds=seconds)).isoformat()},
    ))


def test_quoted_image_takes_over_related_demand_after_restart(engine):
    engine.context_buffer = ContextBuffer()
    engine.context_buffer.push_media(
        msg_id="deduction-image", chat_id=ROOM, sender_id=ROOM, actor_id="customer@openim",
        kind="image", local_path="", media_id="image",
    )
    first, _ = enqueue(engine, "苏州仓抵扣了这个金额")
    second, _ = enqueue(engine, "核实一下", seconds=4)
    propose(engine)
    reconcile(engine)
    unrelated, _ = enqueue(engine, "另一个订单要发票", seconds=20, sender="other@openim")
    engine.conversation_state = ConversationStateStore(engine.conversation_state.db_path)
    receipt = staff_event(engine, "苏州仓核实无误", seconds=160, quote="deduction-image")
    assert set(receipt.task_ids) == {first.task_id, second.task_id}
    assert engine.conversation_state.get_task(first.task_id).human_owner_id == "staff"
    assert engine.conversation_state.get_task(unrelated.task_id).state == "waiting_quiet"
    assert engine.requests == []


def test_pending_group_image_is_not_bound_to_another_customer(engine):
    engine.context_buffer = ContextBuffer()
    engine.context_buffer.push_media(
        msg_id="image-a", chat_id=ROOM, sender_id=ROOM, actor_id="customer@openim",
        kind="image", local_path="", media_id="image",
    )
    other, _ = enqueue(engine, "我要发票", sender="other@openim")
    actual, _ = enqueue(engine, "抵扣这张图里的金额", seconds=1)
    assert engine.conversation_state.issues.message_ids(other.task_id) == []
    assert engine.conversation_state.issues.message_ids(actual.task_id) == ["image-a"]


def test_association_timeout_retries_after_restart_instead_of_splitting_soothes(engine):
    first, _ = enqueue(engine, "苏州仓抵扣金额需要核实")
    second, _ = enqueue(engine, "核实一下", seconds=4)

    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("test")

    engine.model_client.associate_demands = timeout
    reconcile(engine, 4000)
    store = engine.conversation_state
    assert store.issues.get(first.task_id) is None and store.issues.get(second.task_id) is None
    assert store.claim_ready_tasks(now=START + timedelta(hours=2),
                                   require_issue_chats=(ROOM,)) == []
    engine.conversation_state = ConversationStateStore(store.db_path)
    propose(engine)
    reconcile(engine, 4010)
    assert engine.conversation_state.issues.get(first.task_id) is None
    reconcile(engine, 4030)
    tasks = claim(engine, 4030)
    assert len(tasks) == 1
    engine.process_conversation_task(tasks[0])
    assert len(engine.requests) == 1


def test_unquoted_staff_reply_matches_only_suzhou_issue_in_busy_group(engine):
    first, original = enqueue(engine, "苏州仓抵扣金额需要核实")
    second, _ = enqueue(engine, "核实一下", seconds=4)
    propose(engine)
    reconcile(engine)
    other, _ = enqueue(engine, "我要另一个订单的发票", sender="other@openim", seconds=10)
    engine.model_client.associate_demands = lambda *args, **kwargs: []
    reconcile(engine, 200)
    receipt = staff_event(engine, "好，核对无误今天发出", seconds=210)
    assert receipt.task_ids == ()
    assert claim(engine) == []  # The review is durable and fences an early poll.
    engine.conversation_state = ConversationStateStore(engine.conversation_state.db_path)

    def match(staff, candidates, context):
        assert "苏州仓抵扣金额" in context and "role=staff" in context
        return [{"task_id": first.task_id, "confidence": 1,
                 "evidence_message_ids": [original]}]

    engine.model_client.match_staff_takeover = match
    reconcile(engine, 250)
    assert engine.conversation_state.get_task(first.task_id).state == "human_active"
    assert engine.conversation_state.get_task(second.task_id).state == "human_active"
    assert engine.conversation_state.get_task(second.task_id).human_owner_id == "staff"
    assert [task.task_id for task in claim(engine)] == [other.task_id]


@pytest.mark.parametrize(
    "failure", ["timeout", "unrelated_evidence", "low_confidence", "unknown_id"]
)
def test_staff_matching_failures_do_not_take_over_unrelated_work(engine, failure):
    first, original = enqueue(engine, "苏州仓核对抵扣")
    reconcile(engine)
    receipt = staff_event(engine, "好", seconds=160)

    def match(*args):
        if failure == "timeout":
            raise httpx.ReadTimeout("test")
        return [{"task_id": "unknown" if failure == "unknown_id" else first.task_id,
                 "confidence": 0.8 if failure == "low_confidence" else 1,
                 "evidence_message_ids": ["invented" if failure == "unrelated_evidence"
                                          else original]}]

    engine.model_client.match_staff_takeover = match
    reconcile(engine, 200)
    assert engine.conversation_state.get_task(first.task_id).state == "waiting_quiet"
    if failure in {"timeout", "unknown_id"}:
        assert claim(engine) == []
        assert engine.conversation_state.issues.pending_staff_reviews(
            [ROOM], now=(START + timedelta(hours=1)).isoformat()
        )[0]["event_id"] == receipt.event_id
    else:
        assert len(claim(engine)) == 1


def test_same_issue_after_long_gap_requires_real_daily_message_evidence(engine):
    first, original = enqueue(engine, "苏州仓抵扣金额需要核对")
    reconcile(engine)
    followup, reminder = enqueue(engine, "尽快核对，不要影响发货", seconds=360)

    def associate(items, candidates, **kwargs):
        assert original in kwargs["group_context"] and reminder in kwargs["group_context"]
        return [{"task_id": followup.task_id, "relation": "continue",
                 "target_id": candidates[0]["issue_id"], "confidence": 1,
                 "evidence_message_ids": [original, reminder]}]

    engine.model_client.associate_demands = associate
    reconcile(engine, 500)
    assert engine.conversation_state.issues.get(first.task_id)["issue_id"] == (
        engine.conversation_state.issues.get(followup.task_id)["issue_id"]
    )
    assert len(claim(engine)) == 1


def test_staff_review_received_while_planning_blocks_outbound_before_matching(engine):
    first, original = enqueue(engine, "苏州仓抵扣金额需要核实")
    reconcile(engine)
    task = claim(engine)[0]

    def query(event, **kwargs):
        staff_event(engine, "我在核实了", seconds=1900)
        return ReplyDeliverable(text="收到，我反馈核实一下", channel_used="soothe")

    engine._process_text_query = query
    engine.process_conversation_task(task)
    assert engine.requests == []
    assert engine.conversation_state.get_task(first.task_id).state == "waiting_quiet"


@pytest.mark.parametrize("quoted", [True, False])
def test_staff_can_take_over_answered_issue_without_reopening_delivery(engine, quoted):
    first, original = enqueue(engine, "苏州仓抵扣金额请核实")
    reconcile(engine)
    engine.process_conversation_task(claim(engine)[0])
    engine.model_client.match_staff_takeover = lambda *args: [
        {"task_id": first.task_id, "confidence": 1, "evidence_message_ids": [original]}
    ]
    receipt = staff_event(engine, "这笔我来核实", seconds=4100,
                          quote=original if quoted else "")
    reconcile(engine, 4200)
    store = engine.conversation_state
    issue = store.issues.get(first.task_id)
    assert issue["state"] == "human_active" and issue["soothe_status"] == "sent"
    assert store.get_task(first.task_id).state == "responded"
    assert store.get_task(first.task_id).human_owner_id == "staff"
    assert store.issues.referenced_tasks(ROOM, "staff-event") == {first.task_id}
    assert receipt.task_ids == ()  # No historical task is queued for another delivery.

    engine.conversation_state = ConversationStateStore(store.db_path)
    followup, _ = enqueue(engine, "这笔还没核实好，麻烦继续跟进", seconds=4300, quote=original)
    propose(engine, relation="renew")
    reconcile(engine, 4450)
    assert engine.conversation_state.get_task(followup.task_id).state == "human_active"
    assert engine.conversation_state.get_task(followup.task_id).human_owner_id == "staff"
    assert claim(engine, 7000) == []
    assert len(engine.requests) == 1

    unrelated, _ = enqueue(engine, "另外一笔订单需要发票", seconds=4500)
    engine.model_client.associate_demands = lambda *args, **kwargs: []
    reconcile(engine, 4700)
    assert [task.task_id for task in claim(engine, 7000)] == [unrelated.task_id]


def test_explicit_handback_of_answered_issue_survives_delayed_older_staff_reply(engine):
    first, original = enqueue(engine, "苏州仓抵扣金额请核实")
    reconcile(engine)
    engine.process_conversation_task(claim(engine)[0])
    staff_event(engine, "我来核实", seconds=4100, quote=original)
    store = engine.conversation_state
    receipt = store.record_staff_message(
        chat_id=ROOM, staff_id="staff", external_event_id="handback", text="@机器人 请协助",
        received_at=START + timedelta(seconds=4250), mentioned_bot=True,
        quoted_msg_id=original, precise_issue_target=True,
    )
    assert receipt.task_ids == ()
    assert store.issues.get(first.task_id)["state"] == "open"
    assert store.get_task(first.task_id).state == "responded"
    staff_event(engine, "旧的核实回复迟到", seconds=4200, quote=original, message_id="delayed")
    reconcile(engine, 4260)
    assert store.issues.get(first.task_id)["state"] == "open"
    expected_staff_at = (START + timedelta(seconds=4250)).isoformat()
    assert store.get_task(first.task_id).last_staff_at == expected_staff_at
    followup, _ = enqueue(engine, "这笔请机器人再次核实", seconds=4300, quote=original)
    propose(engine, relation="renew")
    reconcile(engine, 4450)
    assert [task.task_id for task in claim(engine, 7000)] == [followup.task_id]


@pytest.mark.parametrize("transition", ["revoke", "closed", "blocked", "responded"])
def test_removing_last_unassigned_task_clears_retry_without_blocking_other_issue(
    engine, transition
):
    first, _ = enqueue(engine, "独立订单需要发票")
    reconcile(engine)
    second, original = enqueue(engine, "另一个订单核实漏发", seconds=160)
    store = engine.conversation_state
    store.issues.defer_association(ROOM, "customer@openim", now=START.isoformat())
    assert claim(engine, 4000) == []
    if transition == "revoke":
        store.close_revoked_message(chat_id=ROOM, external_event_id=original)
    else:
        store.set_state(second.task_id, transition)
    with store._connection() as conn:
        retries = conn.execute(
            "SELECT COUNT(*) FROM conversation_association_retries"
        ).fetchone()[0]
        assert retries == 0
    assert [task.task_id for task in claim(engine, 4000)] == [first.task_id]
    engine.process_conversation_task(store.get_task(first.task_id))
    assert len(engine.requests) == 1


@pytest.mark.parametrize("entrypoint", ["startup", "poll", "claim", "send"])
def test_existing_orphan_retry_is_recovered_at_state_and_send_boundaries(engine, entrypoint):
    first, _ = enqueue(engine, "独立订单需要发票")
    reconcile(engine)
    second, _ = enqueue(engine, "另一笔漏发核实", seconds=160)
    store = engine.conversation_state
    store.issues.defer_association(ROOM, "customer@openim", now=START.isoformat())
    # Reproduce an old deployment's leftover row without invoking new cleanup.
    with store._connection() as conn:
        conn.execute("UPDATE conversation_tasks SET state='closed' WHERE task_id=?",
                     (second.task_id,))
    if entrypoint == "startup":
        engine.conversation_state = ConversationStateStore(store.db_path)
    elif entrypoint == "poll":
        reconcile(engine, 4000)
    elif entrypoint == "claim":
        assert [task.task_id for task in claim(engine, 4000)] == [first.task_id]
    else:
        issue = store.issues.get(first.task_id)
        assert store.issues.can_send(issue["issue_id"], issue["version"])
    with store._connection() as conn:
        retries = conn.execute(
            "SELECT COUNT(*) FROM conversation_association_retries"
        ).fetchone()[0]
        assert retries == 0


def test_revoking_one_message_keeps_retry_for_other_unassigned_work(engine):
    enqueue(engine, "正常事项")
    reconcile(engine)
    second, original = enqueue(engine, "漏发核实", seconds=160)
    third, _ = enqueue(engine, "补充漏发图片", seconds=170)
    store = engine.conversation_state
    store.issues.defer_association(ROOM, "customer@openim", now=START.isoformat())
    store.close_revoked_message(chat_id=ROOM, external_event_id=original)
    with store._connection() as conn:
        retries = conn.execute(
            "SELECT COUNT(*) FROM conversation_association_retries"
        ).fetchone()[0]
        assert retries == 1
    assert claim(engine, 4000) == []
    assert store.get_task(third.task_id).state == "waiting_quiet"


def test_revoke_during_model_timeout_does_not_recreate_an_orphan_retry(engine):
    first, _ = enqueue(engine, "正常事项")
    reconcile(engine)
    second, original = enqueue(engine, "漏发核实", seconds=160)

    def timeout(*args, **kwargs):
        engine.conversation_state.close_revoked_message(chat_id=ROOM, external_event_id=original)
        raise httpx.ReadTimeout("simulated race")

    engine.model_client.associate_demands = timeout
    reconcile(engine, 300)
    with engine.conversation_state._connection() as conn:
        retries = conn.execute(
            "SELECT COUNT(*) FROM conversation_association_retries"
        ).fetchone()[0]
        assert retries == 0
    assert [task.task_id for task in claim(engine, 4000)] == [first.task_id]


def test_review_of_answered_task_cannot_reopen_issue_closed_during_model_call(engine):
    first, original = enqueue(engine, "苏州仓抵扣金额请核实")
    reconcile(engine)
    engine.process_conversation_task(claim(engine)[0])
    staff_event(engine, "这笔我来核实", seconds=4100)
    store = engine.conversation_state
    issue_id = store.issues.get(first.task_id)["issue_id"]

    def match(*args):
        resolved, _ = enqueue(engine, "已经核实好了", seconds=4150, quote=original)
        store.issues.assign(resolved.task_id, target_id=issue_id, relation="resolved")
        return [{"task_id": first.task_id, "confidence": 1,
                 "evidence_message_ids": [original]}]

    engine.model_client.match_staff_takeover = match
    engine.reconcile_staff_takeovers([ROOM], now=(START + timedelta(seconds=4200)).isoformat())
    assert store.issues.get(first.task_id)["state"] == "closed"
    assert store.get_task(first.task_id).state == "responded"
    assert store.get_task(first.task_id).last_staff_at is None
    assert len(engine.requests) == 1


@pytest.mark.parametrize("deferred", [False, True])
def test_newer_handback_of_supplement_protects_older_answered_anchor(engine, deferred):
    first, original = enqueue(engine, "苏州仓抵扣金额请核实")
    reconcile(engine)
    engine.process_conversation_task(claim(engine)[0])
    staff_event(engine, "这笔我来核实", seconds=4100, quote=original)
    supplement, supplement_message = enqueue(engine, "这笔继续核实一下", seconds=4300,
                                             quote=original)
    propose(engine, relation="renew")
    reconcile(engine, 4430)
    store = engine.conversation_state

    def handback():
        store.record_staff_message(
            chat_id=ROOM, staff_id="staff", external_event_id="newer-handback",
            text="@机器人 请协助", received_at=START + timedelta(seconds=4450),
            mentioned_bot=True, quoted_msg_id=supplement_message, precise_issue_target=True,
        )

    if deferred:
        staff_event(engine, "旧的核实回复", seconds=4350, message_id="delayed")

        def match(*args):
            handback()
            return [{"task_id": first.task_id, "confidence": 1,
                     "evidence_message_ids": [original]}]

        engine.model_client.match_staff_takeover = match
        engine.reconcile_staff_takeovers([ROOM], now=(START + timedelta(seconds=4460)).isoformat())
    else:
        handback()
        staff_event(engine, "旧的核实回复", seconds=4350, quote=original, message_id="delayed")
    assert store.issues.get(first.task_id)["state"] == "open"
    assert store.get_task(first.task_id).state == "responded"
    assert store.get_task(supplement.task_id).state == "waiting_quiet"


def test_image_and_text_separated_by_restart_restore_bytes_and_persistent_takeover_links(
    engine, tmp_path
):
    engine.context_buffer = ContextBuffer()
    engine.inbound_media_store = InboundMediaStore(tmp_path / "media", tmp_path / "media.db")
    image_event = InboundEvent(
        message_id="persistent-image", from_user=ROOM, to_user="bot", chat_id=ROOM,
        user_id="customer@openim", sender_id="customer@openim", sender_role="customer",
        is_group=True, is_image=True, msg_type=3, text="",
        image_data=b"\x89PNG\r\n\x1a\nimage", raw_payload={"received_at": START.isoformat()},
    )
    engine.record_group_context_event(image_event)
    assert engine.process_image(image_event)
    engine.context_buffer = ContextBuffer()
    engine.conversation_state = ConversationStateStore(engine.conversation_state.db_path)
    first, _ = enqueue(engine, "请核实图片中抵扣金额", seconds=10)
    assert engine.conversation_state.issues.message_ids(first.task_id) == ["persistent-image"]
    reconcile(engine)
    engine.context_buffer = ContextBuffer()  # Another restart before the quiet-window worker.

    def query(event, **kwargs):
        media = engine.context_buffer.get_task_media(chat_id=ROOM, sender_id=ROOM,
                                                    task_id=first.task_id)
        assert len(media) == 1 and media[0].image_data == image_event.image_data
        return ReplyDeliverable(text="收到", channel_used="soothe")

    engine._process_text_query = query
    engine.process_conversation_task(claim(engine)[0])
    assert len(engine.requests) == 1
    staff_event(engine, "核实无误", seconds=4100, quote="persistent-image")
    assert engine.conversation_state.issues.get(first.task_id)["state"] == "human_active"


def test_pending_image_survives_beijing_midnight_cleanup(engine, tmp_path):
    engine.context_buffer = ContextBuffer()
    engine.inbound_media_store = InboundMediaStore(tmp_path / "media", tmp_path / "media.db")
    image_event = InboundEvent(
        message_id="midnight-image", from_user=ROOM, to_user="bot", chat_id=ROOM,
        user_id="customer@openim", sender_id="customer@openim", sender_role="customer",
        is_group=True, is_image=True, msg_type=3, text="",
        image_data=b"\x89PNG\r\n\x1a\nimage",
        raw_payload={"received_at": (START - timedelta(hours=8, seconds=1)).isoformat()},
    )
    engine.record_group_context_event(image_event)
    assert engine.process_image(image_event)
    engine.context_buffer = ContextBuffer()
    first, _ = enqueue(engine, "核实这张图的金额")
    assert engine.conversation_state.issues.message_ids(first.task_id) == ["midnight-image"]
    assert engine.inbound_media_store.find_by_message(chat_id=ROOM, sender_id=ROOM,
                                                     message_id="midnight-image") is not None


def test_download_finishing_after_text_binds_cache_to_original_task_only(
    engine, tmp_path, monkeypatch
):
    engine.context_buffer = ContextBuffer()
    engine.inbound_media_store = InboundMediaStore(tmp_path / "media", tmp_path / "media.db")
    created = []

    def download(**kwargs):
        created.append(enqueue(engine, "核实图中金额", seconds=10)[0])
        return b"\x89PNG\r\n\x1a\nimage"

    monkeypatch.setattr(engine.wechat, "download_inbound_media", download)
    event = InboundEvent(
        message_id="slow-image", from_user=ROOM, to_user="bot", chat_id=ROOM,
        user_id="customer@openim", sender_id="customer@openim", is_group=True,
        is_image=True, msg_type=3, text="",
        raw_payload={"received_at": START.isoformat(), "Data": {"Content": "<msg><img/></msg>"}},
    )
    assert engine.process_image(event)
    later, _ = enqueue(engine, "另一笔订单需要发票", seconds=20)
    first = created[0]
    assert engine.conversation_state.issues.message_ids(first.task_id) == ["slow-image"]
    assert engine.conversation_state.issues.message_ids(later.task_id) == []
    assert len(engine.context_buffer.get_task_media(chat_id=ROOM, sender_id=ROOM,
                                                  task_id=first.task_id)) == 1


def test_revoked_cached_image_cannot_be_rebound_by_memory_fallback(engine, tmp_path):
    engine.context_buffer = ContextBuffer()
    engine.inbound_media_store = InboundMediaStore(tmp_path / "media", tmp_path / "media.db")
    event = InboundEvent(
        message_id="revoked-image", from_user=ROOM, to_user="bot", chat_id=ROOM,
        user_id="customer@openim", sender_id="customer@openim", is_group=True,
        is_image=True, msg_type=3, text="", image_data=b"\x89PNG\r\n\x1a\nimage",
        raw_payload={"received_at": START.isoformat()},
    )
    assert engine.process_image(event)
    engine.conversation_state.close_revoked_message(
        chat_id=ROOM, external_event_id=event.message_id
    )
    later, _ = enqueue(engine, "核实金额", seconds=10)
    assert engine.conversation_state.issues.message_ids(later.task_id) == []
    assert engine.context_buffer.get_task_media(chat_id=ROOM, sender_id=ROOM,
                                              task_id=later.task_id) == []
