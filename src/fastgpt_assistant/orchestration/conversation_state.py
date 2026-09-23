"""Durable, non-sensitive state for WeChat conversation orchestration.

The state database deliberately stores metadata only. Message text remains in
the encrypted payload vault and is never written to this database.
"""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

ConversationState = Literal[
    "waiting_quiet",
    "human_active",
    "processing",
    "responded",
    "closed",
    "blocked",
]


@dataclass(frozen=True, slots=True)
class ConversationTask:
    """A conversation-level task containing metadata, never raw message text."""

    task_id: str
    chat_id: str
    customer_id: str
    state: ConversationState
    latest_customer_event_id: str | None
    latest_customer_at: str | None
    last_staff_at: str | None
    human_owner_id: str | None
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class EventReceipt:
    """Result of recording an external event idempotently."""

    event_id: str
    task_id: str | None
    accepted: bool
    duplicate: bool
    task_ids: tuple[str, ...] = ()


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_timestamp(value: datetime | str | None = None) -> str:
    """Return a normalized UTC ISO timestamp for state comparisons."""
    if value is None:
        timestamp = utc_now()
    elif isinstance(value, datetime):
        timestamp = value
    else:
        timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).isoformat()


def content_digest(text: str) -> tuple[str, int]:
    """Return a digest and character length without retaining the text."""
    encoded = text.encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(text)


