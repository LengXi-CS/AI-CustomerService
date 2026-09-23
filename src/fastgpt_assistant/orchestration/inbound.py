"""Small helpers for normalizing GeWe callback scalar and group fields."""

from __future__ import annotations

import html
import re
from typing import Any


def strip_cdata(value: str) -> str:
    """Remove XML CDATA wrappers while preserving enclosed text."""
    return re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", value, flags=re.IGNORECASE | re.DOTALL)


def extract_quote_payload(
    raw_text: str,
) -> tuple[str, str, str, str, str]:
    """Extract clean user text, quoted text, quoted type, quoted media xml, and quoted msg_id.

    Returns:
        (clean_text, quoted_text, quoted_type, quoted_media_xml, quoted_msg_id)
    """
    clean_text = raw_text
    quoted_text = ""
    quoted_type = ""
    quoted_media_xml = ""
    quoted_msg_id = ""

    if "\n- - - - - - - - - - - - - - -\n" in raw_text:
        parts = raw_text.split("\n- - - - - - - - - - - - - - -\n", 1)
        quoted_text = parts[0].strip()
        clean_text = parts[1].strip()
    elif "<refermsg" in raw_text.casefold():
        title_match = re.search(
            r"<title\b[^>]*>(.*?)</title>",
            raw_text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if title_match:
            clean_text = strip_cdata(html.unescape(title_match.group(1))).strip()
        else:
            without_refer = re.sub(
                r"<refermsg\b[^>]*>.*?</refermsg>",
                "",
                raw_text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            clean_text = strip_cdata(re.sub(r"<.*?>", "", without_refer)).strip()

        refer_match = re.search(
            r"<refermsg\b[^>]*>(?P<body>.*?)</refermsg>",
            raw_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if refer_match:
            body = refer_match.group("body")

            def _tag(name: str) -> str:
                m = re.search(
                    rf"<{name}\b[^>]*>(.*?)</{name}>",
                    body,
                    flags=re.DOTALL | re.IGNORECASE,
                )
                return strip_cdata(html.unescape(m.group(1))).strip() if m else ""

            quoted_type = _tag("type")
            quoted_msg_id = _tag("svrid") or _tag("msgid") or _tag("msgidstr")
            content = _tag("content")
            displayname = _tag("displayname")
            quoted_text = (
                f"{displayname}: {content}"
                if displayname and content
                else (content or displayname)
            )
            if re.search(r"<(?:msg|img|voicemsg|filemsg)\b", content, re.IGNORECASE):
                quoted_media_xml = content

    return clean_text, quoted_text, quoted_type, quoted_media_xml, quoted_msg_id


def scalar_text(value: Any) -> str:
    """Read the common GeWe ``{"string": ...}`` envelope safely."""
    if isinstance(value, dict):
        value = value.get("string", "")
    return value.strip() if isinstance(value, str) else ""


def normalize_media_xml(raw_text: Any, *, is_group: bool = False) -> str:
    """Normalize GeWe media XML before calling a download endpoint.

    Group image callbacks prefix the XML with ``sender_wxid:\n``.  LangBot
    removes that prefix before calling GeWe; using the raw callback Content
    directly makes GeWe reject the XML with a parse error.
    """
    text = scalar_text(raw_text)
    if is_group:
        _, text = split_group_sender(text)
    text = strip_cdata(text).strip()
    if "<msg" not in text.casefold():
        decoded = html.unescape(text).strip()
        if "<msg" in decoded.casefold():
            text = decoded
    if not text.startswith("<?xml") and not text.startswith("<msg"):
        start = text.casefold().find("<msg")
        if start >= 0:
            text = text[start:]
    return text


def split_group_sender(raw_content: str) -> tuple[str, str]:
    """Split LangBot/GeWe's ``sender:\nmessage`` group representation."""
    for separator in (":\r\n", ":\n"):
        if separator in raw_content:
            return tuple(raw_content.split(separator, 1))  # type: ignore[return-value]
    return "unknown_member", raw_content


def _strings(value: Any):
    if isinstance(value, str):
        yield value.strip()
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def is_bot_mentioned(
    data: dict[str, Any],
    text: str,
    *,
    bot_id: str,
    aliases: tuple[str, ...] = (),
    generic_staff_fallback: bool = False,
) -> bool:
    """Recognize explicit GeWe mention fields and configured display aliases."""
    identities = tuple(item for item in (bot_id, *aliases) if item.strip())
    mention_values: list[str] = []
    for key in (
        "AtUserList",
        "AtUserNames",
        "MentionedUserList",
        "MentionedUserNames",
        "AtUserName",
        "AtUser",
    ):
        mention_values.extend(_strings(data.get(key)))
    if identities and any(
        value == identity or value.lstrip("@").strip() == identity
        for value in mention_values
        for identity in identities
    ):
        return True

    markers = [text, scalar_text(data.get("PushContent"))]
    for marker in markers:
        if any(f"@{identity}" in marker for identity in identities):
            return True

    # LangBot's historical config did not persist the bot's display nickname.
    # For a configured staff sender, a leading @ is treated as an explicit bot
    # command until a real bot alias is added to the group TOML.
    return generic_staff_fallback and bool(re.match(r"^\s*@[^\s\u2005]+", text))


def strip_group_mention(text: str, *, aliases: tuple[str, ...] = ()) -> str:
    """Remove the leading WeChat mention token before sending text to the planner."""
    cleaned = text.strip()
    for alias in aliases:
        cleaned = re.sub(rf"^\s*@{re.escape(alias)}[\s\u2005]*", "", cleaned)
    return re.sub(r"^\s*@\S+[\s\u2005]*", "", cleaned).strip()


def mention_names(data: dict[str, Any], text: str) -> tuple[str, ...]:
    """Return the names a message @-mentioned, for faithful replay.

    :func:`strip_group_mention` removes the leading ``@昵称`` before the planner
    runs, so the archived body no longer shows who was addressed. GeWe's explicit
    mention fields are read first; only when they are absent does the leading
    display name inside the content stand in for them.

    De-duplicated and order-preserving, so a viewer can render the mention chips
    exactly as the platform reported them.
    """
    names: list[str] = []
    for key in (
        "AtUserNames",
        "MentionedUserNames",
        "AtUserName",
        "AtUserList",
        "MentionedUserList",
    ):
        for value in _strings(data.get(key)):
            candidate = value.lstrip("@").strip()
            if candidate and candidate not in names:
                names.append(candidate)
    if not names:
        match = re.match(r"^\s*@([^\s\u2005]+)", text)
        if match:
            names.append(match.group(1))
    return tuple(names)


_SENDER_NAME_KEYS = (
    "DisplayName",
    "SenderName",
    "SenderNickName",
    "NickName",
    "MemberName",
)


def sender_display_name(data: dict[str, Any]) -> str:
    """Best-effort display name of the sender.

    GeWe documents no nickname field for group messages, and the callbacks this
    deployment has captured never carried one. Returns an empty string rather
    than guessing, so a viewer falls back to the wxid instead of showing a name
    we invented.
    """
    for key in _SENDER_NAME_KEYS:
        value = scalar_text(data.get(key))
        if value:
            return value
    return ""
