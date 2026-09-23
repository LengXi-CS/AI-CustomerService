"""Knowledge-base retrieval robustness.

Two distinct failures were conflated before this file existed:

* the search **could not run** (a 308 redirect, a network error, FastGPT down) —
  we do not know whether the knowledge base has the answer;
* the search **ran and matched nothing** — the knowledge base genuinely lacks it.

Treating the first as the second produced "暂未记录该信息" for a question the
knowledge base actually answers. Measured live on 2026-09-18: of three narrative
questions in one batch, W-12 (`想买瀚思的产品，现在哪些平台有官方店？`) hit
``Redirect response '308 Permanent Redirect'`` and replied "暂未记录该信息",
while the other two answered normally.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from fastgpt_assistant.adapters.fastgpt import client as fastgpt_client
from fastgpt_assistant.adapters.fastgpt.client import FastGptClient
from fastgpt_assistant.core.query_engine import ReadModel
from fastgpt_assistant.core.schema import QueryPlan
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig
from fastgpt_assistant.orchestration.context_buffer import ContextBuffer
from fastgpt_assistant.orchestration.engine import AssistantEngine, InboundEvent

ROOM = "49195424680@chatroom"
CUSTOMER = "customer-a"
HANDOFF_TEXT = "不好意思，这个我这边没查到，已经转给负责人帮您确认了，稍等哈。"


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
SEARCH_OK = {
    "code": 200,
    "data": {"list": [{"id": "c1", "q": "购买渠道", "a": "天猫、京东、抖音", "score": 0.92}]},
}


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry backoff is tuned to a measured 3-6s cooldown; keep tests fast."""
    monkeypatch.setattr(fastgpt_client, "_RETRY_DELAY_SECONDS", 0.0)


def _client_with(handler) -> FastGptClient:
    client = FastGptClient(base_url="http://localhost:3001", api_key="k")
    client._client = httpx.Client(
        base_url="http://localhost:3001",
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    return client


def test_fastgpt_client_does_not_follow_redirects() -> None:
    """Following is actively harmful here: the 308 points back at the request URL.

    实测：`Location` 与请求 URL 完全相同，跟随会一路循环到
    "Exceeded maximum allowed redirects"，比不跟随更糟。
    """
    client = FastGptClient(base_url="http://localhost:3001", api_key="k")

    assert client._client.follow_redirects is False

    client.close()


def test_search_dataset_retries_the_flaky_self_redirect() -> None:
    """实测：同一 POST 会成串自重定向，冷却约 3~6 秒后恢复。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(308, headers={"Location": str(request.url)})
        return httpx.Response(200, json=SEARCH_OK)

    client = _client_with(handler)
    results = client.search_dataset(dataset_id="ds", text="官方店")
    client.close()

    assert len(calls) == 2, "the self-redirect must be retried, not followed"
    assert [r.a for r in results] == ["天猫、京东、抖音"]


def test_retry_backoff_spans_a_measured_cooldown_burst() -> None:
    """实测串行 4 次为 [401, 308, 308, 308]；退避必须跨过整段冷却期。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) <= 2:
            return httpx.Response(308, headers={"Location": str(request.url)})
        return httpx.Response(200, json=SEARCH_OK)

    client = _client_with(handler)
    results = client.search_dataset(dataset_id="ds", text="官方店")
    client.close()

    assert len(calls) == 3
    assert [r.a for r in results] == ["天猫、京东、抖音"]


def test_search_dataset_surfaces_a_persistent_failure() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(308, headers={"Location": str(request.url)})

    client = _client_with(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.search_dataset(dataset_id="ds", text="官方店")
    client.close()

    assert len(calls) == 3, "retries are bounded"


def test_search_dataset_does_not_retry_a_client_error() -> None:
    """A 4xx is a real request problem (e.g. a malformed datasetId), not flakiness."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(400, json={"code": 400, "message": "Data validation error"})

    client = _client_with(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.search_dataset(dataset_id="bad", text="官方店")
    client.close()

    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
class _ModelClient:
    def compose_reply(self, user_query, *, context="", history=None):
        return f"通用回复（基于 {context[:30]}）"

    def compose_insufficient_data_reply(self, user_query, *, history=None):
        return HANDOFF_TEXT


class _FastGpt:
    def __init__(self, *, fail: bool, chunks: list[object] | None = None) -> None:
        self.fail = fail
        self.chunks = chunks or []

    def search_dataset(self, *, dataset_id, text, limit=4):
        if self.fail:
            raise httpx.HTTPStatusError(
                "308", request=httpx.Request("POST", "http://x"), response=httpx.Response(308)
            )
        return self.chunks


class _Chunk:
    q = "购买渠道"
    a = "天猫、京东、抖音、小红书"
    source_name = "官网"


def _read_model(tmp_path) -> ReadModel:
    path = tmp_path / "read-model.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE skus (sku_code TEXT PRIMARY KEY)")
    return ReadModel(path)


def _engine(tmp_path, *, fail: bool, chunks: list[object] | None = None) -> AssistantEngine:
    engine = object.__new__(AssistantEngine)
    engine.settings = SimpleNamespace(
        conversation_quiet_window_seconds=120,
        conversation_poll_interval_seconds=30,
        routing_normal_confidence_threshold=0.80,
        routing_human_confidence_threshold=0.50,
        owner_notify_enabled=True,
        attachment_dispatch_interval_seconds=0.0,
        fastgpt_dataset_id="ds",
    )
    engine.context_buffer = ContextBuffer(media_ttl_seconds=180.0)
    engine.read_model = _read_model(tmp_path)
    engine.model_client = _ModelClient()
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
    engine.fastgpt = _FastGpt(fail=fail, chunks=chunks)
    return engine


def _narrative_model_client(engine: AssistantEngine) -> None:
    class _Client(_ModelClient):
        def plan_query(self, user_query, *, history=None, image_context=""):
            return QueryPlan(
                channel="narrative",
                operation="clarify",
                intent_confidence=0.9,
                completeness=0.9,
            )

    engine.model_client = _Client()


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


def test_search_outage_hands_off_instead_of_claiming_nothing_is_recorded(tmp_path) -> None:
    """A failed search is "we could not check", not "we have no such knowledge"."""
    engine = _engine(tmp_path, fail=True)
    _narrative_model_client(engine)

    reply = engine.process_text_query(_event("想买瀚思的产品，现在哪些平台有官方店？"))

    assert reply.channel_used == "structured_no_evidence"
    assert reply.text == HANDOFF_TEXT
    assert "检索未完成" in reply.owner_notify_text


def test_a_successful_search_still_answers_normally(tmp_path) -> None:
    engine = _engine(tmp_path, fail=False, chunks=[_Chunk()])
    _narrative_model_client(engine)

    reply = engine.process_text_query(_event("想买瀚思的产品，现在哪些平台有官方店？"))

    assert reply.channel_used == "narrative"
    assert "天猫" in reply.text
    assert reply.owner_notify_text == ""


def test_an_empty_but_successful_search_does_not_hand_off(tmp_path) -> None:
    """The knowledge base genuinely lacking an entry must not page the owner."""
    engine = _engine(tmp_path, fail=False, chunks=[])
    _narrative_model_client(engine)

    reply = engine.process_text_query(_event("想问一个知识库里没有的问题"))

    assert reply.channel_used == "narrative"
    assert reply.owner_notify_text == ""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
