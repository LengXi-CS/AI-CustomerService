"""Scratch probe: confirm composer nodes no longer leak a JSON envelope.

Talks to the configured LLM directly — no HTTP server and no task poller, so this
cannot send anything to a customer. Delete after use.
"""

from __future__ import annotations

from pathlib import Path

from fastgpt_assistant.adapters.llm.chat_model import (
    ChatModelClient,
    MultimodalTaskInput,
    _task_protocol_text,
)
from fastgpt_assistant.core.config import Settings

ROUNDS = 3

FACT_CONTEXT = (
    "【商品信息】\n"
    "· SKU编码：6976070461130\n"
    "· 商品名称：医用外科口罩\n"
    "【关联工厂】\n"
    "· 工厂名称：河南瀚思医疗器械有限公司"
)


def main() -> None:
    settings = Settings()
    client = ChatModelClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        vision_model=settings.llm_vision_model or None,
    )
    lines: list[str] = []
    leaks = 0
    attempts = 0

    task_input = MultimodalTaskInput(
        task_id="probe-json-fix",
        user_query="查询商品 6976070461130 的信息与关联工厂",
        group_context=(
            "[message_id=current actor_id=member CURRENT]\n"
            "查询商品 6976070461130 的信息与关联工厂"
        ),
    )
    protocol = _task_protocol_text(
        task_input, node="reply_composer", request="probe", as_json=False
    )
    lines.append("=== 协议要求行（reply_composer）===")
    lines.append(protocol.splitlines()[-1])
    lines.append("")

    try:
        lines.append(f"=== compose_reply × {ROUNDS} ===")
        for index in range(ROUNDS):
            reply = client.compose_reply(
                "查询商品 6976070461130 的信息与关联工厂",
                context=FACT_CONTEXT,
                task_input=task_input,
            )
            attempts += 1
            leaks += int(reply.strip().startswith("{"))
            lines.append(f"[{index + 1}] {reply.strip().replace(chr(10), ' / ')[:200]}")
        lines.append("")

        lines.append(f"=== compose_soothe_reply × {ROUNDS} ===")
        for index in range(ROUNDS):
            reply = client.compose_soothe_reply(
                "这批货能不能再便宜点，走的量很大", task_input=task_input
            )
            attempts += 1
            leaks += int(reply.strip().startswith("{"))
            lines.append(f"[{index + 1}] {reply.strip().replace(chr(10), ' / ')[:200]}")
        lines.append("")

        lines.append(f"=== compose_insufficient_data_reply × {ROUNDS} ===")
        for index in range(ROUNDS):
            reply = client.compose_insufficient_data_reply(
                "6976070469999 的第三方检测报告", task_input=task_input
            )
            attempts += 1
            leaks += int(reply.strip().startswith("{"))
            lines.append(f"[{index + 1}] {reply.strip().replace(chr(10), ' / ')[:200]}")
        lines.append("")
    finally:
        client.close()

    lines.append(f"=== JSON 信封泄漏: {leaks}/{attempts} ===")
    Path("_verify_reply.txt").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
