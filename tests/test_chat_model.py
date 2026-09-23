"""Tests for the provider-neutral text and image model adapter."""

from __future__ import annotations

import json

import httpx
import pytest

from fastgpt_assistant.adapters.llm.chat_model import (
    ChatModelClient,
    MultimodalImage,
    MultimodalTaskInput,
    _unwrap_composer_envelope,
)


def _client_with_transport(handler) -> ChatModelClient:
    client = ChatModelClient(
        api_key="test-key",
        base_url="https://model.example/v1",
        model="text-model",
        vision_model="image-model",
    )
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://model.example/v1",
    )
    return client


def test_analyze_image_sends_openai_compatible_content_parts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        user_content = payload["messages"][1]["content"]
        assert payload["model"] == "image-model"
        assert user_content[0]["type"] == "text"
        assert user_content[1]["type"] == "image_url"
        assert user_content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent": "barcode",
                                    "visible_text": "6976070460164",
                                    "candidates": [
                                        {
                                            "type": "upc",
                                            "raw": "6976070460164",
                                            "normalized": "6976070460164",
                                            "confidence": 0.96,
                                        },
                                        {"type": "ignored", "raw": "x"},
                                    ],
                                    "image_confidence": 1.2,
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = _client_with_transport(handler)
    try:
        result = client.analyze_image(b"png-bytes", mime_type="image/png")
    finally:
        client.close()

    assert result["intent"] == "barcode"
    assert result["image_confidence"] == 1.0
    assert result["candidates"] == [
        {
            "type": "upc",
            "raw": "6976070460164",
            "normalized": "6976070460164",
            "confidence": 0.96,
        }
    ]


@pytest.mark.parametrize("mime_type", ["application/pdf", "image/tiff"])
def test_analyze_image_rejects_unsupported_mime_type(mime_type: str) -> None:
    client = ChatModelClient(api_key="test-key")
    try:
        with pytest.raises(ValueError, match="Unsupported image MIME type"):
            client.analyze_image(b"image", mime_type=mime_type)
    finally:
        client.close()


def _planner_response(plan_json: dict) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {"message": {"content": json.dumps(plan_json)}}
                ]
            },
        )

    return handler


def test_planner_prompt_asks_for_two_independent_signals() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["system"] = json.loads(request.content)["messages"][0]["content"]
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "channel": "structured",
                                    "operation": "file",
                                    "scope": "sku",
                                    "entities": [{"type": "sku", "id": "6976070460041"}],
                                    "select": ["third_party_reports"],
                                    "intent_confidence": 0.93,
                                    "completeness": 0.88,
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = _client_with_transport(handler)
    try:
        plan = client.plan_query("6976070460041 的第三方检测报告")
    finally:
        client.close()

    assert "intent_confidence" in seen["system"]
    assert "completeness" in seen["system"]
    assert plan.intent_confidence == 0.93
    assert plan.completeness == 0.88


def test_planner_still_accepts_a_legacy_single_confidence_response() -> None:
    client = _client_with_transport(
        _planner_response(
            {
                "channel": "structured",
                "operation": "record_value",
                "scope": "sku",
                "entities": [{"type": "sku", "id": "6976070461130"}],
                "select": ["sku_code"],
                "confidence": 0.88,
            }
        )
    )
    try:
        plan = client.plan_query("6976070461130 的商品信息")
    finally:
        client.close()

    assert plan.intent_confidence == 0.88
    assert plan.completeness == 0.88


def test_planner_receives_group_context_as_a_bounded_message() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen["messages"] = payload["messages"]
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "channel": "clarify",
                                    "operation": "clarify",
                                    "intent_confidence": 0.9,
                                    "completeness": 0.2,
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = _client_with_transport(handler)
    try:
        client.plan_query(
            "这两款检测报告麻烦提供下",
            group_context="[message_id=old actor_id=member]\n旧问题\n\n"
            "[message_id=current actor_id=member CURRENT]\n这两款检测报告麻烦提供下",
        )
    finally:
        client.close()

    messages = seen["messages"]
    assert isinstance(messages, list)
    assert "【群级最近上下文，按时间升序】" in messages[-2]["content"]
    assert messages[-1]["content"] == "这两款检测报告麻烦提供下"


def test_analyze_image_rejects_oversized_inline_image() -> None:
    client = ChatModelClient(api_key="test-key")
    try:
        with pytest.raises(ValueError, match="inline image size limit"):
            client.analyze_image(b"x" * (8 * 1024 * 1024 + 1), mime_type="image/jpeg")
    finally:
        client.close()


