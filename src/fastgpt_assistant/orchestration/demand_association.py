"""Validate semantic associations without using arrival time as proof of identity."""

import math
import re
import xml.etree.ElementTree as ET

ASSOCIATION_WINDOW_SECONDS = 120

_ORDERS = re.compile(
    r"(?:订单号|单号|订单)\s*[:：]?\s*([A-Za-z0-9-]{8,})|([0-9]{10,})\s*(?:订单|单)"
)


def order_ids(text: str) -> set[str]:
    return {left or right for left, right in _ORDERS.findall(text)}


def association_text(text: str, *, limit: int | None = 1800) -> str:
    """Give the model attachment descriptions, never transport XML/keys."""
    if text.lstrip().startswith("<"):
        try:
            root = ET.fromstring(text)
            title = root.findtext("appmsg/title")
            return f"[附件：{title[:200]}]" if title else "[附件消息]"
        except ET.ParseError:
            return "[附件消息]"
    return text[:limit] if limit is not None else text


def validated_links(value: object, task_ids: set[str]) -> dict[str, dict]:
    if not isinstance(value, list):
        return {}
    result = {}
    duplicates = set()
    seen = set()
    for item in value:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("task_id"), str)
            or item["task_id"] not in task_ids
        ):
            continue
        task_id = item["task_id"]
        if task_id in seen:
            duplicates.add(task_id)
        seen.add(task_id)
        confidence = item.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0.9 <= confidence <= 1
            or not isinstance(item.get("relation"), str)
            or item["relation"] not in {"new", "continue", "ack", "resolved", "renew"}
            or not isinstance(item.get("target_id", ""), str)
        ):
            continue
        result[task_id] = item
    return {key: item for key, item in result.items() if key not in duplicates}
