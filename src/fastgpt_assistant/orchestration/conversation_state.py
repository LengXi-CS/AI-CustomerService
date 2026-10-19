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

from fastgpt_assistant.orchestration.conversation_issues import ConversationIssues

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
    reason: str = ""


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
        self.issues = ConversationIssues(self)

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
                CREATE INDEX IF NOT EXISTS idx_conversation_events_sender_kind
                    ON conversation_events(chat_id, sender_id, event_kind, received_at);
                CREATE TABLE IF NOT EXISTS conversation_revocations (
                    chat_id TEXT NOT NULL, external_event_id TEXT NOT NULL,
                    revoked_at TEXT NOT NULL, PRIMARY KEY(chat_id,external_event_id)
                );

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
                CREATE TABLE IF NOT EXISTS conversation_delivery_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL,
                    text_status TEXT NOT NULL,
                    text_format TEXT NOT NULL,
                    message_id TEXT NOT NULL DEFAULT '',
                    reply_to_message_id TEXT NOT NULL DEFAULT '',
                    occurred_at TEXT NOT NULL
                );
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

    def record_delivery_attempt(
        self, *, task_id: str, chat_id: str, text_status: str, text_format: str,
        message_id: str = "", reply_to_message_id: str = "",
    ) -> None:
        """Retain delivery receipts, including uncertain sends, without message bodies."""
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO conversation_delivery_attempts "
                "(task_id, chat_id, text_status, text_format, message_id, "
                "reply_to_message_id, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, chat_id, text_status, text_format, message_id,
                 reply_to_message_id, iso_timestamp()),
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

    @staticmethod
    def _customer_receipt(conn, event, *, reason="duplicate_event") -> EventReceipt:
        task_ids = tuple(row[0] for row in conn.execute(
            "SELECT task_id FROM conversation_task_events WHERE event_id=? ORDER BY demand_index",
            (event["event_id"],),
        ))
        return EventReceipt(event["event_id"], task_ids[0] if task_ids else None,
                            False, True, task_ids, reason)

    def begin_customer_message(
        self, *, chat_id: str, customer_id: str, external_event_id: str,
        received_at: datetime | str, text: str,
    ) -> EventReceipt:
        """Register arrival before any model wait; bodies remain in the vault."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM conversation_events WHERE chat_id=? AND external_event_id=?",
                (chat_id, external_event_id),
            ).fetchone()
            if conn.execute(
                "SELECT 1 FROM conversation_revocations WHERE chat_id=? AND external_event_id=?",
                (chat_id, external_event_id),
            ).fetchone():
                return EventReceipt(existing["event_id"] if existing else "", None,
                                    False, False, reason="revoked_event")
            if existing:
                if existing["event_kind"] not in {"pending_message", "blocked_message"}:
                    return self._customer_receipt(conn, existing)
                conn.execute("UPDATE conversation_events SET event_kind='pending_message' "
                             "WHERE event_id=?", (existing["event_id"],))
                return EventReceipt(existing["event_id"], None, True, False)
            event_id = str(uuid.uuid4())
            digest, length = content_digest(text)
            conn.execute(
                "INSERT INTO conversation_events "
                "(event_id,external_event_id,chat_id,sender_id,sender_role,event_kind,"
                "mentioned_bot,content_sha256,content_length,received_at,created_at) "
                "VALUES(?,?,?,?,'customer','pending_message',0,?,?,?,?)",
                (event_id, external_event_id, chat_id, customer_id, digest, length,
                 iso_timestamp(received_at), iso_timestamp()),
            )
        return EventReceipt(event_id, None, True, False)

    def abandon_customer_message(self, event_id: str) -> None:
        """An unsuccessful callback must not leave a permanent pending fence."""
        with self._connection() as conn:
            conn.execute("UPDATE conversation_events SET event_kind='blocked_message' "
                         "WHERE event_id=? AND event_kind='pending_message'", (event_id,))

    def pending_customer_events(self) -> list[dict]:
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM conversation_events WHERE event_kind='pending_message' "
                "AND sender_role='customer' ORDER BY received_at,rowid"
            )]

    def customer_event_is_pending(self, event_id: str) -> bool:
        with self._connection() as conn:
            return conn.execute(
                "SELECT 1 FROM conversation_events WHERE event_id=? "
                "AND event_kind='pending_message'", (event_id,),
            ).fetchone() is not None

    @staticmethod
    def _has_pending_customer_events(conn, chat_id: str, *, customer_id="", at="") -> bool:
        return conn.execute(
            "SELECT 1 FROM conversation_events WHERE chat_id=? AND sender_role='customer' "
            "AND event_kind='pending_message' AND (?='' OR sender_id=?) "
            "AND (?='' OR received_at<=?) LIMIT 1",
            (chat_id, customer_id, customer_id, at, at),
        ).fetchone() is not None

    def has_pending_customer_events(self, chat_id: str, *, customer_id="", at="") -> bool:
        with self._connection() as conn:
            return self._has_pending_customer_events(conn, chat_id, customer_id=customer_id, at=at)

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
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM conversation_events "
                "WHERE chat_id = ? AND external_event_id = ?",
                (chat_id, external_event_id),
            ).fetchone()
            if conn.execute(
                "SELECT 1 FROM conversation_revocations WHERE chat_id=? AND external_event_id=?",
                (chat_id, external_event_id),
            ).fetchone():
                return EventReceipt(existing["event_id"] if existing else "", None,
                                    False, False, reason="revoked_event")
            if existing and existing["event_kind"] != "pending_message":
                return self._customer_receipt(conn, existing)
            if existing:
                event_id = existing["event_id"]
                received_iso, customer_id = existing["received_at"], existing["sender_id"]
                conn.execute("UPDATE conversation_events SET event_kind='message' WHERE event_id=?",
                             (event_id,))
            else:
                conn.execute(
                    "INSERT INTO conversation_events "
                    "(event_id, external_event_id, chat_id, sender_id, sender_role, "
                    "event_kind, mentioned_bot, content_sha256, content_length, "
                    "received_at, created_at) "
                    "VALUES (?, ?, ?, ?, 'customer', 'message', 0, ?, ?, ?, ?)",
                    (event_id, external_event_id, chat_id, customer_id, digest, length,
                     received_iso, created_iso),
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

            media_ids = self._pending_customer_media(
                conn, chat_id, customer_id=customer_id, before=received_iso,
                exclude_event_id=event_id,
            )
            conn.executemany(
                "INSERT OR IGNORE INTO conversation_message_tasks VALUES(?,?,?)",
                [(chat_id, msg_id, task_id) for msg_id in media_ids for task_id in task_ids],
            )

        return EventReceipt(
            event_id=event_id,
            task_id=task_ids[0],
            accepted=True,
            duplicate=False,
            task_ids=tuple(task_ids),
        )

    def record_customer_media(
        self, *, chat_id: str, customer_id: str, external_event_id: str,
        received_at: datetime | str,
    ) -> None:
        """Persist image identity before download, without creating a demand."""
        digest, length = content_digest("[image]")
        with self._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO conversation_events "
                "(event_id,external_event_id,chat_id,sender_id,sender_role,event_kind,"
                "mentioned_bot,content_sha256,content_length,received_at,created_at) "
                "SELECT ?,?,?,?,'customer','image',0,?,?,?,? WHERE NOT EXISTS "
                "(SELECT 1 FROM conversation_revocations WHERE chat_id=? AND external_event_id=?)",
                (str(uuid.uuid4()), external_event_id, chat_id, customer_id, digest, length,
                 iso_timestamp(received_at), iso_timestamp(), chat_id, external_event_id),
            )

    @staticmethod
    def _pending_customer_media(
        conn, chat_id: str, *, customer_id="", before="", exclude_event_id="",
    ) -> list[str]:
        rows = conn.execute(
            "SELECT media.external_event_id FROM conversation_events media "
            "WHERE media.chat_id=? AND media.sender_role='customer' AND media.event_kind='image' "
            "AND (?='' OR media.sender_id=?) AND (?='' OR media.received_at<=?) "
            "AND (?='' OR media.received_at<>? OR media.rowid<"
            "(SELECT rowid FROM conversation_events WHERE event_id=?)) "
            "AND NOT EXISTS (SELECT 1 FROM conversation_message_tasks bound "
            "WHERE bound.chat_id=media.chat_id AND bound.message_id=media.external_event_id) "
            "AND NOT EXISTS (SELECT 1 FROM conversation_events text "
            "WHERE text.chat_id=media.chat_id "
            "AND text.sender_id=media.sender_id AND text.sender_role='customer' "
            "AND text.event_kind IN "
            "('message','pending_message','blocked_message','revoked_message') "
            "AND text.event_id<>? "
            "AND (?='' OR text.received_at<=?) AND (text.received_at>media.received_at "
            "OR (text.received_at=media.received_at AND text.rowid>media.rowid))) "
            "ORDER BY media.received_at,media.rowid",
            (chat_id, customer_id, customer_id, before, before,
             exclude_event_id, before, exclude_event_id, exclude_event_id, before, before),
        ).fetchall()
        return [row[0] for row in rows]

    def pending_customer_media_ids(self, chat_id: str) -> set[str]:
        with self._connection() as conn:
            return set(self._pending_customer_media(conn, chat_id))

    def retained_customer_media_ids(self, chat_id: str) -> set[str]:
        """Read pending and bound images from one snapshot during midnight cleanup."""
        with self._connection() as conn:
            conn.execute("BEGIN")
            active = {row[0] for row in conn.execute(
                "SELECT link.message_id FROM conversation_message_tasks link "
                "JOIN conversation_tasks task ON task.task_id=link.task_id "
                "WHERE task.chat_id=? AND task.state IN "
                "('waiting_quiet','processing','human_active')", (chat_id,),
            )}
            return active | set(self._pending_customer_media(conn, chat_id))

    def recorded_media_ids(self, chat_id: str, message_ids: list[str]) -> set[str]:
        if not message_ids:
            return set()
        marks = ",".join("?" for _ in message_ids)
        with self._connection() as conn:
            return {row[0] for row in conn.execute(
                "SELECT external_event_id FROM conversation_events WHERE chat_id=? "
                f"AND external_event_id IN ({marks}) AND event_kind IN ('image','revoked_image')",
                (chat_id, *message_ids),
            )}

    def close_revoked_message(self, *, chat_id: str, external_event_id: str) -> tuple[str, ...]:
        """Close unfinished tasks linked to a revoked customer message."""
        updated_iso = iso_timestamp()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO conversation_revocations VALUES(?,?,?)",
                (chat_id, external_event_id, updated_iso),
            )
            conn.execute(
                "UPDATE conversation_events SET event_kind=CASE WHEN event_kind='image' "
                "THEN 'revoked_image' ELSE 'revoked_message' END WHERE chat_id=? "
                "AND external_event_id=? AND event_kind IN "
                "('image','pending_message','blocked_message')", (chat_id, external_event_id),
            )
            rows = conn.execute(
                "SELECT DISTINCT task.task_id FROM conversation_tasks AS task "
                "JOIN conversation_task_events AS link ON link.task_id = task.task_id "
                "JOIN conversation_events AS event ON event.event_id = link.event_id "
                "WHERE event.chat_id = ? AND event.external_event_id = ? "
                "AND event.sender_role = 'customer' "
                "AND task.state IN ('waiting_quiet', 'human_active', 'processing')",
                (chat_id, external_event_id),
            ).fetchall()
            task_ids = tuple(row["task_id"] for row in rows)
            for task_id in task_ids:
                conn.execute(
                    "UPDATE conversation_tasks SET state = 'closed', version = version + 1, "
                    "updated_at = ? WHERE task_id = ?",
                    (updated_iso, task_id),
                )
            self.issues.invalidate(conn, task_ids)
            self.issues.prune_association_retries(conn)
        return task_ids

    def record_staff_message(
        self,
        *,
        chat_id: str,
        staff_id: str,
        external_event_id: str,
        received_at: datetime | str,
        text: str,
        mentioned_bot: bool,
        quoted_msg_id: str = "",
        precise_issue_target: bool = False,
    ) -> EventReceipt:
        """Use a quote to select a customer before applying human takeover.

        Ordinary replies take over that customer's open tasks, including text
        accompanying a quoted order file. An explicit bot mention instead hands
        back only the uniquely referenced demand. Unresolved quotes must never
        fall back to an unrelated task just because it is the only open one.
        """
        received_iso = iso_timestamp(received_at)
        created_iso = iso_timestamp()
        digest, length = content_digest(text)
        event_id = str(uuid.uuid4())

        with self._connection() as conn:
            # Keep target selection and takeover atomic with poller claims.
            conn.execute("BEGIN IMMEDIATE")
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

            # An ordinary staff reply takes over every open task when they all
            # belong to one customer. A follow-up message may have created a
            # second task before the staff replied. Never suppress another
            # customer's task just because it shares the same group.
            # An @-mention still requires one task to hand back to the bot.
            open_tasks = conn.execute(
                "SELECT task_id, customer_id, state, latest_customer_at FROM conversation_tasks "
                "WHERE chat_id = ? AND state IN "
                "('waiting_quiet', 'human_active', 'processing') "
                "AND (last_staff_at IS NULL OR last_staff_at<=?) "
                "ORDER BY updated_at DESC, task_id DESC",
                (chat_id, received_iso),
            ).fetchall()
            control_anchors: tuple[str, ...] = ()
            if quoted_msg_id:
                quote_states = (
                    "('waiting_quiet','human_active','processing','responded')"
                    if precise_issue_target
                    else "('waiting_quiet','human_active','processing')"
                )
                referenced = self.issues._referenced_tasks(conn, chat_id, quoted_msg_id)
                placeholders = ",".join("?" for _ in referenced) or "NULL"
                quoted_tasks = conn.execute(
                    "SELECT task.task_id, task.customer_id FROM conversation_tasks AS task "
                    f"WHERE task.task_id IN ({placeholders}) AND task.chat_id = ? "
                    f"AND task.state IN {quote_states} "
                    "AND task.latest_customer_at <= ? "
                    "AND (task.last_staff_at IS NULL OR task.last_staff_at<=?) "
                    "AND NOT EXISTS (SELECT 1 FROM conversation_issue_tasks link "
                    "JOIN conversation_issues issue ON issue.issue_id=link.issue_id "
                    "WHERE link.task_id=task.task_id AND issue.state='closed') "
                    "AND NOT EXISTS (SELECT 1 FROM conversation_issue_tasks own "
                    "JOIN conversation_issue_tasks member ON member.issue_id=own.issue_id "
                    "JOIN conversation_tasks newer ON newer.task_id=member.task_id "
                    "WHERE own.task_id=task.task_id AND newer.last_staff_at>?)",
                    (*referenced, chat_id, received_iso, received_iso, received_iso),
                ).fetchall()
                if precise_issue_target:
                    referenced_ids = {row["task_id"] for row in quoted_tasks}
                    marks = ",".join("?" for _ in referenced_ids)
                    issue_members = (
                        {
                            row["task_id"]
                            for row in conn.execute(
                                "SELECT task_id FROM conversation_issue_tasks WHERE issue_id IN "
                            "(SELECT issue_id FROM conversation_issue_tasks "
                            f"WHERE task_id IN ({marks}))",
                                tuple(referenced_ids),
                            )
                        }
                        if referenced_ids
                        else set()
                    )
                    task_ids = (
                        tuple(
                            row["task_id"]
                            for row in open_tasks
                            if row["task_id"] in referenced_ids | issue_members
                            and row["latest_customer_at"] <= received_iso
                        )
                        if not mentioned_bot or len(quoted_tasks) == 1
                        else ()
                    )
                    if not mentioned_bot or len(quoted_tasks) == 1:
                        # Record control of an answered issue even when there
                        # are no unfinished tasks to migrate or hand to the bot.
                        control_anchors = tuple(referenced_ids)
                elif mentioned_bot:
                    task_ids = (quoted_tasks[0]["task_id"],) if len(quoted_tasks) == 1 else ()
                else:
                    quoted_customers = {row["customer_id"] for row in quoted_tasks}
                    quoted_customer = (
                        next(iter(quoted_customers)) if len(quoted_customers) == 1 else None
                    )
                    task_ids = tuple(
                        row["task_id"]
                        for row in open_tasks
                        if row["customer_id"] == quoted_customer
                        # A delayed staff callback must not take over a newer demand.
                        and row["latest_customer_at"] is not None
                        and row["latest_customer_at"] <= received_iso
                    )
            elif mentioned_bot:
                human_tasks = [row for row in open_tasks if row["state"] == "human_active"]
                if len(human_tasks) == 1:
                    task_ids = (human_tasks[0]["task_id"],)
                elif len(open_tasks) == 1:
                    task_ids = (open_tasks[0]["task_id"],)
                else:
                    task_ids = ()
            else:
                customer_ids = {row["customer_id"] for row in open_tasks}
                task_ids = (
                    tuple(row["task_id"] for row in open_tasks
                          if row["latest_customer_at"] <= received_iso)
                    if len(customer_ids) == 1 and not precise_issue_target else ()
                )
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
            next_state = "waiting_quiet" if mentioned_bot else "human_active"
            controlled_ids = tuple(dict.fromkeys((*task_ids, *control_anchors)))
            for task_id in controlled_ids:
                conn.execute(
                    "UPDATE conversation_tasks SET state = CASE WHEN state='responded' "
                    "THEN state ELSE ? END, last_staff_at = ?, human_owner_id=?, "
                    "version = version + 1, "
                    "updated_at = ? WHERE task_id = ?",
                    (next_state, received_iso, None if mentioned_bot else staff_id,
                     created_iso, task_id),
                )
            self.issues.invalidate(
                conn, controlled_ids, state="open" if mentioned_bot else "human_active",
                staff_event_id=event_id,
            )
            if precise_issue_target and not mentioned_bot and not controlled_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO conversation_staff_reviews(event_id) VALUES(?)",
                    (event_id,),
                )

        return EventReceipt(
            event_id=event_id,
            task_id=task_ids[0] if len(task_ids) == 1 else None,
            accepted=True,
            duplicate=False,
            task_ids=task_ids,
        )

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
            if state in {"closed", "blocked", "responded"}:
                self.issues.prune_association_retries(conn)
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
        quiet_windows_by_chat: dict[str, int] | None = None,
        limit: int = 20,
        require_issue_chats: tuple[str, ...] = (),
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
        cutoffs = {}
        for chat_id, seconds in (quiet_windows_by_chat or {}).items():
            if (isinstance(seconds, bool) or not isinstance(seconds, int)
                    or not 0 <= seconds <= 86400):
                raise ValueError("group wait time must be an integer between 0 and 86400")
            cutoffs[chat_id] = iso_timestamp(
                datetime.fromisoformat(now_iso) - timedelta(seconds=seconds)
            )
        # Fetch the broadest due range, then filter before applying the batch
        # limit so a slow group cannot starve an independently ready fast group.
        candidate_cutoff = max([cutoff_iso, *cutoffs.values()])
        claimed_at = iso_timestamp()

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.issues.prune_association_retries(conn)
            rows = conn.execute(
                "SELECT task.task_id,task.chat_id,link.issue_id, "
                "COALESCE(issue.latest_customer_at,task.latest_customer_at) AS ready_at "
                "FROM conversation_tasks task "
                "LEFT JOIN conversation_issue_tasks link ON link.task_id=task.task_id "
                "LEFT JOIN conversation_issues issue ON issue.issue_id=link.issue_id "
                "WHERE task.state='waiting_quiet' AND task.latest_customer_at IS NOT NULL "
                "AND COALESCE(issue.latest_customer_at,task.latest_customer_at)<=? "
                "AND (task.last_staff_at IS NULL OR task.last_staff_at<task.latest_customer_at) "
                "AND NOT EXISTS (SELECT 1 FROM conversation_events incoming "
                "WHERE incoming.chat_id=task.chat_id AND incoming.sender_id=task.customer_id "
                "AND incoming.event_kind='pending_message' AND incoming.sender_role='customer') "
                "AND NOT EXISTS (SELECT 1 FROM conversation_association_retries retry "
                "WHERE retry.chat_id=task.chat_id AND retry.customer_id=task.customer_id) "
                "AND NOT EXISTS (SELECT 1 FROM conversation_staff_reviews review "
                "JOIN conversation_events staff ON staff.event_id=review.event_id "
                "WHERE review.status='pending' AND staff.chat_id=task.chat_id "
                "AND staff.received_at>=task.latest_customer_at) "
                "AND (issue.issue_id IS NULL OR (issue.state='open' AND NOT EXISTS "
                "(SELECT 1 FROM conversation_tasks busy JOIN conversation_issue_tasks member "
                "ON member.task_id=busy.task_id WHERE member.issue_id=issue.issue_id "
                "AND busy.state='processing'))) ORDER BY task.latest_customer_at,task.task_id",
                (candidate_cutoff,),
            ).fetchall()
            task_ids = []
            seen_issues = set()
            for row in rows:
                if row["ready_at"] > cutoffs.get(row["chat_id"], cutoff_iso):
                    continue
                issue_id = row["issue_id"]
                if not issue_id and row["chat_id"] in require_issue_chats:
                    continue
                if issue_id and issue_id in seen_issues:
                    continue
                task_ids.append(row["task_id"])
                if issue_id:
                    seen_issues.add(issue_id)
                    conn.execute(
                        "UPDATE conversation_tasks SET state='processing',version=version+1, "
                        "updated_at=? "
                        "WHERE state='waiting_quiet' AND task_id IN "
                        "(SELECT task_id FROM conversation_issue_tasks WHERE issue_id=?)",
                        (claimed_at, issue_id),
                    )
                if len(task_ids) >= limit:
                    break
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