def test_multimodal_visual_node_receives_complete_task_protocol() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen["model"] = payload["model"]
        seen["content"] = payload["messages"][1]["content"]
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "visual_candidates": [
                                        {
                                            "type": "upc",
                                            "raw": "6976070461215",
                                            "normalized": "6976070461215",
                                            "confidence": 0.42,
                                        }
                                    ],
                                    "target_count": {"min": 3, "max": 3, "exact": True},
                                    "missing_information": ["包装背面批次号"],
                                    "image_confidence": 0.51,
                                }
                            )
                        }
                    }
                ]
            },
        )

    task_input = MultimodalTaskInput(
        task_id="task-visual-1",
        user_query="这三个商品的检测报告",
        quoted_text="引用的原始问题",
        quoted_message_id="quoted-1",
        quoted_type="49",
        quoted_context="[message_id=quoted-1 actor_id=member]\n引用上下文",
        group_context="[message_id=old actor_id=member]\n旧消息\n\n"
        "[message_id=current actor_id=member CURRENT]\n这三个商品的检测报告",
        history=({"role": "user", "content": "历史"},),
        images=(
            MultimodalImage(
                media_id="media-1",
                message_id="msg-1",
                mime_type="image/png",
                image_data=b"png-bytes",
            ),
        ),
    )
    client = _client_with_transport(handler)
    try:
        result = client.analyze_image(
            b"png-bytes",
            mime_type="image/png",
            task_input=task_input,
            media_id="media-1",
        )
    finally:
        client.close()

    assert seen["model"] == "image-model"
    content = seen["content"]
    assert isinstance(content, list)
    protocol_text = content[0]["text"]
    assert "fastgpt-multimodal-task-v1" in protocol_text
    assert "这三个商品的检测报告" in protocol_text
    assert "引用的原始问题" in protocol_text
    assert "引用上下文" in protocol_text
    assert "旧消息" in protocol_text
    assert result["candidates"][0]["confidence"] == 0.42
    assert result["target_count"] == {"min": 3, "max": 3, "exact": True}
    assert result["missing_information"] == ["包装背面批次号"]


def test_multimodal_planner_extracts_query_plan_envelope_and_uses_image_model() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen["model"] = payload["model"]
        seen["content"] = payload["messages"][-1]["content"]
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "visual_candidates": [],
                                    "target_count": {"min": 5, "max": 5, "exact": True},
                                    "missing_information": [],
                                    "query_plan": {
                                        "channel": "structured",
                                        "operation": "file",
                                        "scope": "sku",
                                        "entities": [
                                            {"type": "sku", "id": "6976070460041"}
                                        ],
                                        "select": ["third_party_reports"],
                                        "intent_confidence": 0.91,
                                        "completeness": 0.87,
                                    },
                                }
                            )
                        }
                    }
                ]
            },
        )

    task_input = MultimodalTaskInput(
        task_id="task-plan-1",
        user_query="5个产品的检测报告",
        quoted_text="引用文本",
        quoted_message_id="quoted-plan",
        quoted_context="引用关联消息",
        group_context="最近群上下文",
        images=(
            MultimodalImage(
                media_id="media-plan",
                message_id="msg-plan",
                mime_type="image/jpeg",
                image_data=b"jpeg-bytes",
            ),
        ),
    )
    client = _client_with_transport(handler)
    try:
        plan = client.plan_query(
            "5个产品的检测报告",
            task_input=task_input,
        )
    finally:
        client.close()

    assert seen["model"] == "image-model"
    content = seen["content"]
    assert isinstance(content, list)
    assert "task-plan-1" in content[0]["text"]
    assert "引用关联消息" in content[0]["text"]
    # 解析节点必须继续要求严格 JSON —— 与 composer 节点的纯文本契约刚好相反。
    assert "输出严格 JSON" in content[0]["text"]
    assert plan.select == ["third_party_reports"]
    assert plan.intent_confidence == 0.91
    assert plan.completeness == 0.87


def test_multimodal_visual_node_rejects_malformed_json_for_engine_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "not-json"}}]},
        )

    client = _client_with_transport(handler)
    try:
        with pytest.raises(ValueError):
            client.analyze_image(b"png-bytes", mime_type="image/png")
    finally:
        client.close()


# --- composer nodes: the protocol must ask for plain text, not JSON ----------
#
# 缺陷复现（2026-09-20，部署实例实测 3 题里 2 题泄漏）：composer 的系统提示词要求
# "只输出最终回复正文"，而全模态协议结尾统一追加"输出严格 JSON"，两条指令互相矛盾
# → 模型时而输出裸文本、时而输出 {"reply": "..."}，后者被原样发到客户微信气泡。
# 下面两组测试分别锁住两个修复面：协议契约按节点区分，以及漏网的 JSON 会被拆包。


