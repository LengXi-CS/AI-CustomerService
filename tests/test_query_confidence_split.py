"""Integration tests for the split intent/completeness confidence signals.

The planner used to return one fused ``confidence``. Image ambiguity was folded
into that number, so a well-understood demand ("send me those two test reports")
could fall below the silence floor and produce no reply at all. These tests pin
the new behavior at the engine level: intent owns silence, completeness owns
clarification.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from fastgpt_assistant.core.models import ExecutionResult
from fastgpt_assistant.core.query_engine import ReadModel
from fastgpt_assistant.core.schema import QueryPlan
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.orchestration.context_buffer import ContextBuffer
from fastgpt_assistant.orchestration.engine import (
    AssistantEngine,
    InboundEvent,
    clarify_question,
)
from fastgpt_assistant.orchestration.inbound_media_store import InboundMediaStore

ROOM = "room@chatroom"
CUSTOMER = "customer-a"
AMBIGUOUS_QUESTION = "图片中识别到多个已登记商品，请提供具体 SKU/UPC 后再查询。"
DEFAULT_CLARIFY_QUESTION = "请问您想查询哪个品牌或产品的具体信息？"


def _read_model(tmp_path) -> ReadModel:
    path = tmp_path / "read-model.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE skus (sku_code TEXT PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO skus (sku_code) VALUES (?)",
            [("6976070460164",), ("6976070460171",)],
        )
    return ReadModel(path)


class _ModelClient:
    def __init__(self, plan: QueryPlan) -> None:
        self.plan = plan
        self.image_contexts: list[str] = []

    def plan_query(self, user_query, *, history=None, image_context=""):
        self.image_contexts.append(image_context)
        return self.plan

    def analyze_image(self, image_data, *, mime_type):
        raise AssertionError("a buffered visual_analysis must be reused")

    def compose_reply(self, user_query, *, context="", history=None):
        return "已为您查询。"

    def compose_soothe_reply(self, user_query, *, history=None):
        return "收到，我先记下了。"

    def compose_insufficient_data_reply(self, user_query, *, history=None):
        return "抱歉，这份资料我这边暂时没有查到，已经同步给负责人帮您确认。"


class _Executor:
    def __init__(self) -> None:
        self.calls: list[QueryPlan] = []

    def execute_to_result(self, plan: QueryPlan) -> ExecutionResult:
        self.calls.append(plan)
        return ExecutionResult(status="answered", text="【商品信息】\n· SKU编码：6976070460164")


def _engine(plan: QueryPlan, tmp_path) -> AssistantEngine:
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
    engine.model_client = _ModelClient(plan)
    engine.query_executor = _Executor()
    engine.groups_config = WechatGroupsConfig.from_mapping(
        {
            "global": {"enabled": True},
            "groups": [
                {
                    "group_id": ROOM,
                    "group_name": "测试群",
                    "mode": "active",
                    "owner_id": "wxid_owner",
                }
            ],
        }
    )
    return engine


def _event(text: str, *, visual_sku_code: str = "") -> InboundEvent:
    return InboundEvent(
        message_id="msg-1",
        from_user=ROOM,
        to_user="bot-wxid",
        chat_id=ROOM,
        user_id=CUSTOMER,
        msg_type=1,
        text=text,
        sender_id=CUSTOMER,
        sender_role="customer",
        is_group=True,
        mentioned_bot=True,
        visual_sku_code=visual_sku_code,
    )


def _structured_plan(*, intent_confidence: float, completeness: float) -> QueryPlan:
    return QueryPlan(
        channel="structured",
        operation="file",
        scope="sku",
        select=["third_party_reports"],
        intent_confidence=intent_confidence,
        completeness=completeness,
    )


def _buffer_ambiguous_image(engine: AssistantEngine) -> None:
    # Group messages key their media/history window by chat_id, matching
    # engine.process_image's context_sender rule.
    engine.context_buffer.push_media(
        msg_id="img-1",
        chat_id=ROOM,
        sender_id=ROOM,
        kind="image",
        local_path="",
        media_id="med_img_1",
        image_data=b"\x89PNG\r\n\x1a\n",
        mime_type="image/png",
    )
    engine.context_buffer.set_visual_analysis(
        chat_id=ROOM,
        sender_id=ROOM,
        msg_id="img-1",
        analysis={
            "image_confidence": 0.99,
            "candidates": [
                {"type": "upc", "raw": code, "normalized": code, "confidence": 0.99}
                for code in ("6976070460164", "6976070460171")
            ],
        },
    )


def test_ambiguous_image_asks_the_customer_instead_of_going_silent(tmp_path) -> None:
    engine = _engine(_structured_plan(intent_confidence=0.95, completeness=0.95), tmp_path)
    _buffer_ambiguous_image(engine)

    reply = engine.process_text_query(_event("这个产品的检测报告"))

    assert reply.channel_used == "clarify"
    assert "这个产品的检测报告" in reply.text
    assert AMBIGUOUS_QUESTION in reply.text
    assert reply.suppress_outbound is False
    # The demand survives: intent is intact, only completeness is zeroed.
    assert reply.plan is not None
    assert reply.plan.intent_confidence == 0.95
    assert reply.plan.completeness == 0.0
    # Nothing was answered against a guessed record.
    assert engine.query_executor.calls == []


def test_planner_never_sees_image_confidence_numbers(tmp_path) -> None:
    engine = _engine(_structured_plan(intent_confidence=0.95, completeness=0.95), tmp_path)
    _buffer_ambiguous_image(engine)

    engine.process_text_query(_event("这个产品的检测报告"))

    context = engine.model_client.image_contexts[0]
    assert "6976070460164" in context
    assert "image_confidence" not in context
    assert '"confidence"' not in context
    assert "识别把握" in context


def test_staff_direct_question_does_not_inherit_unquoted_customer_image(tmp_path) -> None:
    engine = _engine(_structured_plan(intent_confidence=0.95, completeness=0.95), tmp_path)
    _buffer_ambiguous_image(engine)

    reply = engine.process_text_query(
        replace(
            _event("查询商品 6976070461130 的信息与关联工厂"),
            sender_id="wxid_owner",
            sender_role="staff",
            conversation_task_id="",
        )
    )

    assert reply.channel_used == "structured"
    assert engine.model_client.image_contexts == [""]


def test_staff_direct_question_can_use_explicitly_quoted_customer_image(tmp_path) -> None:
    engine = _engine(_structured_plan(intent_confidence=0.95, completeness=0.95), tmp_path)
    _buffer_ambiguous_image(engine)

    engine.process_text_query(
        replace(
            _event("这个产品的检测报告"),
            sender_id="wxid_owner",
            sender_role="staff",
            conversation_task_id="",
            quoted_msg_id="img-1",
        )
    )

    assert "6976070460164" in engine.model_client.image_contexts[0]


@pytest.mark.parametrize("completeness", [0.0, 0.2, 0.49, 0.79])
def test_incomplete_structured_lookup_asks_instead_of_answering(
    tmp_path, completeness: float
) -> None:
    engine = _engine(
        _structured_plan(intent_confidence=0.95, completeness=completeness), tmp_path
    )

    reply = engine.process_text_query(_event("帮我查一下检测报告"))

    assert reply.channel_used == "clarify"
    assert "帮我查一下检测报告" in reply.text
    assert DEFAULT_CLARIFY_QUESTION in reply.text
    assert reply.suppress_outbound is False
    assert engine.query_executor.calls == []


def test_low_intent_still_stays_silent_even_with_complete_information(tmp_path) -> None:
    """The two signals act independently: completeness cannot buy a reply."""
    engine = _engine(_structured_plan(intent_confidence=0.30, completeness=0.95), tmp_path)

    reply = engine.process_text_query(_event("帮我查一下检测报告"))

    assert reply.suppress_outbound is True
    assert reply.text == ""
    assert engine.query_executor.calls == []


def test_verified_sku_lifts_completeness_and_executes(tmp_path) -> None:
    engine = _engine(_structured_plan(intent_confidence=0.95, completeness=0.30), tmp_path)

    reply = engine.process_text_query(
        _event("这两款检测报告麻烦提供下", visual_sku_code="6976070460164")
    )

    assert reply.channel_used == "structured"
    assert len(engine.query_executor.calls) == 1
    # A locally verified entity resolves the entity doubt only — not the intent.
    assert reply.plan is not None
    assert reply.plan.intent_confidence == 0.95
    assert reply.plan.completeness == 0.80
    called_plan = engine.query_executor.calls[0]
    assert called_plan.operation == "file"
    assert called_plan.scope == "sku"
    assert [entity.id for entity in called_plan.entities] == ["6976070460164"]
    assert called_plan.select == ["third_party_reports"]


def test_unique_single_image_sku_is_locked_to_report_query(tmp_path) -> None:
    engine = _engine(_structured_plan(intent_confidence=0.95, completeness=0.30), tmp_path)
    engine.context_buffer.push_media(
        msg_id="single-image",
        chat_id=ROOM,
        sender_id=ROOM,
        kind="image",
        local_path="",
        media_id="single-media",
        image_data=b"image",
        mime_type="image/jpeg",
    )
    engine.context_buffer.set_visual_analysis(
        chat_id=ROOM,
        sender_id=ROOM,
        msg_id="single-image",
        analysis={
            "image_confidence": 0.88,
            "candidates": [
                {
                    "type": "upc",
                    "raw": "6976070460164",
                    "normalized": "6976070460164",
                    "confidence": 0.88,
                }
            ],
        },
    )

    reply = engine.process_text_query(_event("这个质检报告发一下"))

    assert reply.channel_used == "structured"
    assert len(engine.query_executor.calls) == 1
    called_plan = engine.query_executor.calls[0]
    assert called_plan.operation == "file"
    assert called_plan.scope == "sku"
    assert [entity.id for entity in called_plan.entities] == ["6976070460164"]
    assert called_plan.select == ["third_party_reports"]


def test_quoted_image_is_downloaded_and_bound_to_the_current_task(tmp_path) -> None:
    engine = _engine(_structured_plan(intent_confidence=0.95, completeness=0.95), tmp_path)
    downloads: list[tuple[str, str]] = []
    engine.wechat = SimpleNamespace(
        download_inbound_media=lambda *, xml, message_id, kind: (
            downloads.append((xml, message_id)) or b"\x89PNG\r\n\x1a\nquoted-image"
        )
    )
    engine.model_client.analyze_image = lambda image_data, *, mime_type: {
        "intent": "product",
        "visible_text": "6976070460164",
        "candidates": [
            {
                "type": "upc",
                "raw": "6976070460164",
                "normalized": "6976070460164",
                "confidence": 0.99,
            }
        ],
        "image_confidence": 0.99,
    }

    reply = engine.process_text_query(
        replace(
            _event("这个产品的检测报告"),
            conversation_task_id="task-quoted-image",
            quoted_media_xml="<img>quoted-payload</img>",
            quoted_msg_id="quoted-message-1",
        )
    )

    assert reply.channel_used == "structured"
    assert downloads == [("<img>quoted-payload</img>", "quoted-message-1")]
    assert "6976070460164" in engine.model_client.image_contexts[0]
    assert engine.context_buffer.get_task_media(
        chat_id=ROOM,
        sender_id=ROOM,
        task_id="task-quoted-image",
    )


def test_group_image_download_strips_sender_prefix_from_callback_xml(tmp_path) -> None:
    engine = _engine(_structured_plan(intent_confidence=0.95, completeness=0.95), tmp_path)
    engine.inbound_media_store = InboundMediaStore(
        tmp_path / "inbound-media",
        tmp_path / "inbound-media.db",
    )
    downloads: list[tuple[str, str]] = []
    engine.wechat = SimpleNamespace(
        download_inbound_media=lambda *, xml, message_id, kind: (
            downloads.append((xml, message_id)) or b"\x89PNG\r\n\x1a\nimage"
        )
    )
    event = replace(
        _event(""),
        message_id="group-image-1",
        msg_type=3,
        is_image=True,
        raw_payload={
            "Data": {
                "Content": {
                    "string": "customer-a:\n<msg><img aeskey=\"opaque\" /></msg>"
                }
            }
        },
    )

    assert engine.process_image(event) is True
    assert downloads == [
        ('<msg><img aeskey="opaque" /></msg>', "group-image-1")
    ]


def test_quoted_image_rehydrates_from_persistent_media_handle(tmp_path) -> None:
    engine = _engine(_structured_plan(intent_confidence=0.95, completeness=0.95), tmp_path)
    engine.inbound_media_store = InboundMediaStore(
        tmp_path / "inbound-media",
        tmp_path / "inbound-media.db",
    )
    engine.inbound_media_store.stage(
        media_id="inm_original-image",
        message_id="original-image-message",
        chat_id=ROOM,
        sender_id=ROOM,
        kind="image",
        mime_type="image/png",
        content=b"\x89PNG\r\n\x1a\noriginal-image",
    )
    engine.model_client.analyze_image = lambda image_data, *, mime_type: {
        "intent": "product",
        "visible_text": "6976070460164",
        "candidates": [
            {
                "type": "upc",
                "raw": "6976070460164",
                "normalized": "6976070460164",
                "confidence": 0.99,
            }
        ],
        "image_confidence": 0.99,
    }

    reply = engine.process_text_query(
        replace(
            _event("这两款检测报告麻烦提供下"),
            conversation_task_id="task-rehydrated-image",
            quoted_msg_id="original-image-message",
        )
    )

    assert reply.channel_used == "structured"
    assert "6976070460164" in engine.model_client.image_contexts[0]
    assert engine.context_buffer.get_task_media(
        chat_id=ROOM,
        sender_id=ROOM,
        task_id="task-rehydrated-image",
    )


def test_clarify_question_tolerates_an_unvalidated_dict_payload() -> None:
    """model_copy(update=...) skips validation, so clarification can be a dict.

    The old fused-score routing always turned an ambiguous image into the
    human-assist reply, so this payload shape was never read and the latent
    mismatch stayed hidden until the clarify route started consuming it.
    """
    plan = QueryPlan(
        channel="clarify",
        operation="clarify",
        intent_confidence=0.9,
        completeness=0.0,
    ).model_copy(update={"clarification": {"reason": "r", "question": "请问是哪一款？"}})

    assert clarify_question(plan) == "请问是哪一款？"
    assert (
        clarify_question(
            QueryPlan(
                channel="direct",
                operation="clarify",
                intent_confidence=0.9,
                completeness=0.9,
            )
        )
        == DEFAULT_CLARIFY_QUESTION
    )
