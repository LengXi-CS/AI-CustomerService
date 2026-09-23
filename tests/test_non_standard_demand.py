"""Non-standard demands: soothe the customer, privately remind the group owner.

2026-09-18 meeting decision (00:04:37): clarification belongs to *standard*
demands only — "命中标准需求但信息不足才澄清", to stop the 误澄清/乱澄清 the agent
was producing. A demand that matched no standard intent must therefore never be
answered with a clarifying question. It gets a warm, human acknowledgement plus
a private reminder to the group's owner.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from fastgpt_assistant.core.chat_log import ChatLogStore
from fastgpt_assistant.core.query_engine import ReadModel
from fastgpt_assistant.core.schema import QueryPlan
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.orchestration.context_buffer import ContextBuffer
from fastgpt_assistant.orchestration.engine import (
    AssistantEngine,
    InboundEvent,
    ReplyDeliverable,
    build_owner_notice,
)
from fastgpt_assistant.orchestration.query_routing import (
    NON_STANDARD_SOOTHE_REPLY,
    decide_query_route,
)

ROOM = "49195424680@chatroom"
OWNER = "wxid_owner"
CUSTOMER = "customer-a"
SOOTHE = "收到收到，这个我记下了，马上同步给负责人看一下，您放心。"


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #
def _plan(
    *,
    channel: str = "clarify",
    demand_kind: str = "non_standard",
    intent_confidence: float = 0.9,
    completeness: float = 0.9,
) -> QueryPlan:
    return QueryPlan(
        channel=channel,
        operation="clarify" if channel == "clarify" else "record_value",
        scope=None if channel == "clarify" else "sku",
        intent_confidence=intent_confidence,
        completeness=completeness,
        demand_kind=demand_kind,
    )


def test_non_standard_demand_is_soothed_not_clarified() -> None:
    decision = decide_query_route(_plan(), "帮我把这段话翻译成德文谢谢")

    assert decision.kind == "soothe"
    assert decision.reason == "non_standard_demand"


def test_soothe_route_leaves_the_wording_to_the_model() -> None:
    """A fixed template is the thing we are avoiding, so routing must not set it."""
    decision = decide_query_route(_plan(), "帮我投诉一下上周的物流")

    assert decision.reply_text == ""


def test_standard_demand_still_clarifies() -> None:
    decision = decide_query_route(
        _plan(demand_kind="standard"), "查一下产品"
    )

    assert decision.kind == "clarify"
    assert decision.reason == "standard_intent_incomplete"


def test_soothe_wins_over_the_medium_intent_human_fallback() -> None:
    """只安抚 is unconditional for a non-standard demand.

    The 5-8 confidence tier normally replies "请 @ 人工客服". For a non-standard
    demand that is group noise — the owner reminder already does the handoff.
    """
    decision = decide_query_route(
        _plan(intent_confidence=0.62), "你们能不能帮我改一下发票抬头"
    )

    assert decision.kind == "soothe"
    assert decision.reason == "non_standard_demand"


def test_silence_floor_still_wins_over_soothing() -> None:
    """Below the floor we have not confirmed a demand at all."""
    decision = decide_query_route(_plan(intent_confidence=0.30), "嗯")

    assert decision.kind == "silent"


def test_contradictory_structured_non_standard_plan_stays_on_the_standard_path() -> None:
    """channel == "structured" means a standard scope/operation WAS matched.

    The two signals contradict each other and the plan cannot be trusted, so the
    conservative reading wins: clarify the real lookup rather than replace it
    with a non-answer.
    """
    decision = decide_query_route(
        _plan(channel="structured", completeness=0.2), "查一下检测报告"
    )

    assert decision.kind == "clarify"
    assert decision.reason == "entity_incomplete"


def test_demand_kind_defaults_to_standard_for_legacy_plans() -> None:
    plan = QueryPlan(
        channel="clarify",
        operation="clarify",
        intent_confidence=0.9,
        completeness=0.9,
    )

    assert plan.demand_kind == "standard"


def test_demand_kind_rejects_unknown_values() -> None:
    with pytest.raises(ValueError):
        _plan(demand_kind="maybe")


# --------------------------------------------------------------------------- #
# owner reminder text
# --------------------------------------------------------------------------- #
def _groups_config(*, owner: str = OWNER, name: str = "测试客户群") -> WechatGroupsConfig:
    return WechatGroupsConfig.from_mapping(
        {
            "global": {"enabled": True, "reply_to_staff_when_mentioned": True},
            "groups": [
                {
                    "group_id": ROOM,
                    "group_name": name,
                    "mode": "active",
                    "require_customer_mention": False,
                    "owner_id": owner,
                }
            ],
        }
    )


def test_owner_notice_names_the_group_and_the_customer_wxid() -> None:
    notice = build_owner_notice(
        _groups_config(),
        chat_id=ROOM,
        sender_id=CUSTOMER,
        user_query="帮我改一下发票抬头",
    )

    assert "测试客户群" in notice
    assert CUSTOMER in notice
    assert "帮我改一下发票抬头" in notice


def test_owner_notice_is_empty_without_a_configured_owner() -> None:
    assert (
        build_owner_notice(
            _groups_config(owner=""), chat_id=ROOM, sender_id=CUSTOMER, user_query="x"
        )
        == ""
    )


def test_owner_notice_is_empty_for_a_private_chat() -> None:
    """A private chat has no group owner, so there is nobody to remind."""
    assert (
        build_owner_notice(
            _groups_config(), chat_id="wxid_someone", sender_id=CUSTOMER, user_query="x"
        )
        == ""
    )


def test_owner_notice_truncates_a_very_long_demand() -> None:
    notice = build_owner_notice(
        _groups_config(),
        chat_id=ROOM,
        sender_id=CUSTOMER,
        user_query="长" * 500,
    )

    assert "…" in notice
    assert "长" * 201 not in notice


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
class _ModelClient:
    def __init__(self, plan: QueryPlan, *, soothe_error: Exception | None = None) -> None:
        self.plan = plan
        self.soothe_error = soothe_error
        self.soothe_calls: list[str] = []

    def plan_query(self, user_query, *, history=None, image_context=""):
        return self.plan

    def analyze_image(self, image_data, *, mime_type):
        raise AssertionError("no image in these tests")

    def compose_reply(self, user_query, *, context="", history=None):
        return "通用回复"

    def compose_soothe_reply(self, user_query, *, history=None):
        self.soothe_calls.append(user_query)
        if self.soothe_error is not None:
            raise self.soothe_error
        return SOOTHE


class _Executor:
    def __init__(self) -> None:
        self.calls: list[QueryPlan] = []

    def execute_to_result(self, plan):
        self.calls.append(plan)
        raise AssertionError("a non-standard demand must never reach the executor")


class _Wechat:
    def __init__(self, *, fail_to: set[str] | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_to = fail_to or set()

    def send_text(self, *, to_wxid: str, content: str) -> None:
        if to_wxid in self.fail_to:
            raise RuntimeError("gewe down")
        self.sent.append((to_wxid, content))


def _read_model(tmp_path) -> ReadModel:
    path = tmp_path / "read-model.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE skus (sku_code TEXT PRIMARY KEY)")
    return ReadModel(path)


def _engine(
    plan: QueryPlan,
    tmp_path,
    *,
    soothe_error: Exception | None = None,
    owner_notify_enabled: bool = True,
) -> AssistantEngine:
    engine = object.__new__(AssistantEngine)
    engine.settings = SimpleNamespace(
        conversation_quiet_window_seconds=120,
        conversation_poll_interval_seconds=30,
        routing_normal_confidence_threshold=0.80,
        routing_human_confidence_threshold=0.50,
        owner_notify_enabled=owner_notify_enabled,
        attachment_dispatch_interval_seconds=0.0,
    )
    engine.context_buffer = ContextBuffer(media_ttl_seconds=180.0)
    engine.read_model = _read_model(tmp_path)
    engine.model_client = _ModelClient(plan, soothe_error=soothe_error)
    engine.query_executor = _Executor()
    engine.groups_config = _groups_config()
    engine.wechat = _Wechat()
    return engine


def _event(text: str, *, chat_id: str = ROOM, is_group: bool = True) -> InboundEvent:
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
        is_group=is_group,
        mentioned_bot=True,
    )


def test_non_standard_demand_replies_with_model_wording_not_a_template(tmp_path) -> None:
    engine = _engine(_plan(), tmp_path)

    reply = engine.process_text_query(_event("帮我改一下发票抬头"))

    assert reply.channel_used == "soothe"
    assert reply.text == SOOTHE
    assert reply.suppress_outbound is False
    # The executor is never consulted for a non-standard demand.
    assert engine.query_executor.calls == []
    assert engine.model_client.soothe_calls == ["帮我改一下发票抬头"]


def test_soothe_text_is_cleaned_of_markdown(tmp_path) -> None:
    engine = _engine(_plan(), tmp_path)
    engine.model_client.compose_soothe_reply = lambda user_query, history=None: (
        "**收到**，我看下。"
    )

    reply = engine.process_text_query(_event("帮我改一下发票抬头"))

    assert "**" not in reply.text
    assert "收到" in reply.text


def test_soothe_falls_back_when_the_model_is_unreachable(tmp_path) -> None:
    engine = _engine(_plan(), tmp_path, soothe_error=RuntimeError("llm down"))

    reply = engine.process_text_query(_event("帮我改一下发票抬头"))

    assert reply.text == NON_STANDARD_SOOTHE_REPLY
    assert reply.channel_used == "soothe"


def test_soothe_falls_back_when_the_model_returns_nothing(tmp_path) -> None:
    engine = _engine(_plan(), tmp_path)
    engine.model_client.compose_soothe_reply = lambda user_query, history=None: "   "

    reply = engine.process_text_query(_event("帮我改一下发票抬头"))

    assert reply.text == NON_STANDARD_SOOTHE_REPLY


def test_non_standard_demand_queues_an_owner_reminder(tmp_path) -> None:
    engine = _engine(_plan(), tmp_path)

    reply = engine.process_text_query(_event("帮我改一下发票抬头"))

    assert reply.owner_notify_text
    assert OWNER not in reply.owner_notify_text  # wxid stays out of the payload
    assert CUSTOMER in reply.owner_notify_text


def test_owner_reminder_is_sent_privately_not_into_the_group(tmp_path) -> None:
    engine = _engine(_plan(), tmp_path)
    reply = engine.process_text_query(_event("帮我改一下发票抬头"))

    engine.dispatch_outbound(ROOM, reply)

    targets = [target for target, _ in engine.wechat.sent]
    assert targets == [ROOM, OWNER]
    assert engine.wechat.sent[0][1] == SOOTHE


def test_owner_reminder_can_be_disabled(tmp_path) -> None:
    engine = _engine(_plan(), tmp_path, owner_notify_enabled=False)
    reply = engine.process_text_query(_event("帮我改一下发票抬头"))

    engine.dispatch_outbound(ROOM, reply)

    assert [target for target, _ in engine.wechat.sent] == [ROOM]


def test_owner_reminder_failure_does_not_break_the_customer_reply(tmp_path) -> None:
    engine = _engine(_plan(), tmp_path)
    engine.wechat.fail_to = {OWNER}
    reply = engine.process_text_query(_event("帮我改一下发票抬头"))

    engine.dispatch_outbound(ROOM, reply)  # must not raise

    assert [target for target, _ in engine.wechat.sent] == [ROOM]


def test_owner_reminder_is_skipped_for_an_unconfigured_chat(tmp_path) -> None:
    engine = _engine(_plan(), tmp_path)
    reply = engine.process_text_query(_event("帮我改一下发票抬头", chat_id="other@chatroom"))

    engine.dispatch_outbound("other@chatroom", reply)

    assert [target for target, _ in engine.wechat.sent] == ["other@chatroom"]


def test_dispatch_without_owner_notice_sends_only_the_group_text(tmp_path) -> None:
    engine = _engine(_plan(), tmp_path)

    engine.dispatch_outbound(
        ROOM, ReplyDeliverable(text="普通回复", channel_used="direct")
    )

    assert engine.wechat.sent == [(ROOM, "普通回复")]


def test_standard_demand_is_unaffected(tmp_path) -> None:
    """The existing clarify path must keep working exactly as before."""
    plan = QueryPlan(
        channel="clarify",
        operation="clarify",
        intent_confidence=0.9,
        completeness=0.9,
        demand_kind="standard",
    )
    engine = _engine(plan, tmp_path)

    reply = engine.process_text_query(_event("查一下产品"))

    assert reply.channel_used == "clarify"
    assert reply.owner_notify_text == ""


def test_demand_bundle_merges_owner_reminders(tmp_path) -> None:
    engine = _engine(_plan(), tmp_path)

    reply = engine.process_demand_bundle(_event("帮我改发票抬头"), ["帮我改发票抬头"])

    assert reply.owner_notify_text
    assert CUSTOMER in reply.owner_notify_text


# --------------------------------------------------------------------------- #
# soothing-wording mode (soothe_mode = fixed | model)
# --------------------------------------------------------------------------- #
FIXED_REPLY = "收到，我们加急处理"


def _fixed_wording_config(reply_text: str = FIXED_REPLY) -> WechatGroupsConfig:
    return WechatGroupsConfig.from_mapping(
        {
            "global": {
                "enabled": True,
                "soothe_mode": "fixed",
                "soothe_fixed_reply": reply_text,
            },
            "groups": [
                {"group_id": ROOM, "mode": "active", "owner_id": OWNER,
                 "require_customer_mention": False}
            ],
        }
    )


def test_fixed_mode_sends_the_configured_sentence(tmp_path) -> None:
    engine = _engine(_plan(), tmp_path)
    engine.groups_config = _fixed_wording_config()

    reply = engine.process_text_query(_event("帮我改一下发票抬头"))

    assert reply.channel_used == "soothe"
    assert reply.text == FIXED_REPLY
    # The whole point of the fixed mode: it costs no model call.
    assert engine.model_client.soothe_calls == []


def test_fixed_mode_still_reminds_the_owner(tmp_path) -> None:
    """The wording is fixed; the handoff is not optional."""
    engine = _engine(_plan(), tmp_path)
    engine.groups_config = _fixed_wording_config()

    reply = engine.process_text_query(_event("帮我改一下发票抬头"))

    assert reply.owner_notify_text
    assert CUSTOMER in reply.owner_notify_text


def test_fixed_mode_honours_a_custom_sentence(tmp_path) -> None:
    engine = _engine(_plan(), tmp_path)
    engine.groups_config = _fixed_wording_config("稍等，我这边马上安排人跟进。")

    reply = engine.process_text_query(_event("帮我改一下发票抬头"))

    assert reply.text == "稍等，我这边马上安排人跟进。"


def test_fixed_mode_falls_back_to_the_model_when_no_sentence_is_configured(
    tmp_path,
) -> None:
    """A blank fixed wording must not turn into an empty bubble."""
    engine = _engine(_plan(), tmp_path)
    engine.groups_config = WechatGroupsConfig(soothe_mode="fixed", soothe_fixed_reply="   ")

    assert engine.compose_soothe_text(user_query="帮我改一下发票抬头") == SOOTHE
    assert engine.model_client.soothe_calls == ["帮我改一下发票抬头"]


def test_model_mode_still_generates_the_wording(tmp_path) -> None:
    engine = _engine(_plan(), tmp_path)
    engine.groups_config = WechatGroupsConfig(soothe_mode="model")

    assert engine.compose_soothe_text(user_query="帮我改一下发票抬头") == SOOTHE
    assert engine.model_client.soothe_calls == ["帮我改一下发票抬头"]


def test_model_mode_keeps_the_fallback_when_the_model_is_unreachable(tmp_path) -> None:
    engine = _engine(_plan(), tmp_path, soothe_error=RuntimeError("llm down"))
    engine.groups_config = WechatGroupsConfig(soothe_mode="model")

    assert engine.compose_soothe_text(user_query="帮我改一下发票抬头") == (
        NON_STANDARD_SOOTHE_REPLY
    )


def test_fixed_mode_reply_reaches_the_group_chat_log(tmp_path) -> None:
    """The two 2026-09-23 changes have to agree end to end: the archived group
    record must show the same sentence the group actually received."""
    engine = _engine(_plan(), tmp_path)
    engine.groups_config = _fixed_wording_config()
    engine.chat_log = ChatLogStore(tmp_path / "chat-log.db")

    reply = engine.process_text_query(_event("帮我改一下发票抬头"))
    engine.dispatch_outbound(ROOM, reply)
    engine.log_group_outbound(to_wxid=ROOM, reply=reply, task_id="task-1")

    assert engine.wechat.sent[0] == (ROOM, FIXED_REPLY)
    rows = engine.chat_log.recent(chat_id=ROOM)
    assert [row.text for row in rows] == [FIXED_REPLY]
    assert rows[0].direction == "outbound"
    assert rows[0].sender_role == "assistant"
    assert rows[0].channel_used == "soothe"
    assert rows[0].task_id == "task-1"
