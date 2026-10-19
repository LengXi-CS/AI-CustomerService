import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest

from fastgpt_assistant.orchestration.conversation_state import EventReceipt
from fastgpt_assistant.orchestration.engine import GroupEventRoute, InboundEvent
from fastgpt_assistant.server import app as app_module


@pytest.mark.asyncio
async def test_callbacks_keep_customer_order_and_share_stable_arrival_time(monkeypatch):
    split_started = threading.Event()
    release_split = threading.Event()
    second_arrived = asyncio.Event()
    second_split_finished = threading.Event()
    splits, recorded, arrivals = [], [], {}

    def parse(body):
        return InboundEvent(message_id=body["id"], text=body["id"], from_user="room@chatroom",
                            to_user="bot", chat_id="room@chatroom", user_id="customer",
                            sender_id="customer", is_group=True, msg_type=1)

    def context(event):
        arrivals[event.message_id] = event.raw_payload["received_at"]
        if event.message_id == "second":
            second_arrived.set()

    def split(text):
        splits.append(text)
        if text == "first":
            split_started.set()
            assert release_split.wait(5)
        else:
            second_split_finished.set()
        return [text]

    def record(event, **kwargs):
        assert event.raw_payload["received_at"] == arrivals[event.message_id]
        recorded.append(event.message_id)
        return EventReceipt(event.message_id, event.message_id, True, False, (event.message_id,))

    engine = SimpleNamespace(
        observe_public_origin=lambda **kwargs: None,
        query_executor=SimpleNamespace(media_base_url=""),
        parse_gewe_callback=parse, log_group_inbound=lambda event: None,
        groups_config=SimpleNamespace(get=lambda chat_id: object()),
        record_group_context_event=context,
        route_group_event=lambda event: GroupEventRoute(allowed=True), split_customer_demands=split,
        prepare_customer_event=lambda event: EventReceipt(event.message_id, None, True, False),
        complete_customer_event=record,
        release_customer_event=lambda event_id: None,
    )
    monkeypatch.setattr(app_module, "get_engine", lambda: engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app),
                                  base_url="http://testserver") as client:
        first = asyncio.create_task(client.post("/api/wechat/callback", json={"id": "first"}))
        assert await asyncio.to_thread(split_started.wait, 5)
        second = asyncio.create_task(client.post("/api/wechat/callback", json={"id": "second"}))
        try:
            await asyncio.wait_for(second_arrived.wait(), 5)
            assert await asyncio.to_thread(second_split_finished.wait, 5)
            assert splits == ["first", "second"] and recorded == []
        finally:
            release_split.set()
        responses = await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert all(response.json()["msg"] == "queued" for response in responses)
    assert recorded == ["first", "second"]
    assert app_module.app.state.customer_event_order == {}


@pytest.mark.asyncio
async def test_different_customers_do_not_block_each_other_and_cancelled_waiter_is_removed():
    async with app_module._customer_event_order("room", "a"):
        waiter = asyncio.create_task(_enter_lock("room", "a"))
        await asyncio.wait_for(_enter_lock("room", "b"), 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    await asyncio.wait_for(_enter_lock("room", "a"), 1)
    assert app_module.app.state.customer_event_order == {}


async def _enter_lock(room, customer):
    async with app_module._customer_event_order(room, customer) as previous:
        if previous is not None:
            await asyncio.shield(previous)
        return True
