"""Small deterministic splitter for independent customer demands."""

from __future__ import annotations

import re

_STRONG_SPLIT = re.compile(
    r"\s*(?:\r?\n+|[；;]|另外(?:再)?|再帮我查一下|再查一下|还有一个问题)\s*"
)
_CONTINUATION_PREFIXES = (
    "还有",
    "这个",
    "这个产品",
    "它的",
    "该产品",
    "工厂",
    "商标",
    "品类",
    "报告",
    "正面图",
    "背面标签",
)


def _split_message(message: str) -> list[str]:
    return [part.strip() for part in _STRONG_SPLIT.split(message) if part.strip()]


def split_message_demands(message: str) -> list[str]:
    """Split one inbound message into independent demand slots.

    This intentionally does not merge with any other message.  Cross-message
    continuation is a separate, explicit decision; the durable task layer must
    never infer it from chat arrival order.
    """

    return _split_message(message)


def _is_continuation(message: str) -> bool:
    normalized = re.sub(r"\s+", "", message)
    return any(normalized.startswith(prefix) for prefix in _CONTINUATION_PREFIXES)


def split_and_merge_demands(messages: list[str]) -> list[str]:
    """Split strong same-message separators and merge short next-message follow-ups.

    Only the first segment of a new message may continue the previous demand;
    explicit separators inside one message always remain independent demands.
    """
    demands: list[str] = []
    for message in messages:
        parts = _split_message(message)
        if not parts:
            continue
        first = parts[0]
        if demands and _is_continuation(first):
            demands[-1] = f"{demands[-1]}\n{first}"
            parts = parts[1:]
        demands.extend(parts)
    return demands