class ConversationStateStore:
    """SQLite store for conversation metadata and external-event idempotency."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

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
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_tasks (
                    task_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    latest_customer_event_id TEXT,
                    latest_customer_at TEXT,
                    last_staff_at TEXT,
                    human_owner_id TEXT,
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_tasks_chat_state
                    ON conversation_tasks(chat_id, state, updated_at);

                CREATE TABLE IF NOT EXISTS conversation_events (
                    event_id TEXT PRIMARY KEY,
                    external_event_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_role TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    mentioned_bot INTEGER NOT NULL DEFAULT 0,
                    content_sha256 TEXT NOT NULL,
                    content_length INTEGER NOT NULL,
                    received_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(chat_id, external_event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_events_chat_time
                    ON conversation_events(chat_id, received_at);

                CREATE TABLE IF NOT EXISTS conversation_task_events (
                    task_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    demand_index INTEGER NOT NULL DEFAULT 0,
                    linked_at TEXT NOT NULL,
                    PRIMARY KEY(event_id, demand_index),
                    UNIQUE(task_id, event_id, demand_index),
                    FOREIGN KEY(task_id) REFERENCES conversation_tasks(task_id),
                    FOREIGN KEY(event_id) REFERENCES conversation_events(event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_task_events_task
                    ON conversation_task_events(task_id, linked_at);
                """
            )

    @staticmethod
    def _task_from_row(row: sqlite3.Row | None) -> ConversationTask | None:
        if row is None:
            return None
        return ConversationTask(
            task_id=row["task_id"],
            chat_id=row["chat_id"],
            customer_id=row["customer_id"],
            state=row["state"],
            latest_customer_event_id=row["latest_customer_event_id"],
            latest_customer_at=row["latest_customer_at"],
            last_staff_at=row["last_staff_at"],
            human_owner_id=row["human_owner_id"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_task(self, task_id: str) -> ConversationTask | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._task_from_row(row)

    def get_open_task(self, chat_id: str) -> ConversationTask | None:
        """Return the newest task which can still receive conversation events."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_tasks "
                "WHERE chat_id = ? AND state IN ('waiting_quiet', 'human_active', 'processing') "
                "ORDER BY updated_at DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        return self._task_from_row(row)

    def record_customer_message(
        self,
        *,
        chat_id: str,
        customer_id: str,
        external_event_id: str,
        received_at: datetime | str,
        text: str,
        demand_count: int = 1,
    ) -> EventReceipt:
        """Record one customer event as one or more independent demand tasks.

        The operation is idempotent on ``(chat_id, external_event_id)``. A
        duplicate event never advances the quiet window a second time.  The
        caller supplies the number of demand slots after inspecting the text;
        message bodies never enter this store.
        """
        if isinstance(demand_count, bool) or not isinstance(demand_count, int):
            raise TypeError("demand_count must be an integer")
        if not 1 <= demand_count <= 16:
            raise ValueError("demand_count must be between 1 and 16")
        received_iso = iso_timestamp(received_at)
        created_iso = iso_timestamp()
        digest, length = content_digest(text)
        event_id = str(uuid.uuid4())

        with self._connection() as conn:
            existing = conn.execute(
                "SELECT event_id FROM conversation_events "
                "WHERE chat_id = ? AND external_event_id = ?",
                (chat_id, external_event_id),
            ).fetchone()
            if existing:
                linked = conn.execute(
                    "SELECT task_id FROM conversation_task_events "
                    "WHERE event_id = ? ORDER BY demand_index",
                    (existing["event_id"],),
                ).fetchone()
                linked_ids = tuple(
                    row["task_id"]
                    for row in conn.execute(
                        "SELECT task_id FROM conversation_task_events "
                        "WHERE event_id = ? ORDER BY demand_index",
                        (existing["event_id"],),
                    ).fetchall()
                )
                return EventReceipt(
                    event_id=existing["event_id"],
                    task_id=linked["task_id"] if linked else None,
                    accepted=False,
                    duplicate=True,
                    task_ids=linked_ids,
                )

            conn.execute(
                "INSERT INTO conversation_events "
                "(event_id, external_event_id, chat_id, sender_id, sender_role, "
                "event_kind, mentioned_bot, content_sha256, content_length, "
                "received_at, created_at) "
                "VALUES (?, ?, ?, ?, 'customer', 'message', 0, ?, ?, ?, ?)",
                (
                    event_id,
                    external_event_id,
                    chat_id,
                    customer_id,
                    digest,
                    length,
                    received_iso,
                    created_iso,
                ),
            )
            task_ids: list[str] = []
            for demand_index in range(demand_count):
                task_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO conversation_tasks "
                    "(task_id, chat_id, customer_id, state, latest_customer_event_id, "
                    "latest_customer_at, last_staff_at, human_owner_id, version, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, 'waiting_quiet', ?, ?, NULL, NULL, 0, ?, ?)",
                    (
                        task_id,
                        chat_id,
                        customer_id,
                        event_id,
                        received_iso,
                        created_iso,
                        created_iso,
                    ),
                )
                conn.execute(
                    "INSERT INTO conversation_task_events "
                    "(task_id, event_id, demand_index, linked_at) VALUES (?, ?, ?, ?)",
                    (task_id, event_id, demand_index, created_iso),
                )
                task_ids.append(task_id)

        return EventReceipt(
            event_id=event_id,
            task_id=task_ids[0],
            accepted=True,
            duplicate=False,
            task_ids=tuple(task_ids),
        )

    def record_staff_message(
        self,
        *,
        chat_id: str,
        staff_id: str,
        external_event_id: str,
        received_at: datetime | str,
        text: str,
        mentioned_bot: bool,
    ) -> EventReceipt:
        """Record staff metadata and update the open task's suppression marker."""
        received_iso = iso_timestamp(received_at)
        created_iso = iso_timestamp()
        digest, length = content_digest(text)
        event_id = str(uuid.uuid4())

        with self._connection() as conn:
            existing = conn.execute(
                "SELECT event_id FROM conversation_events "
                "WHERE chat_id = ? AND external_event_id = ?",
                (chat_id, external_event_id),
            ).fetchone()
            if existing:
                return EventReceipt(
                    event_id=existing["event_id"],
                    task_id=None,
                    accepted=False,
                    duplicate=True,
                )

            # A staff message may affect a task only when the target is
            # unambiguous.  An @-mention prefers the single human-active task;
            # otherwise a single open task is enough.  With multiple tasks we
            # deliberately do nothing instead of moving the newest task by
            # arrival order.
            open_tasks = conn.execute(
                "SELECT task_id, state FROM conversation_tasks "
                "WHERE chat_id = ? AND state IN "
                "('waiting_quiet', 'human_active', 'processing') "
                "ORDER BY updated_at DESC, task_id DESC",
                (chat_id,),
            ).fetchall()
            if mentioned_bot:
                human_tasks = [row for row in open_tasks if row["state"] == "human_active"]
                if len(human_tasks) == 1:
                    task_id = human_tasks[0]["task_id"]
                elif len(open_tasks) == 1:
                    task_id = open_tasks[0]["task_id"]
                else:
                    task_id = None
            else:
                task_id = open_tasks[0]["task_id"] if len(open_tasks) == 1 else None
            conn.execute(
                "INSERT INTO conversation_events "
                "(event_id, external_event_id, chat_id, sender_id, sender_role, "
                "event_kind, mentioned_bot, content_sha256, content_length, "
                "received_at, created_at) "
                "VALUES (?, ?, ?, ?, 'staff', 'message', ?, ?, ?, ?, ?)",
                (
                    event_id,
                    external_event_id,
                    chat_id,
                    staff_id,
                    int(mentioned_bot),
                    digest,
                    length,
                    received_iso,
                    created_iso,
                ),
            )
            if task_id:
                next_state = "waiting_quiet" if mentioned_bot else "human_active"
                conn.execute(
                    "UPDATE conversation_tasks SET state = ?, last_staff_at = ?, "
                    "version = version + 1, "
                    "updated_at = ? WHERE task_id = ?",
                    (next_state, received_iso, created_iso, task_id),
                )

        return EventReceipt(event_id=event_id, task_id=task_id, accepted=True, duplicate=False)

    def set_state(
        self,
        task_id: str,
        state: ConversationState,
        *,
        human_owner_id: str | None = None,
    ) -> ConversationTask | None:
        """Transition a task state and optionally record the human owner."""
        updated_iso = iso_timestamp()
        with self._connection() as conn:
            conn.execute(
                "UPDATE conversation_tasks SET state = ?, "
                "human_owner_id = COALESCE(?, human_owner_id), "
                "version = version + 1, updated_at = ? WHERE task_id = ?",
                (state, human_owner_id, updated_iso, task_id),
            )
        return self.get_task(task_id)

    def list_customer_event_ids(self, task: ConversationTask) -> list[str]:
        """Return customer event keys belonging to one conversation task."""
        return [event_id for event_id, _ in self.list_customer_event_refs(task)]

    def list_customer_event_refs(self, task: ConversationTask) -> list[tuple[str, int]]:
        """Return only the event/demand slots assigned to one task.

        The mapping is the isolation boundary: two tasks from the same chat may
        point at the same inbound event only when that event contained multiple
        demand slots, and each task receives its own demand index.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT event_id, demand_index FROM conversation_task_events "
                "WHERE task_id = ? ORDER BY linked_at ASC, demand_index ASC",
                (task.task_id,),
            ).fetchall()
        if rows:
            return [(row["event_id"], int(row["demand_index"])) for row in rows]

        # Compatibility for tasks created before the mapping table existed.
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT event_id FROM conversation_events "
                "WHERE chat_id = ? AND sender_role = 'customer' "
                "AND event_kind = 'message' AND created_at >= ? "
                "AND received_at <= ? ORDER BY received_at ASC, created_at ASC",
                (task.chat_id, task.created_at, task.latest_customer_at or task.updated_at),
            ).fetchall()
        return [(row["event_id"], 0) for row in rows]

    def claim_ready_tasks(
        self,
        *,
        now: datetime | str | None = None,
        quiet_window_seconds: int = 120,
        limit: int = 20,
    ) -> list[ConversationTask]:
        """Atomically claim tasks whose customer quiet window has elapsed.

        A staff event after the latest customer event blocks the claim. A later
        customer event advances ``latest_customer_at`` and makes the task
        eligible again after a fresh quiet window.
        """
        if quiet_window_seconds < 0:
            raise ValueError("quiet_window_seconds must be non-negative")
        if limit <= 0:
            return []
        now_iso = iso_timestamp(now)
        cutoff_iso = iso_timestamp(
            datetime.fromisoformat(now_iso) - timedelta(seconds=quiet_window_seconds)
        )
        claimed_at = iso_timestamp()

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT task_id FROM conversation_tasks "
                "WHERE state = 'waiting_quiet' "
                "AND latest_customer_at IS NOT NULL "
                "AND latest_customer_at <= ? "
                "AND (last_staff_at IS NULL OR last_staff_at < latest_customer_at) "
                "ORDER BY latest_customer_at ASC LIMIT ?",
                (cutoff_iso, limit),
            ).fetchall()
            task_ids = [row["task_id"] for row in rows]
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                conn.execute(
                    f"UPDATE conversation_tasks SET state = 'processing', "
                    f"version = version + 1, updated_at = ? "
                    f"WHERE state = 'waiting_quiet' AND task_id IN ({placeholders})",
                    (claimed_at, *task_ids),
                )

            claimed_rows = []
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                claimed_rows = conn.execute(
                    f"SELECT * FROM conversation_tasks WHERE state = 'processing' "
                    f"AND task_id IN ({placeholders}) ORDER BY latest_customer_at ASC",
                    task_ids,
                ).fetchall()

        return [task for row in claimed_rows if (task := self._task_from_row(row)) is not None]

    def count_events(self, chat_id: str) -> int:
        """Return event count for test/diagnostic purposes."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM conversation_events WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return int(row["count"])
