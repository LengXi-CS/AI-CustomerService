"""Standard demand, but the archive cannot answer it → tell the customer and
hand off to the group owner privately.

负责人决策（2026-09-18）：可以答资料不足，然后转人工。

The distinction this file protects: "we did not find it" is not the same claim as
"it does not exist". The bot must say the former, never the latter, and must not
leave the customer with silence either.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from fastgpt_assistant.core.models import ExecutionResult
from fastgpt_assistant.core.query_engine import ReadModel
from fastgpt_assistant.core.schema import QueryPlan
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.orchestration.context_buffer import ContextBuffer
from fastgpt_assistant.orchestration.engine import (
    INSUFFICIENT_DATA_FALLBACK_REPLY,
    AssistantEngine,
    InboundEvent,
    build_owner_notice,
    owner_notice_reason_for_failure,
)

ROOM = "49195424680@chatroom"
OWNER = "wxid_owner"
CUSTOMER = "customer-a"

NO_EVIDENCE_TEXT = "未找到相关数据或附件记录。"
HANDOFF_TEXT = "不好意思，这个资料我这边没查到，已经转给负责人帮您确认了，稍等哈。"


# --------------------------------------------------------------------------- #
# owner notice templates
# --------------------------------------------------------------------------- #
def _groups_config(*, owner: str = OWNER, name: str = "测试客户群") -> WechatGroupsConfig:
    return WechatGroupsConfig.from_mapping(
        {
            "global": {"enabled": True},
            "groups": [
                {
                    "group_id": ROOM,
                    "group_name": name,
                    "mode": "active",
                    "owner_id": owner,
                }
            ],
        }
    )


def test_no_evidence_notice_says_the_archive_had_no_record() -> None:
    notice = build_owner_notice(
        _groups_config(),
        chat_id=ROOM,
        sender_id=CUSTOMER,
        user_query="6976070460041 的检测报告",
        reason="no_evidence",
    )

    assert "资料不足" in notice
    assert "未检索到对应记录" in notice
    assert CUSTOMER in notice
    assert "6976070460041 的检测报告" in notice


def test_the_two_notice_templates_are_distinguishable() -> None:
    config = _groups_config()
    common = {"chat_id": ROOM, "sender_id": CUSTOMER, "user_query": "x"}

    soothe = build_owner_notice(config, **common, reason="non_standard")
    no_evidence = build_owner_notice(config, **common, reason="no_evidence")

    assert soothe != no_evidence
    assert "非标准需求" in soothe
    assert "资料不足" in no_evidence


def test_unknown_notice_reason_is_rejected_loudly() -> None:
    with pytest.raises(ValueError):
        build_owner_notice(
            _groups_config(), chat_id=ROOM, sender_id=CUSTOMER, user_query="x", reason="?"
        )


def test_fallback_wording_never_claims_the_record_does_not_exist() -> None:
    """Only "we could not find it" is true; "it does not exist" is not ours to say."""
    for forbidden in ("不存在", "没有该", "未取得", "无此"):
        assert forbidden not in INSUFFICIENT_DATA_FALLBACK_REPLY


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
class _ModelClient:
    def __init__(self, *, error: Exception | None = None, text: str = HANDOFF_TEXT) -> None:
        self.error = error
        self.text = text
        self.calls: list[str] = []
        self.compose_contexts: list[str] = []

    def plan_query(self, user_query, *, history=None, image_context=""):
        raise AssertionError("these tests build the plan directly")

    def analyze_image(self, image_data, *, mime_type):
        raise AssertionError("no image in these tests")

    def compose_reply(self, user_query, *, context="", history=None):
        self.compose_contexts.append(context)
        return f"通用回复（基于 {context[:40]}）"

    def compose_insufficient_data_reply(self, user_query, *, history=None):
        self.calls.append(user_query)
        if self.error is not None:
            raise self.error
        return self.text


class _Executor:
    def __init__(self, result: ExecutionResult) -> None:
        self.result = result
        self.calls: list[QueryPlan] = []

    def execute_to_result(self, plan: QueryPlan) -> ExecutionResult:
        self.calls.append(plan)
        return self.result


class _Wechat:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, *, to_wxid: str, content: str) -> None:
        self.sent.append((to_wxid, content))


def _read_model(tmp_path) -> ReadModel:
    path = tmp_path / "read-model.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE skus (sku_code TEXT PRIMARY KEY)")
    return ReadModel(path)


def _plan() -> QueryPlan:
    return QueryPlan(
        channel="structured",
        operation="file",
        scope="sku",
        select=["third_party_reports"],
        intent_confidence=0.90,
        completeness=0.90,
    )


def _engine(
    tmp_path,
    result: ExecutionResult,
    *,
    error: Exception | None = None,
    text: str = HANDOFF_TEXT,
) -> AssistantEngine:
    engine = object.__new__(AssistantEngine)
    engine.settings = SimpleNamespace(
        conversation_quiet_window_seconds=120,
        conversation_poll_interval_seconds=30,
        routing_normal_confidence_threshold=0.80,
        routing_human_confidence_threshold=0.50,
        owner_notify_enabled=True,
        attachment_dispatch_interval_seconds=0.0,
    )
    engine.context_buffer = ContextBuffer(media_ttl_seconds=180.0)
    engine.read_model = _read_model(tmp_path)
    engine.model_client = _ModelClient(error=error, text=text)
    engine.query_executor = _Executor(result)
    engine.groups_config = _groups_config()
    engine.wechat = _Wechat()
    return engine


def _event(text: str, *, chat_id: str = ROOM) -> InboundEvent:
    return InboundEvent(
        message_id="msg-1",
        from_user=chat_id,
        to_user="bot-wxid",
        chat_id=chat_id,
        user_id=CUSTOMER,
        msg_type=1,
        text=text,
        sender_id=CUSTOMER,
        sender_role="customer",
        is_group=chat_id.endswith("@chatroom"),
        mentioned_bot=True,
    )


def _no_evidence() -> ExecutionResult:
    return ExecutionResult(status="no_evidence", text=NO_EVIDENCE_TEXT)


class _PlanModelClient(_ModelClient):
    """Supplies a fixed plan so the executor stub is the thing under test."""

    def __init__(self, plan: QueryPlan, **kwargs) -> None:
        super().__init__(**kwargs)
        self.plan = plan

    def plan_query(self, user_query, *, history=None, image_context=""):
        return self.plan


def _wired_engine(tmp_path, result: ExecutionResult, **kwargs) -> AssistantEngine:
    engine = _engine(tmp_path, result, **kwargs)
    engine.model_client = _PlanModelClient(_plan(), **kwargs)
    return engine


def test_no_evidence_reply_is_generated_and_handed_to_the_owner(tmp_path) -> None:
    engine = _wired_engine(tmp_path, _no_evidence())

    reply = engine.process_text_query(_event("6976070460041 的检测报告"))

    assert reply.channel_used == "structured_no_evidence"
    assert reply.text == HANDOFF_TEXT
    assert reply.suppress_outbound is False
    assert reply.owner_notify_text
    assert "资料不足" in reply.owner_notify_text
    assert engine.model_client.calls == ["6976070460041 的检测报告"]


def test_no_evidence_reminder_is_sent_privately(tmp_path) -> None:
    engine = _wired_engine(tmp_path, _no_evidence())
    reply = engine.process_text_query(_event("6976070460041 的检测报告"))

    engine.dispatch_outbound(ROOM, reply)

    assert [target for target, _ in engine.wechat.sent] == [ROOM, OWNER]
    assert engine.wechat.sent[0][1] == HANDOFF_TEXT


def test_a_failing_composer_still_hands_off_with_the_fallback_wording(tmp_path) -> None:
    engine = _wired_engine(tmp_path, _no_evidence(), error=RuntimeError("llm down"))

    reply = engine.process_text_query(_event("6976070460041 的检测报告"))

    assert reply.text == INSUFFICIENT_DATA_FALLBACK_REPLY
    assert reply.owner_notify_text  # the handoff must survive an LLM outage


def test_an_empty_composer_result_also_falls_back(tmp_path) -> None:
    engine = _wired_engine(tmp_path, _no_evidence(), text="   ")

    reply = engine.process_text_query(_event("6976070460041 的检测报告"))

    assert reply.text == INSUFFICIENT_DATA_FALLBACK_REPLY


def test_executor_error_also_hands_off(tmp_path) -> None:
    engine = _wired_engine(
        tmp_path, ExecutionResult(status="error", text="查询执行异常")
    )

    reply = engine.process_text_query(_event("6976070460041 的检测报告"))

    assert reply.channel_used == "structured_no_evidence"
    assert reply.owner_notify_text


def test_executor_clarify_does_not_hand_off_to_a_human(tmp_path) -> None:
    """The executor asking a clarifying question is not a data gap."""
    engine = _wired_engine(
        tmp_path, ExecutionResult(status="clarify", text="请补充查询信息。")
    )

    reply = engine.process_text_query(_event("查一下报告"))

    assert reply.channel_used == "structured"
    assert reply.text == "请补充查询信息。"
    assert reply.owner_notify_text == ""


def test_a_successful_lookup_queues_no_owner_reminder(tmp_path) -> None:
    engine = _wired_engine(
        tmp_path,
        ExecutionResult(
            status="answered",
            text="",
            metadata={"data": {"source": {"scope": "sku", "fields": {"sku_code": "X"}}}},
        ),
    )

    reply = engine.process_text_query(_event("查一下 SKU X"))

    assert reply.owner_notify_text == ""


def test_private_chat_gets_the_answer_but_no_owner_reminder(tmp_path) -> None:
    engine = _wired_engine(tmp_path, _no_evidence())

    reply = engine.process_text_query(
        _event("6976070460041 的检测报告", chat_id="wxid_customer")
    )

    assert reply.text == HANDOFF_TEXT
    assert reply.owner_notify_text == ""


def test_owner_reminder_can_be_disabled_for_no_evidence(tmp_path) -> None:
    engine = _wired_engine(tmp_path, _no_evidence())
    engine.settings.owner_notify_enabled = False
    reply = engine.process_text_query(_event("6976070460041 的检测报告"))

    engine.dispatch_outbound(ROOM, reply)

    assert [target for target, _ in engine.wechat.sent] == [ROOM]


# --------------------------------------------------------------------------- #
# internal validation text must never reach a customer
# --------------------------------------------------------------------------- #
INTERNAL_MESSAGE = "file 和 validity 操作必须选择一个已注册的附件字段。"


def _rejected(reason: str, message: str = INTERNAL_MESSAGE) -> ExecutionResult:
    return ExecutionResult(
        status="clarify",
        text=message,
        metadata={"clarify": {"reason": reason, "question": message}},
    )


@pytest.mark.parametrize(
    "reason",
    [
        "missing_entity",
        "too_many_entities",
        "missing_attachment_field",
        "invalid_filter",
        "missing_scope",
        "unknown_scope",
    ],
)
def test_plan_rejection_is_never_forwarded_to_the_customer(reason: str, tmp_path) -> None:
    """实测：`把周口美腾实业有限公司的消防验收报告发我。` 曾把内部校验文案
    原样发给客户。`QueryPlanRejected` 与规划器自己的澄清问句共用
    status="clarify"，所以必须按 reason 分流。"""
    engine = _wired_engine(tmp_path, _rejected(reason))

    reply = engine.process_text_query(_event("把周口美腾实业有限公司的消防验收报告发我。"))

    assert INTERNAL_MESSAGE not in reply.text
    assert reply.text == HANDOFF_TEXT
    assert reply.channel_used == "structured_no_evidence"
    assert "检索未完成" in reply.owner_notify_text


def test_planner_clarify_question_is_still_forwarded(tmp_path) -> None:
    """非校验类澄清是规划器写给客户的话，应照常转发、不转人工。"""
    engine = _wired_engine(
        tmp_path,
        ExecutionResult(
            status="clarify",
            text="请补充具体的查询对象。",
            metadata={"clarify": {"reason": "planner_clarification"}},
        ),
    )

    reply = engine.process_text_query(_event("查一下"))

    assert reply.text == "请补充具体的查询对象。"
    assert reply.channel_used == "structured"
    assert reply.owner_notify_text == ""


def test_owner_notice_reason_helper_classifies_each_failure_shape() -> None:
    assert owner_notice_reason_for_failure(_no_evidence()) == "no_evidence"
    assert (
        owner_notice_reason_for_failure(ExecutionResult(status="error", text="boom"))
        == "unanswerable"
    )
    assert owner_notice_reason_for_failure(_rejected("missing_entity")) == "unanswerable"
    assert (
        owner_notice_reason_for_failure(
            ExecutionResult(
                status="clarify",
                text="请补充具体的查询对象。",
                metadata={"clarify": {"reason": "planner_clarification"}},
            )
        )
        == ""
    )
    assert owner_notice_reason_for_failure(ExecutionResult(status="answered")) == ""


# --------------------------------------------------------------------------- #
# count results must reach the composer
# --------------------------------------------------------------------------- #
def _count_only(count: int) -> ExecutionResult:
    return ExecutionResult(
        status="answered",
        text="",
        metadata={"data": {"count": count, "records": [], "items": [], "source": None}},
    )


def test_count_only_result_is_handed_to_the_composer_as_a_fact(tmp_path) -> None:
    """实测缺陷：计数计划的信封只有 {count: N}，engine 从不读 count，
    于是 context_blocks 为空、raw_answer 退回假文案"查询成功。"，模型只能答
    "暂未记录该信息" —— `周口美腾实业有限公司有几份医护级认证？` 就栽在这里。"""
    engine = _wired_engine(tmp_path, _count_only(4))

    reply = engine.process_text_query(_event("周口美腾实业有限公司有几份医护级认证？"))

    assert reply.channel_used == "structured"
    assert engine.model_client.compose_contexts == ["【匹配记录数】\n· 共 4 条"]
    assert reply.owner_notify_text == ""


def test_an_answered_result_with_no_facts_at_all_hands_off(tmp_path) -> None:
    """No fabricated "查询成功。" context is fed to the model any more."""
    engine = _wired_engine(tmp_path, ExecutionResult(status="answered", text=""))

    reply = engine.process_text_query(_event("查一下某资料"))

    assert reply.channel_used == "structured_no_evidence"
    assert reply.text == HANDOFF_TEXT
    assert engine.model_client.compose_contexts == []


def test_answered_text_is_still_used_as_a_fact_source(tmp_path) -> None:
    """exec_res.text 是真实事实来源，不能被丢弃。"""
    engine = _wired_engine(
        tmp_path, ExecutionResult(status="answered", text="【商品信息】\n· SKU编码：X")
    )

    reply = engine.process_text_query(_event("查一下 SKU X"))

    assert reply.channel_used == "structured"
    assert engine.model_client.compose_contexts == ["【商品信息】\n· SKU编码：X"]
    assert reply.owner_notify_text == ""
