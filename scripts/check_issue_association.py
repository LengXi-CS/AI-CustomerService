"""Read-only model check for issue relationships; never connects to WeChat or live state."""

from fastgpt_assistant.adapters.llm.chat_model import ChatModelClient
from fastgpt_assistant.core.config import Settings
from fastgpt_assistant.orchestration.demand_association import validated_links


def main() -> None:
    settings = Settings()
    texts = [
        "87662609290266订单的1702357数量有900，签收面单上没有",
        "请核实是不是漏发了",
        "订单87662609290267请帮忙提供发票",
        "另外，87662609290266订单也需要开票",
    ]
    items = [
        {
            "task_id": f"test-{index}",
            "event_id": f"event-{index}",
            "message_id": f"message-{index}",
            "text": text,
            "quoted_msg_id": "",
            "quoted_text": "",
            "received_at": f"2026-10-13T00:00:{index * 17:02}+00:00",
        }
        for index, text in enumerate(texts)
    ]
    with ChatModelClient(
        api_key=settings.llm_api_key, base_url=settings.llm_base_url, model=settings.llm_model
    ) as client:
        links = validated_links(client.associate_demands(items, []), {i["task_id"] for i in items})
    expected = {"test-0": "new", "test-1": "continue", "test-2": "new", "test-3": "new"}
    for task_id, relation in expected.items():
        proposal = links.get(task_id, {})
        assert proposal.get("relation") == relation, f"Unexpected relationship for {task_id}"
    assert links["test-1"]["target_id"] == "test-0", "Follow-up did not reference the first demand"
    print(
        "PASS: missing-item follow-up is linked; different order and invoicing remain independent."
    )
    print("No live database changes and no WeChat messages sent.")


if __name__ == "__main__":
    main()
