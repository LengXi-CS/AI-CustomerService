"""Tests for explicit multi-product visual query fan-out."""

from __future__ import annotations

from fastgpt_assistant.orchestration.context_buffer import ContextBuffer
from fastgpt_assistant.orchestration.engine import (
    AssistantEngine,
    InboundEvent,
    ReplyDeliverable,
)


def test_visual_query_bundle_fans_out_and_records_one_merged_turn(monkeypatch) -> None:
    engine = object.__new__(AssistantEngine)
    engine.context_buffer = ContextBuffer()
    calls: list[tuple[str, bool]] = []

    def fake_process(event: InboundEvent, *, record_history: bool = True) -> ReplyDeliverable:
        calls.append((event.visual_sku_code, record_history))
        return ReplyDeliverable(text=f"工厂查询结果 {event.visual_sku_code}")

    monkeypatch.setattr(engine, "process_text_query", fake_process)
    event = InboundEvent(
        message_id="multi-1",
        from_user="user-1",
        to_user="assistant",
        chat_id="user-1",
        user_id="user-1",
        msg_type=1,
        text="请分别查询这两个产品的工厂",
        sender_id="user-1",
    )

    reply = engine.process_visual_query_bundle(
        event,
        ("6976070460164", "6976070460171"),
    )

    assert calls == [("6976070460164", False), ("6976070460171", False)]
    assert "【SKU编码：6976070460164】" in reply.text
    assert "【SKU编码：6976070460171】" in reply.text
    history = engine.context_buffer.get_history("user-1", "user-1", limit=4)
    assert len(history) == 2
    assert history[0]["content"] == event.text
    assert history[1]["content"] == reply.text
