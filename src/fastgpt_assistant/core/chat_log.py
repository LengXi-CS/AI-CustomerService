"""Plaintext local chat log for explicitly configured WeChat groups.

Three stores live side by side and they deliberately disagree:

* ``conversation_state`` keeps orchestration **metadata only** — who, when, which
  task — and never the message body (see its module docstring);
* ``payload_vault`` keeps bodies, but encrypted and short-lived, for restoring
  one pending demand;
* ``chat_log`` (this module) keeps the **conversation itself**: every message of
  a configured group, both directions, in arrival order, and readable.

Keeping the third store separate is deliberate. The first two are correctness
mechanisms with tight invariants (idempotency, safe re-parse); this one is an
archive with opposite properties — append-only, wide rows, unbounded growth, and
no part in deciding a reply. Folding an archive into either would put a
read-heavy, ever-growing table on the message hot path.

Scope is enforced by the caller (``AssistantEngine``), not here: only groups
listed in ``config/wechat_groups.toml`` are written, and a private chat never is.

Storage form is **plaintext**, in a local SQLite file under ``data/`` (gitignored
and never pushed). The owner chose this over reusing the payload-vault AES-GCM
key so the table stays directly queryable and can be exported to a real database
later. The cost — anyone with filesystem access can read customer text — is
accepted knowingly and recorded here on purpose.

The row shape is aimed at a **web frontend that replays a group's conversation**,
not merely at audit. That is why every row carries enough to render one bubble
without a second lookup:

* who — ``sender_id`` plus ``sender_name`` and ``sender_role`` (customer / staff
  / assistant), so a viewer can pick the bubble side and label;
* when — ``occurred_at`` (UTC ISO) for ordering, ``id`` for stable paging;
* what — ``kind`` for the render type, ``text`` for the body, ``msg_type`` for the
  raw platform code, ``media_*`` for an attachment;
* which bubble it answers — ``quoted_msg_id`` / ``quoted_type`` / ``quoted_text``;
* why the bot replied that way — ``channel_used`` and ``task_id``, which join the
  row back to ``conversation_tasks``;
* everything else — ``metadata`` JSON, holding what has no column yet
  (``mentioned_bot``, ``mentions``, ``is_group``, ``quoted_media_xml``). Unknown
  extras belong there so the schema does not churn for each new field.

Writes are best-effort by contract: a full disk or a locked file must never stop
the assistant from answering customers, so :meth:`ChatLogStore.record` logs a
storage error and returns ``False`` instead of raising.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

ChatDirection = Literal["inbound", "outbound"]
ChatSenderRole = Literal["customer", "staff", "assistant"]
ChatMessageKind = Literal["text", "image", "file", "voice", "video", "other"]

# GeWe MsgType → coarse kind. Only the types this deployment actually receives are
# mapped; anything else falls back to text/other rather than inventing a type.
# 49 is WeChat's ``appmsg`` envelope (files, links, mini-programs), which is the
# closest available bucket for an attachment.
_MSGTYPE_KINDS: dict[int, ChatMessageKind] = {
    3: "image",
    34: "voice",
    43: "video",
    49: "file",
}


def message_kind(
    msg_type: int,
    *,
    is_image: bool = False,
    has_text: bool = False,
) -> ChatMessageKind:
    """Classify one inbound GeWe message without trusting its raw ``MsgType``.

    ``is_image`` wins over the raw type because the parser already decided the
    image question from the sender/type pair; the mapping is only for the
    non-image cases.
    """
    if is_image:
        return "image"
    mapped = _MSGTYPE_KINDS.get(msg_type)
    if mapped is not None:
        return mapped
    return "text" if has_text else "other"


def iso_timestamp(value: datetime | str | None = None) -> str:
    """Return a normalized UTC ISO timestamp.

    A local copy of the helper in ``orchestration/conversation_state.py``: this
    module sits in ``core`` and must not import from ``orchestration``, which
    would invert the package layering.
    """
    if value is None:
        timestamp = datetime.now(UTC)
    elif isinstance(value, datetime):
        timestamp = value
    else:
        timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ChatLogEntry:
    """One archived message, already normalized by the caller."""

    chat_id: str
    chat_name: str
    direction: ChatDirection
    sender_id: str
    sender_role: ChatSenderRole
    kind: ChatMessageKind
    text: str = ""
    message_id: str = ""
    sender_name: str = ""
    msg_type: int = 0
    quoted_msg_id: str = ""
    quoted_type: str = ""
    quoted_text: str = ""
    media_ref: str = ""
    media_name: str = ""
    media_mime: str = ""
    media_size: int = 0
    channel_used: str = ""
    task_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime | str | None = None
    # Populated only on rows read back from the store; the autoincrement id is
    # the keyset cursor a frontend passes back as ``after_id`` when paging.
    id: int = 0
    recorded_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready view of one row for a frontend or an export job."""
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "chat_name": self.chat_name,
            "message_id": self.message_id,
            "direction": self.direction,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "sender_role": self.sender_role,
            "kind": self.kind,
            "msg_type": self.msg_type,
            "text": self.text,
            "quoted_msg_id": self.quoted_msg_id,
            "quoted_type": self.quoted_type,
            "quoted_text": self.quoted_text,
            "media_ref": self.media_ref,
            "media_name": self.media_name,
            "media_mime": self.media_mime,
            "media_size": self.media_size,
            "channel_used": self.channel_used,
            "task_id": self.task_id,
            "metadata": dict(self.metadata),
            # Normalized so the payload is JSON-serializable even for an entry
            # built by a caller with a datetime, not just rows read back.
            "occurred_at": iso_timestamp(self.occurred_at),
            "recorded_at": self.recorded_at,
        }


