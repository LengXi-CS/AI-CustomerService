"""Replay customer bursts through durable association, claims and actual dispatch."""

import json
import sqlite3
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
from fastgpt_assistant.orchestration.conversation_state import ConversationStateStore
from fastgpt_assistant.orchestration.demand_association import association_text, validated_links
from fastgpt_assistant.orchestration.engine import AssistantEngine, InboundEvent, ReplyDeliverable
from fastgpt_assistant.orchestration.inbound_media_store import InboundMediaStore
from fastgpt_assistant.orchestration.payload_vault import PayloadVault, generate_payload_vault_key

ROOM = "issue-test@chatroom"
START = datetime(2026, 10, 13, 0, 0, tzinfo=UTC)


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
    result.model_client = SimpleNamespace(associate_demands=lambda items, candidates: [])
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
    receipt = store.record_customer_message(
        chat_id=ROOM,
        customer_id=sender,
        external_event_id=message_id,
        received_at=at,
        text=text,
        demand_count=len(demands) if demands else 1,
    )
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
    )
    assert engine.persist_pending_group_event(event, event_id=receipt.event_id, demands=demands)
    return receipt, message_id


def propose(engine, *, relation="continue", confidence=1):
    def associate(items, candidates):
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
    engine.model_client.associate_demands = lambda items, candidates: []
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
    engine.model_client.associate_demands = lambda items, candidates: [
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
            sender_id="customer@openim",
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
