from fastgpt_assistant.core.schema import QueryPlan
from fastgpt_assistant.orchestration.engine import AssistantEngine, ReplyDeliverable
from fastgpt_assistant.orchestration.query_routing import decide_query_route


def _plan(
    *,
    channel: str = "structured",
    intent_confidence: float = 0.9,
    completeness: float | None = None,
) -> QueryPlan:
    return QueryPlan(
        channel=channel,
        operation="record_value" if channel == "structured" else "clarify",
        scope="sku" if channel == "structured" else None,
        intent_confidence=intent_confidence,
        completeness=(
            intent_confidence if completeness is None else completeness
        ),
    )


def test_high_confidence_standard_query_is_normal() -> None:
    decision = decide_query_route(_plan(intent_confidence=0.80), "查询 SKU")

    assert decision.kind == "normal"
    assert decision.reason == "intent_and_entity_normal"


def test_standard_incomplete_query_is_clarified() -> None:
    decision = decide_query_route(_plan(channel="clarify", intent_confidence=0.90), "查一下产品")

    assert decision.kind == "clarify"
    assert decision.reason == "standard_intent_incomplete"


def test_medium_intent_confidence_requests_human_help() -> None:
    """The medium tier only fires when there is no entity to clarify.

    A structured plan with low completeness is diverted to clarify first, so the
    human-assist tier is reachable via narrative/direct (no entity to be missing).
    """
    decision = decide_query_route(
        _plan(channel="narrative", intent_confidence=0.50), "查一下这个产品"
    )

    assert decision.kind == "human"
    assert decision.reason == "intent_confidence_human_assist"
    assert decision.reply_text == "我暂时无法准确确认您的需求，请稍等。"


def test_clarify_gate_outranks_the_medium_intent_human_gate() -> None:
    """Owner decision 2026-09-18: entity clarify must beat "please @ a human".

    Measured shapes that motivated the reorder:
    `查一下产品` → intent 0.55 / completeness 0.20,
    `这两款检测报告麻烦提供下` → intent 0.72 / completeness 0.25.
    Both are standard demands missing only their entity, and both used to be
    swallowed by the human-assist tier before ever reaching the clarify gate.
    """
    for intent, completeness in ((0.55, 0.20), (0.72, 0.25), (0.79, 0.10)):
        decision = decide_query_route(
            _plan(intent_confidence=intent, completeness=completeness),
            "这两款检测报告麻烦提供下",
        )
        assert decision.kind == "clarify", (intent, completeness)
        assert decision.reason == "entity_incomplete"


def test_planner_declared_clarify_outranks_the_medium_intent_human_gate() -> None:
    """`查一下产品` arrives as channel=clarify with intent 0.55 — still clarify."""
    decision = decide_query_route(
        _plan(channel="clarify", intent_confidence=0.55, completeness=0.20),
        "查一下产品",
    )

    assert decision.kind == "clarify"
    assert decision.reason == "standard_intent_incomplete"


def test_silence_floor_still_outranks_every_clarify_gate() -> None:
    """Reordering must not hand back a reply we decided not to make."""
    decision = decide_query_route(
        _plan(intent_confidence=0.30, completeness=0.10), "嗯"
    )

    assert decision.kind == "silent"
    assert decision.reason == "intent_confidence_low"


def test_low_intent_confidence_query_is_silent() -> None:
    decision = decide_query_route(_plan(intent_confidence=0.49), "这是什么")

    assert decision.kind == "silent"
    assert decision.reason == "intent_confidence_low"
    assert decision.reply_text == ""


def test_explicit_human_request_overrides_confidence() -> None:
    decision = decide_query_route(_plan(intent_confidence=0.2), "我要转人工客服")

    assert decision.kind == "human"
    assert decision.reason == "explicit_human_request"


def test_clear_intent_with_unknown_entity_asks_instead_of_going_silent() -> None:
    """The regression this split exists for.

    Under the old fused score this plan was 0.10, i.e. below the silence floor:
    a well-understood demand ("send me those two test reports") produced no reply
    at all just because the picture could not pin the entity down.
    """
    decision = decide_query_route(
        _plan(intent_confidence=0.92, completeness=0.10),
        "这两款检测报告麻烦提供下",
    )

    assert decision.kind == "clarify"
    assert decision.reason == "entity_incomplete"


def test_entity_doubt_never_produces_silence() -> None:
    for completeness in (0.0, 0.2, 0.49, 0.79):
        decision = decide_query_route(
            _plan(intent_confidence=0.95, completeness=completeness),
            "查一下这个产品",
        )
        assert decision.kind == "clarify", completeness


def test_entity_completeness_gate_is_limited_to_structured_lookups() -> None:
    """narrative/direct replies have no entity to be missing."""
    decision = decide_query_route(
        _plan(channel="narrative", intent_confidence=0.9, completeness=0.2),
        "你们的官网怎么走",
    )

    assert decision.kind == "normal"


def test_legacy_single_confidence_expands_into_both_signals() -> None:
    plan = QueryPlan(
        channel="structured",
        operation="record_value",
        scope="sku",
        confidence=0.9,
    )

    assert plan.intent_confidence == 0.9
    assert plan.completeness == 0.9
    assert plan.confidence == 0.9


def test_single_signal_output_falls_back_to_the_value_provided() -> None:
    plan = QueryPlan(
        channel="structured",
        operation="record_value",
        scope="sku",
        intent_confidence=0.4,
    )

    assert plan.completeness == 0.4


def test_derived_confidence_is_the_conservative_minimum() -> None:
    plan = _plan(intent_confidence=0.9, completeness=0.3)

    assert plan.confidence == 0.3


def test_suppressed_reply_does_not_send() -> None:
    class FakeWechat:
        def __init__(self) -> None:
            self.calls = 0

        def send_text(self, **_kwargs) -> None:
            self.calls += 1

    engine = object.__new__(AssistantEngine)
    engine.wechat = FakeWechat()
    engine.dispatch_outbound(
        "room@chatroom",
        ReplyDeliverable(text="", channel_used="silent", suppress_outbound=True),
    )

    assert engine.wechat.calls == 0