class ChatLogStore:
    """Append-only SQLite archive of configured-group messages."""

    def __init__(self, db_path: str | Path, *, enabled: bool = True) -> None:
        self.db_path = Path(db_path).resolve()
        self.enabled = bool(enabled)
        # The schema is created even when writing is disabled, so an operator can
        # still open the file and see "no rows yet" instead of a missing table.
        self._init_db()

    # ------------------------------------------------------------------ #
    # connection handling
    # ------------------------------------------------------------------ #
    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextlib.contextmanager
    def _connection(self):
        conn = self._get_connection()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT,
                    chat_id TEXT NOT NULL,
                    chat_name TEXT NOT NULL DEFAULT '',
                    message_id TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL DEFAULT '',
                    sender_role TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'text',
                    msg_type INTEGER NOT NULL DEFAULT 0,
                    text TEXT NOT NULL DEFAULT '',
                    quoted_msg_id TEXT NOT NULL DEFAULT '',
                    quoted_type TEXT NOT NULL DEFAULT '',
                    quoted_text TEXT NOT NULL DEFAULT '',
                    media_ref TEXT NOT NULL DEFAULT '',
                    media_name TEXT NOT NULL DEFAULT '',
                    media_mime TEXT NOT NULL DEFAULT '',
                    media_size INTEGER NOT NULL DEFAULT 0,
                    channel_used TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    occurred_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_dedupe
                    ON chat_messages(dedupe_key);
                CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_seq
                    ON chat_messages(chat_id, id);
                CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_time
                    ON chat_messages(chat_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_chat_messages_role_time
                    ON chat_messages(sender_role, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_chat_messages_task
                    ON chat_messages(task_id);
                """
            )

    # ------------------------------------------------------------------ #
    # writes
    # ------------------------------------------------------------------ #
    @staticmethod
    def _dedupe_key(entry: ChatLogEntry) -> str | None:
        """Key making a re-delivered platform message idempotent.

        GeWe retries a callback when we answer too slowly, so the same
        ``NewMsgId`` can arrive repeatedly; without this every retry would add a
        duplicate row. An outbound row carries no platform id, so its key stays
        ``NULL`` — SQLite allows unlimited NULLs in a unique index, which is
        exactly the "always append" behaviour an outbound audit row needs.
        """
        message_id = entry.message_id.strip()
        if not message_id:
            return None
        return f"{entry.direction}\x1f{entry.chat_id.strip()}\x1f{message_id}"

    def record(self, entry: ChatLogEntry) -> int | None:
        """Append one message and return its row id.

        Returns ``None`` when the write was skipped because logging is disabled,
        the message is a duplicate, or the database refused the row. Never
        raises: archiving must not be able to break message handling.
        """
        if not self.enabled:
            return None
        try:
            with self._connection() as conn:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO chat_messages "
                    "(dedupe_key, chat_id, chat_name, message_id, direction, "
                    "sender_id, sender_name, sender_role, kind, msg_type, text, "
                    "quoted_msg_id, quoted_type, quoted_text, media_ref, media_name, "
                    "media_mime, media_size, channel_used, task_id, metadata, "
                    "occurred_at, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._dedupe_key(entry),
                        entry.chat_id.strip(),
                        entry.chat_name.strip(),
                        entry.message_id.strip(),
                        entry.direction,
                        entry.sender_id.strip(),
                        entry.sender_name.strip(),
                        entry.sender_role,
                        entry.kind,
                        int(entry.msg_type),
                        entry.text,
                        entry.quoted_msg_id,
                        entry.quoted_type,
                        entry.quoted_text,
                        entry.media_ref,
                        entry.media_name,
                        entry.media_mime,
                        int(entry.media_size),
                        entry.channel_used,
                        entry.task_id,
                        json.dumps(entry.metadata, ensure_ascii=False, sort_keys=True),
                        iso_timestamp(entry.occurred_at),
                        iso_timestamp(),
                    ),
                )
                inserted = cursor.rowcount > 0
                row_id = int(cursor.lastrowid) if inserted else None
        except (sqlite3.Error, ValueError, TypeError):
            logger.exception(
                "Unable to append chat log chat=%s message=%s",
                entry.chat_id,
                entry.message_id or "<outbound>",
            )
            return None
        return row_id

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #
    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> ChatLogEntry:
        raw_metadata = str(row["metadata"] or "{}")
        try:
            metadata = json.loads(raw_metadata)
        except json.JSONDecodeError:
            metadata = {"_unparsable": raw_metadata}
        if not isinstance(metadata, dict):
            metadata = {"_malformed": metadata}
        return ChatLogEntry(
            id=int(row["id"]),
            chat_id=str(row["chat_id"]),
            chat_name=str(row["chat_name"]),
            direction=str(row["direction"]),  # type: ignore[arg-type]
            sender_id=str(row["sender_id"]),
            sender_name=str(row["sender_name"]),
            sender_role=str(row["sender_role"]),  # type: ignore[arg-type]
            kind=str(row["kind"]),  # type: ignore[arg-type]
            msg_type=int(row["msg_type"]),
            text=str(row["text"]),
            message_id=str(row["message_id"]),
            quoted_msg_id=str(row["quoted_msg_id"]),
            quoted_type=str(row["quoted_type"]),
            quoted_text=str(row["quoted_text"]),
            media_ref=str(row["media_ref"]),
            media_name=str(row["media_name"]),
            media_mime=str(row["media_mime"]),
            media_size=int(row["media_size"]),
            channel_used=str(row["channel_used"]),
            task_id=str(row["task_id"]),
            metadata=metadata,
            occurred_at=str(row["occurred_at"]),
            recorded_at=str(row["recorded_at"]),
        )

    def count(self, chat_id: str | None = None) -> int:
        """Number of archived rows, for one chat or all of them."""
        with self._connection() as conn:
            if chat_id:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM chat_messages WHERE chat_id = ?",
                    (chat_id,),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS count FROM chat_messages").fetchone()
        return int(row["count"])

    def recent(
        self,
        *,
        chat_id: str | None = None,
        limit: int = 50,
    ) -> list[ChatLogEntry]:
        """Return the newest ``limit`` messages in chronological order."""
        if limit <= 0:
            return []
        with self._connection() as conn:
            if chat_id:
                rows = conn.execute(
                    "SELECT * FROM chat_messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                    (chat_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._entry_from_row(row) for row in reversed(rows)]

    def list_messages(
        self,
        chat_id: str,
        *,
        after_id: int = 0,
        limit: int = 200,
    ) -> list[ChatLogEntry]:
        """Read one chat forward from ``after_id`` for incremental replay.

        Keyset paging on the autoincrement id rather than ``LIMIT/OFFSET``: a
        viewer that polls for new messages can pass the last id it rendered and
        never re-sees or skips a row, even while a busy group keeps appending.
        """
        if limit <= 0:
            return []
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE chat_id = ? AND id > ? "
                "ORDER BY id ASC LIMIT ?",
                (chat_id, max(0, int(after_id)), limit),
            ).fetchall()
        return [self._entry_from_row(row) for row in rows]

    def summarize(self) -> list[dict[str, Any]]:
        """Per-chat row counts and time span, for the read-only viewer script."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT chat_id, chat_name, COUNT(*) AS total, "
                "MIN(occurred_at) AS first_at, MAX(occurred_at) AS last_at "
                "FROM chat_messages GROUP BY chat_id, chat_name ORDER BY last_at DESC"
            ).fetchall()
        return [
            {
                "chat_id": str(row["chat_id"]),
                "chat_name": str(row["chat_name"]),
                "total": int(row["total"]),
                "first_at": str(row["first_at"]),
                "last_at": str(row["last_at"]),
            }
            for row in rows
        ]
