"""Deterministic post-planner routing for normal, clarify, human, silent, soothe."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fastgpt_assistant.core.schema import QueryPlan

RouteKind = Literal["normal", "clarify", "human", "silent", "soothe"]

_HUMAN_ASSIST_REPLY = "我暂时无法准确确认您的需求，请稍等。"

# 非标准需求只安抚，不反问。澄清属于标准需求，见 decide_query_route 的说明。
NON_STANDARD_SOOTHE_REPLY = (
    "收到，您反馈的情况我已经记录下来了，会同步给负责人尽快跟进，感谢您的耐心等待。"
)


@dataclass(frozen=True, slots=True)
class QueryRouteDecision:
    """Safe routing decision made after validating the planner output."""

    kind: RouteKind
    reason: str
    reply_text: str = ""


def _is_explicit_human_request(query: str) -> bool:
    normalized = "".join(query.split()).lower()
    phrases = (
        "转人工",
        "找人工",
        "人工客服",
        "真人客服",
        "联系客服",
        "找客服",
        "转客服",
        "人工处理",
        "找负责人",
        "联系负责人",
    )
    return any(phrase in normalized for phrase in phrases)


def decide_query_route(
    plan: QueryPlan,
    user_query: str,
    *,
    normal_threshold: float = 0.80,
    human_threshold: float = 0.50,
) -> QueryRouteDecision:
    """Map a validated plan to one of five safe outbound behaviors.

    The planner's signals are read as independent gates in a fixed order:

    1. ``message_act`` distinguishes a request from a status update or greeting.
       A non-standard request is acknowledged regardless of how certain the
       planner is about its specific business intent. An explicit ``unclear``
       stays silent; older plans that omit the field preserve the old route.
    2. ``intent_confidence`` gates uncertain standard plans and legacy plans.
       Below ``human_threshold`` we stay silent. Between the thresholds we ask a
       human to confirm, after the clarify gates have had their turn.
    3. ``completeness`` decides whether the request can be executed. A
       structured lookup whose entity is not pinned down produces a
       clarification question for the customer, rather than collapsing into
       silence. That gate is deliberately limited to ``structured``: for
       narrative and direct replies there is no entity to be missing.
    4. ``demand_kind`` decides whether clarifying is even allowed. Per the
       2026-09-18 meeting (00:04:37): 澄清属于标准需求, and clarification must
       only fire when a *standard* demand was matched but the customer supplied
       too little information. A non-standard demand gets a soothing
       acknowledgement plus a private reminder to the group owner, and is
       checked before the medium-intent human fallback because "只安抚" is
       unconditional for it. See the inline comment for the one documented
       exception (a contradictory ``structured`` + ``non_standard`` plan).

    **Precedence (fixed; do not reorder casually)**: explicit human request →
    silence for statements/unclear messages → soothe for non-standard requests → silence below
    the intent floor → soothe for legacy non-standard plans → the two clarify
    gates → medium-intent human assist → normal.

    The two clarify gates sit **above** the medium-intent human gate by explicit
    owner decision (2026-09-18): a standard demand that is missing only its
    entity must reach the customer as a clarifying question, instead of being
    swallowed by the "please @ a human" fallback. The measured evidence is quoted
    at the gate itself.

    Reading one fused number instead of these independent gates is the defect
    described in ``架构诊断-越改越不如意的根因.md`` 根因 3: an ambiguous image used to
    drag the whole score down and silence a well-understood demand.
    """
    if not 0 <= human_threshold <= normal_threshold <= 1:
        raise ValueError("routing thresholds must satisfy 0 <= human <= normal <= 1")

    if _is_explicit_human_request(user_query):
        return QueryRouteDecision(
            kind="human",
            reason="explicit_human_request",
            reply_text="请 @ 负责人，由人工客服确认当前问题。",
        )

    if plan.message_act in {"statement", "unclear"}:
        return QueryRouteDecision(kind="silent", reason="no_confirmed_request")

    if (
        plan.message_act == "request"
        and plan.demand_kind == "non_standard"
        and plan.channel != "structured"
    ):
        return QueryRouteDecision(kind="soothe", reason="non_standard_request")

    if plan.intent_confidence < human_threshold:
        return QueryRouteDecision(kind="silent", reason="intent_confidence_low")

    if plan.demand_kind == "non_standard" and plan.channel != "structured":
        # 非标准需求：只安抚 + 私聊提醒负责人，绝不反问。
        #
        # Placed *before* the medium-intent human fallback on purpose: "只安抚"
        # is an unconditional rule for a non-standard demand, and asking the
        # customer to go @ a human is exactly the group noise we are removing.
        # The owner reminder already performs the handoff.
        #
        # ``channel == "structured"`` is excluded because it means the planner
        # did match a standard scope/operation — the two signals then contradict
        # each other, and executing/clarifying a real standard lookup is strictly
        # more useful than replacing it with a non-answer.
        #
        # The wording is deliberately NOT set here: it must be composed
        # per-message by the model (``compose_soothe_reply``) so it reads like a
        # human rather than a fixed template. ``reply_text`` stays empty and the
        # engine fills it in, falling back to NON_STANDARD_SOOTHE_REPLY.
        return QueryRouteDecision(kind="soothe", reason="non_standard_demand")

    # --- Entity/clarify gates, deliberately ABOVE the medium-intent human gate ---
    #
    # 负责人决策（2026-09-18）：这两个澄清分支必须排在"0.50 ≤ intent < 0.80 →
    # human"之前。原因有实测证据：`这两款检测报告麻烦提供下` 的 intent=0.72 /
    # completeness=0.25，`查一下产品` 的 intent=0.55 / completeness=0.20 —— 两者
    # 都是"标准需求、只缺实体"，却在到达澄清闸门前就被转人工截走，客户永远拿不到
    # 那句本该问的澄清。澄清闸门排前面，"标准需求缺信息"才真的会去问客户。
    #
    # 注意：这只调整闸门顺序，不动任何阈值。`silent`（intent < 0.50）仍然最优先，
    # 所以"无法安全判断意图"的输入不会被这两个分支捞回来。
    if plan.channel == "structured" and plan.completeness < normal_threshold:
        return QueryRouteDecision(kind="clarify", reason="entity_incomplete")

    if plan.channel == "clarify":
        # Only a standard demand reaches here: a non-standard one was already
        # diverted to the soothe route above. So clarifying is always the matched
        # standard demand whose information is insufficient — the one case the
        # 2026-09-18 meeting allows clarification for.
        return QueryRouteDecision(kind="clarify", reason="standard_intent_incomplete")

    if plan.intent_confidence < normal_threshold:
        return QueryRouteDecision(
            kind="human",
            reason="intent_confidence_human_assist",
            reply_text=_HUMAN_ASSIST_REPLY,
        )

    return QueryRouteDecision(kind="normal", reason="intent_and_entity_normal")
