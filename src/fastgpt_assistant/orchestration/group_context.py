"""LangBot-compatible encrypted rolling context for one WeChat group."""

from __future__ import annotations

import hashlib

from .payload_vault import PayloadVault

MAX_GROUP_CONTEXT_MESSAGES = 60
QUOTE_CONTEXT_RADIUS = 5
MAX_CONTEXT_CHARS = 60_000
MAX_CONTEXT_MESSAGE_CHARS = 4_000


class GroupContextWindow:
    """Store the newest 60 group messages and render quote neighborhoods."""

    def __init__(
        self,
        vault: PayloadVault,
        *,
        bot_id: str,
        group_id: str,
        max_messages: int = MAX_GROUP_CONTEXT_MESSAGES,
    ) -> None:
        if not isinstance(vault, PayloadVault):
            raise TypeError("vault must be a PayloadVault")
        if not 1 <= max_messages <= MAX_GROUP_CONTEXT_MESSAGES:
            raise ValueError("max_messages must be between 1 and 60")
        self.vault = vault
        self.bot_id = bot_id.strip()
        self.group_id = group_id.strip()
        self.max_messages = max_messages
        if not self.bot_id or not self.group_id:
            raise ValueError("bot_id and group_id are required")
        digest = hashlib.sha256(
            f"{self.bot_id}\x1f{self.group_id}".encode()
        ).hexdigest()
        self.session_id = f"group-context-{digest}"

    @staticmethod
    def _text(value: object, *, maximum: int = MAX_CONTEXT_MESSAGE_CHARS) -> str:
        return value.strip()[:maximum] if isinstance(value, str) else ""

    def _read_entries(self) -> list[dict[str, str]]:
        raw_entries = self.vault.retrieve_context(self.session_id)
        if not isinstance(raw_entries, list):
            return []
        entries: list[dict[str, str]] = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            message_id = self._text(raw.get("message_id"), maximum=256)
            text = self._text(raw.get("text"))
            received_at = self._text(raw.get("received_at"), maximum=64)
            actor_id = self._text(raw.get("actor_id"), maximum=256) or "unknown"
            if not message_id or not text or not received_at:
                continue
            entry = {
                "message_id": message_id,
                "text": text,
                "received_at": received_at,
                "actor_id": actor_id,
            }
            reply_to = self._text(raw.get("reply_to_message_id"), maximum=256)
            quoted_text = self._text(raw.get("quoted_text"))
            if reply_to:
                entry["reply_to_message_id"] = reply_to
            if quoted_text:
                entry["quoted_text"] = quoted_text
            entries.append(entry)
        return entries[-self.max_messages :]

    def append(
        self,
        *,
        message_id: str,
        text: str,
        received_at: str,
        actor_id: str,
        reply_to_message_id: str = "",
        quoted_text: str = "",
    ) -> None:
        """Append one idempotent message and retain only the newest 60."""
        message_id = self._text(message_id, maximum=256)
        text = self._text(text)
        received_at = self._text(received_at, maximum=64)
        actor_id = self._text(actor_id, maximum=256) or "unknown"
        if not message_id or not text or not received_at:
            return
        entries = self._read_entries()
        if any(entry["message_id"] == message_id for entry in entries):
            return
        entry = {
            "message_id": message_id,
            "text": text,
            "received_at": received_at,
            "actor_id": actor_id,
        }
        reply_to_message_id = self._text(reply_to_message_id, maximum=256)
        quoted_text = self._text(quoted_text)
        if reply_to_message_id:
            entry["reply_to_message_id"] = reply_to_message_id
        if quoted_text:
            entry["quoted_text"] = quoted_text
        entries.append(entry)
        entries = entries[-self.max_messages :]
        self.vault.store_context(
            self.session_id,
            entries,
        )

    def read(self) -> tuple[dict[str, str], ...]:
        """Return the bounded window in chronological order."""
        return tuple(self._read_entries())

    def format_for_message(
        self,
        *,
        message_id: str,
        current_text: str = "",
    ) -> str:
        """Format current context plus referenced-message ±5 neighborhood.

        The selection order mirrors LangBot: current message first, then the
        referenced message neighborhood, then newest remaining messages; the
        final output is restored to chronological order.
        """
        entries = list(self.read())
        by_id = {entry["message_id"]: index for index, entry in enumerate(entries)}
        current_index = by_id.get(message_id)
        if current_index is None and current_text.strip():
            entries.append(
                {
                    "message_id": message_id,
                    "text": self._text(current_text),
                    "received_at": "",
                    "actor_id": "current",
                }
            )
            current_index = len(entries) - 1
            by_id[message_id] = current_index

        important: set[int] = set()
        if current_index is not None:
            important.add(current_index)
        for entry in entries:
            quoted_index = by_id.get(entry.get("reply_to_message_id", ""))
            if quoted_index is not None:
                important.update(
                    range(
                        max(0, quoted_index - QUOTE_CONTEXT_RADIUS),
                        min(len(entries), quoted_index + QUOTE_CONTEXT_RADIUS + 1),
                    )
                )

        priority: list[int] = []
        if current_index is not None:
            priority.append(current_index)
        priority.extend(sorted(index for index in important if index != current_index))
        priority.extend(
            index
            for index in range(len(entries) - 1, -1, -1)
            if index not in important
        )

        selected_chunks: dict[int, str] = {}
        total = 0
        for index in priority:
            entry = entries[index]
            marker = " CURRENT" if index == current_index else ""
            quote = ""
            reply_to = entry.get("reply_to_message_id", "")
            if reply_to:
                quote = f"\n引用消息ID={reply_to}"
                quoted_text = entry.get("quoted_text", "")
                if quoted_text:
                    quote += f"\n引用摘要={quoted_text[:MAX_CONTEXT_MESSAGE_CHARS]}"
            chunk = (
                f"[message_id={entry['message_id']} actor_id={entry['actor_id']}{marker}]\n"
                f"{entry['text'][:MAX_CONTEXT_MESSAGE_CHARS]}{quote}"
            )
            if total + len(chunk) > MAX_CONTEXT_CHARS:
                if index == current_index:
                    chunk = chunk[: max(0, MAX_CONTEXT_CHARS - total)]
                else:
                    continue
            selected_chunks[index] = chunk
            total += len(chunk) + 1
            if total >= MAX_CONTEXT_CHARS:
                break
        return "\n\n".join(selected_chunks[index] for index in sorted(selected_chunks))
