"""Daily group context, with durable encrypted fallback and bounded rendering."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone

from .demand_association import association_text
from .payload_vault import PayloadVault

QUOTE_CONTEXT_RADIUS = 5
MAX_CONTEXT_CHARS = 60_000
MAX_CONTEXT_MESSAGE_CHARS = 4_000
BEIJING = timezone(timedelta(hours=8))


class GroupContextWindow:
    """Read every message of a Beijing calendar day, isolated by bot and group."""

    def __init__(
        self,
        vault: PayloadVault,
        *,
        bot_id: str,
        group_id: str,
        archive=None,
    ) -> None:
        if not isinstance(vault, PayloadVault):
            raise TypeError("vault must be a PayloadVault")
        self.vault = vault
        self.bot_id = bot_id.strip()
        self.group_id = group_id.strip()
        self.archive = archive
        if not self.bot_id or not self.group_id:
            raise ValueError("bot_id and group_id are required")
        digest = hashlib.sha256(
            f"{self.bot_id}\x1f{self.group_id}".encode()
        ).hexdigest()
        self.session_id = f"group-context-{digest}"
        # Preserve the former rolling window on upgrade. Today's archive fills
        # in older messages already evicted by the previous 60-message limit.
        for entry in self.vault.retrieve_context(self.session_id):
            if isinstance(entry, dict) and entry.get("message_id") and entry.get("received_at"):
                self.append(**{
                    key: value for key, value in entry.items()
                    if key in {"message_id", "text", "received_at", "actor_id",
                               "reply_to_message_id", "quoted_text"}
                })

    @staticmethod
    def _text(value: object, *, maximum: int = MAX_CONTEXT_MESSAGE_CHARS) -> str:
        return value.strip()[:maximum] if isinstance(value, str) else ""

    def _read_entries(self, *, at=None, message_ids=()) -> list[dict[str, str]]:
        message_ids = tuple(dict.fromkeys(
            value.strip() for value in message_ids if isinstance(value, str) and value.strip()
        ))
        moment = self._moment(at)
        midnight = moment.astimezone(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = midnight.astimezone(UTC).isoformat(), moment.isoformat()
        raw_entries = self.vault.retrieve_group_messages(self.session_id, start=start, end=end)
        present = {entry["message_id"] for entry in raw_entries}
        for message_id in message_ids:
            if message_id in present:
                continue
            raw_entries.extend(self.vault.retrieve_group_messages(
                self.session_id, message_id=message_id,
            ))
        if self.archive is not None:
            # Existing archive rows also include staff messages rejected by the
            # bot routing policy and the bot's own replies.
            for row in self.archive.context_entries(
                self.group_id, start=start, end=end, message_ids=tuple(message_ids),
            ):
                raw_entries.append({
                    "message_id": row["message_id"], "actor_id": row["sender_id"],
                    "sender_role": row["sender_role"], "received_at": row["occurred_at"],
                    "text": association_text(row["text"], limit=None, msg_type=row["msg_type"])
                    or f"[{row['kind']}消息]",
                    "reply_to_message_id": row["quoted_msg_id"],
                    "quoted_text": association_text(row["quoted_text"], limit=None,
                                                    msg_type=row["quoted_type"]),
                })
        if not isinstance(raw_entries, list):
            return []
        entries: list[dict[str, str]] = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            message_id = self._text(raw.get("message_id"), maximum=256)
            text = raw.get("text", "").strip()
            received_at = self._text(raw.get("received_at"), maximum=64)
            actor_id = self._text(raw.get("actor_id"), maximum=256) or "unknown"
            if (not message_id or not text or not received_at
                    or self._moment(received_at) > moment):
                continue
            entry = {
                "message_id": message_id,
                "text": text,
                "received_at": received_at,
                "actor_id": actor_id,
                "sender_role": self._text(raw.get("sender_role"), maximum=32),
            }
            reply_to = self._text(raw.get("reply_to_message_id"), maximum=256)
            quoted_text = self._text(association_text(raw.get("quoted_text", "")))
            if reply_to:
                entry["reply_to_message_id"] = reply_to
            if quoted_text:
                entry["quoted_text"] = quoted_text
            entries.append(entry)
        by_id = {entry["message_id"]: entry for entry in entries}
        return sorted(by_id.values(), key=lambda entry: (entry["received_at"], entry["message_id"]))

    @staticmethod
    def _moment(at=None) -> datetime:
        value = datetime.fromisoformat(at) if isinstance(at, str) else at or datetime.now(UTC)
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)

    def append(
        self,
        *,
        message_id: str,
        text: str,
        received_at: str,
        actor_id: str,
        reply_to_message_id: str = "",
        quoted_text: str = "",
        sender_role: str = "",
    ) -> None:
        """Append idempotently; message count does not evict earlier context."""
        message_id = self._text(message_id, maximum=256)
        text = association_text(text, limit=None).strip()
        received_at = self._moment(received_at).isoformat()
        actor_id = self._text(actor_id, maximum=256) or "unknown"
        if not message_id or not text or not received_at:
            return
        entry = {
            "message_id": message_id,
            "text": text,
            "received_at": received_at,
            "actor_id": actor_id,
            "sender_role": sender_role,
        }
        reply_to_message_id = self._text(reply_to_message_id, maximum=256)
        quoted_text = self._text(association_text(quoted_text))
        if reply_to_message_id:
            entry["reply_to_message_id"] = reply_to_message_id
        if quoted_text:
            entry["quoted_text"] = quoted_text
        self.vault.store_group_message(self.session_id, entry)

    def read(self, *, at=None) -> tuple[dict[str, str], ...]:
        """Return the full day up to ``at``; no future messages during replay."""
        return tuple(self._read_entries(at=at))

    def format_for_message(
        self,
        *,
        message_id: str,
        current_text: str = "",
        at=None,
        related_message_ids: tuple[str, ...] = (),
    ) -> str:
        """Format current context plus referenced-message ±5 neighborhood.

        The selection order mirrors LangBot: current message first, then the
        referenced message neighborhood, then newest remaining messages; the
        final output is restored to chronological order.
        """
        entries = self._read_entries(at=at, message_ids=(message_id, *related_message_ids))
        # Pending requests may cross midnight. Pull their explicit quote anchors
        # as well, without loading an unrelated previous day's conversation.
        missing_quotes = {
            entry.get("reply_to_message_id", "") for entry in entries
        } - {entry["message_id"] for entry in entries} - {""}
        if missing_quotes:
            entries = self._read_entries(
                at=at, message_ids=(message_id, *related_message_ids, *missing_quotes),
            )
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
        for related in related_message_ids:
            if related in by_id:
                important.add(by_id[related])
        anchors = set(important)
        anchor_ids = {entries[index]["message_id"] for index in anchors}
        for entry in entries:
            if (entry["message_id"] not in anchor_ids
                    and entry.get("reply_to_message_id", "") not in anchor_ids):
                continue
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
        priority.extend(sorted(index for index in anchors if index != current_index))
        priority.extend(sorted(important - anchors))
        priority.extend(
            index for index in range(len(entries) - 1, -1, -1)
            if entries[index].get("sender_role") == "staff" and index not in important
        )
        priority.extend(
            index
            for index in range(len(entries) - 1, -1, -1)
            if index not in important and entries[index].get("sender_role") != "staff"
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
                f"[message_id={entry['message_id']} actor_id={entry['actor_id']} "
                f"time={entry['received_at']} role={entry.get('sender_role', '')}{marker}]\n"
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
        header = (f"【本群当天上下文及关联原话：共{len(entries)}条，"
                  f"当前载入{len(selected_chunks)}条】")
        return header + "\n" + "\n\n".join(
            selected_chunks[index] for index in sorted(selected_chunks)
        )