def _composer_response(content: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": content}}]},
        )

    return handler


def _composer_task_input() -> MultimodalTaskInput:
    return MultimodalTaskInput(
        task_id="task-reply-1",
        user_query="6976070461130 的商品信息和工厂",
        quoted_text="引用的问题",
        quoted_message_id="quoted-reply",
        quoted_context="引用关联消息",
        group_context="最近群上下文",
    )


def test_reply_composer_protocol_asks_for_plain_text_not_json() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen["system"] = payload["messages"][0]["content"]
        seen["protocol"] = payload["messages"][-1]["content"][0]["text"]
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "【商品信息】\n· SKU编码：6976070461130",
                        }
                    }
                ]
            },
        )

    client = _client_with_transport(handler)
    try:
        client.compose_reply(
            "6976070461130 的商品信息",
            context="【商品】6976070461130",
            task_input=_composer_task_input(),
        )
    finally:
        client.close()

    protocol_text = str(seen["protocol"])
    assert "task-reply-1" in protocol_text
    assert "不要输出 JSON" in protocol_text
    assert "输出严格 JSON" not in protocol_text
    # 系统提示词本来就这么要求 —— 现在协议不再和它打架。
    assert "只输出最终回复正文" in str(seen["system"])


def test_reactive_composer_protocol_also_asks_for_plain_text() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["protocol"] = json.loads(request.content)["messages"][-1]["content"][0]["text"]
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "这边先帮您记下了，稍后同步给负责人。"}}]},
        )

    client = _client_with_transport(handler)
    try:
        client.compose_soothe_reply("能不能便宜点", task_input=_composer_task_input())
    finally:
        client.close()

    protocol_text = str(seen["protocol"])
    assert "reactive_reply_composer" in protocol_text
    assert "不要输出 JSON" in protocol_text
    assert "输出严格 JSON" not in protocol_text


def test_reply_composer_unwraps_json_envelope_before_the_customer_sees_it() -> None:
    body = "【商品信息】\n· SKU编码：6976070461130\n· 工厂名称：河南瀚思医疗器械有限公司"
    client = _client_with_transport(
        _composer_response(json.dumps({"reply": body}, ensure_ascii=False))
    )
    try:
        reply = client.compose_reply("6976070461130 的商品信息", context="【商品】6976070461130")
    finally:
        client.close()

    assert reply == body
    assert "{" not in reply


def test_insufficient_data_composer_unwraps_json_envelope() -> None:
    body = "这次暂时没有查到，已经帮您转给负责人确认，您稍等一下。"
    client = _client_with_transport(
        _composer_response(json.dumps({"reply": body}, ensure_ascii=False))
    )
    try:
        reply = client.compose_insufficient_data_reply("6976070461130 的检测报告")
    finally:
        client.close()

    assert reply == body


def test_soothe_composer_unwraps_json_envelope() -> None:
    body = "这边先帮您记下了，我马上同步给负责人跟进，您稍等哈。"
    client = _client_with_transport(
        _composer_response(f"```json\n{json.dumps({'reply': body}, ensure_ascii=False)}\n```")
    )
    try:
        reply = client.compose_soothe_reply("能不能便宜点")
    finally:
        client.close()

    assert reply == body


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 普通回复原样通过：拆包绝不能改动正常文本
        ("【商品信息】\n· SKU编码：6976070461130", "【商品信息】\n· SKU编码：6976070461130"),
        # 本次缺陷的原始形态
        ('{"reply":"您好，这边帮您确认一下"}', "您好，这边帮您确认一下"),
        # 代码围栏 + 别名键
        ('```json\n{"answer": "您好"}\n```', "您好"),
        # 模型改用中文键
        ('{"回复":"您好"}', "您好"),
        # 裸 JSON 字符串
        ('"您好"', "您好"),
        # 未知键，但只有一个字符串值 → 它就是正文
        ('{"response":"您好","confidence":0.9}', "您好"),
        # 多个字符串且无已知键 → 取最长的正文，而不是元数据
        (
            '{"reason":"no_evidence","detail":"这边暂时没有查到，已经帮您转给负责人确认"}',
            "这边暂时没有查到，已经帮您转给负责人确认",
        ),
        # 已知键嵌在容器里
        ('{"data":{"reply":"您好"}}', "您好"),
        # 截断/不可解析的信封：保留原文，避免把真正的回复截断
        ('{"reply":"您好', '{"reply":"您好'),
        # 纯大括号文本但解析失败 → 原样返回
        ("{不是 JSON}", "{不是 JSON}"),
    ],
)
def test_unwrap_composer_envelope(raw: str, expected: str) -> None:
    assert _unwrap_composer_envelope(raw) == expected
