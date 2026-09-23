"""The planner must never take the whole request down with it.

Measured 2026-09-18: a single malformed planner response raised
``json.JSONDecodeError`` out of ``plan_query``, through the FastAPI handler, and
turned ``/api/v1/query`` into an HTTP 500. On the WeChat path that is worse than a
wrong answer — GeWe sees an error and the customer gets nothing at all.

Behavior pinned here: retry once (the failure is intermittent), then raise
``QueryPlanUnavailable`` so ``AssistantEngine`` can degrade to "资料不足 + 转人工".
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from fastgpt_assistant.adapters.llm.chat_model import ChatModelClient, QueryPlanUnavailable
from fastgpt_assistant.core.query_engine import ReadModel
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.orchestration.context_buffer import ContextBuffer
from fastgpt_assistant.orchestration.engine import AssistantEngine, InboundEvent

ROOM = "49195424680@chatroom"
CUSTOMER = "customer-a"
HANDOFF_TEXT = "抱歉，这个我这边刚才没处理过来，已经转给负责人帮您确认了。"

VALID_PLAN = {
    "schema_version": "query-plan-v1",
    "channel": "structured",
    "operation": "record_value",
    "scope": "sku",
    "entities": [{"type": "sku", "id": "6976070460164"}],
    "select": ["product_name"],
    "intent_confidence": 0.9,
    "completeness": 0.9,
    "demand_kind": "standard",
}


def _completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _chat_client(handler) -> ChatModelClient:
    client = ChatModelClient(
        api_key="k",
        base_url="http://llm.invalid",
        model="test-model",
    )
    client._client = httpx.Client(
        base_url="http://llm.invalid", transport=httpx.MockTransport(handler)
    )
    return client


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
def test_malformed_planner_json_is_retried_then_succeeds() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return _completion("这不是 JSON")
        return _completion(json.dumps(VALID_PLAN))

    client = _chat_client(handler)
    plan = client.plan_query("查一下产品")
    client.close()

    assert len(calls) == 2
    assert plan.scope == "sku"


def test_persistently_malformed_planner_json_raises_a_typed_error() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return _completion("依然不是 JSON")

    client = _chat_client(handler)
    with pytest.raises(QueryPlanUnavailable):
        client.plan_query("查一下产品")
    client.close()

    assert len(calls) == 2, "retries are bounded"


def test_a_schema_invalid_plan_also_raises_a_typed_error() -> None:
    """A well-formed JSON object that violates the plan contract is unusable too."""
    payload = dict(VALID_PLAN, channel="not-a-channel")

    def handler(request: httpx.Request) -> httpx.Response:
        return _completion(json.dumps(payload))

    client = _chat_client(handler)
    with pytest.raises(QueryPlanUnavailable):
        client.plan_query("查一下产品")
    client.close()


# --------------------------------------------------------------------------- #
# engine degradation
# --------------------------------------------------------------------------- #
class _UnavailableModelClient:
    def plan_query(self, user_query, *, history=None, image_context=""):
        raise QueryPlanUnavailable("planner produced no usable plan")

    def compose_insufficient_data_reply(self, user_query, *, history=None):
        return HANDOFF_TEXT

    def compose_reply(self, user_query, *, context="", history=None):
        raise AssertionError("must not compose a normal reply")


def _read_model(tmp_path) -> ReadModel:
    path = tmp_path / "read-model.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE skus (sku_code TEXT PRIMARY KEY)")
    return ReadModel(path)


def _engine(tmp_path) -> AssistantEngine:
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
    engine.model_client = _UnavailableModelClient()
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


def _event(text: str) -> InboundEvent:
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
    )


def test_planner_outage_degrades_instead_of_raising(tmp_path) -> None:
    engine = _engine(tmp_path)

    reply = engine.process_text_query(_event("查一下这个产品的检测报告"))

    assert reply.channel_used == "structured_no_evidence"
    assert reply.text == HANDOFF_TEXT
    assert "检索未完成" in reply.owner_notify_text
    assert reply.plan is None
