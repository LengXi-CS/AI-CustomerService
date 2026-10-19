"""Metadata-only associations and durable acknowledgement receipts for demand tasks."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone


class ConversationIssues:
    def __init__(self, store) -> None:
        self.store = store
        with store._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS conversation_issues (
                    issue_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, customer_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'open', latest_customer_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0, soothe_status TEXT NOT NULL DEFAULT 'none',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_issues_customer
                    ON conversation_issues(chat_id, customer_id, updated_at);
                CREATE TABLE IF NOT EXISTS conversation_issue_tasks (
                    task_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, relation TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_issue_members ON conversation_issue_tasks(issue_id);
                CREATE TABLE IF NOT EXISTS conversation_staff_tasks (
                    event_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    PRIMARY KEY(event_id, task_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_message_tasks (
                    chat_id TEXT NOT NULL, message_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    PRIMARY KEY(chat_id,message_id,task_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_association_retries (
                    chat_id TEXT NOT NULL, customer_id TEXT NOT NULL, attempts INTEGER NOT NULL,
                    next_attempt_at TEXT NOT NULL, PRIMARY KEY(chat_id,customer_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_staff_reviews (
                    event_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL DEFAULT ''
                );
            """)
            self.prune_association_retries(conn)

    @staticmethod
    def prune_association_retries(conn) -> None:
        """A customer retry can fence sending only while there is unassigned work."""
        conn.execute(
            "DELETE FROM conversation_association_retries AS retry WHERE NOT EXISTS "
            "(SELECT 1 FROM conversation_tasks task LEFT JOIN conversation_issue_tasks link "
            "ON link.task_id=task.task_id WHERE task.chat_id=retry.chat_id "
            "AND task.customer_id=retry.customer_id AND link.task_id IS NULL "
            "AND task.state IN ('waiting_quiet','human_active'))"
        )

    def cleanup_association_retries(self) -> None:
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.prune_association_retries(conn)

    def get(self, task_id: str):
        with self.store._connection() as conn:
            row = conn.execute(
                "SELECT issue.* FROM conversation_issues issue JOIN conversation_issue_tasks link "
                "ON link.issue_id = issue.issue_id WHERE link.task_id = ?",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def members(self, issue_id: str, *, pending_only=False) -> list[str]:
        states = (
            "('waiting_quiet','processing','human_active')"
            if pending_only
            else ("('waiting_quiet','processing','human_active','responded')")
        )
        with self.store._connection() as conn:
            rows = conn.execute(
                "SELECT task.task_id FROM conversation_tasks task "
                "JOIN conversation_issue_tasks link "
                "ON link.task_id = task.task_id WHERE link.issue_id = ? AND task.state IN "
                + states
                + " ORDER BY task.latest_customer_at, task.created_at, task.task_id",
                (issue_id,),
            ).fetchall()
        return [row["task_id"] for row in rows]

    def unassigned(
        self, chat_ids: list[str], cutoff: str, *, limit=40, now="", cutoffs_by_chat=None,
    ):
        if not chat_ids:
            return []
        marks = ",".join("?" for _ in chat_ids)
        cutoffs_by_chat = cutoffs_by_chat or {}
        candidate_cutoff = max([cutoff, *cutoffs_by_chat.values()])
        with self.store._connection() as conn:
            rows = conn.execute(
                "SELECT task.* FROM conversation_tasks task "
                "LEFT JOIN conversation_issue_tasks link "
                "ON link.task_id = task.task_id "
                "LEFT JOIN conversation_association_retries retry "
                "ON retry.chat_id=task.chat_id AND retry.customer_id=task.customer_id "
                "WHERE link.task_id IS NULL "
                "AND (retry.next_attempt_at IS NULL OR retry.next_attempt_at<=?) "
                "AND task.state IN ('waiting_quiet','human_active') "
                "AND task.latest_customer_at <= ? "
                f"AND task.chat_id IN ({marks}) ORDER BY task.latest_customer_at",
                (now or cutoff, candidate_cutoff, *chat_ids),
            ).fetchall()
        return [dict(row) for row in rows
                if row["latest_customer_at"] <= cutoffs_by_chat.get(row["chat_id"], cutoff)][:limit]

    def candidates(self, chat_id: str, customer_id: str, *, since=""):
        with self.store._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM conversation_issues WHERE chat_id = ? AND customer_id = ? "
                "AND state <> 'closed' AND (latest_customer_at>=? OR state='human_active' "
                "OR EXISTS (SELECT 1 FROM conversation_issue_tasks link "
                "JOIN conversation_tasks task "
                "ON task.task_id=link.task_id WHERE link.issue_id=conversation_issues.issue_id "
                "AND task.state IN ('waiting_quiet','processing'))) ORDER BY updated_at DESC",
                (chat_id, customer_id, since),
            ).fetchall()
        return [dict(row) for row in rows]

    def referenced_tasks(self, chat_id: str, message_id: str) -> set[str]:
        if not message_id:
            return set()
        with self.store._connection() as conn:
            return self._referenced_tasks(conn, chat_id, message_id)

    @staticmethod
    def _referenced_tasks(conn, chat_id: str, message_id: str) -> set[str]:
        rows = conn.execute(
                "SELECT link.task_id FROM conversation_task_events link "
                "JOIN conversation_events event ON event.event_id = link.event_id "
                "WHERE event.chat_id = ? AND event.external_event_id = ? "
                "UNION SELECT link.task_id FROM conversation_staff_tasks link "
                "JOIN conversation_events event ON event.event_id = link.event_id "
                "WHERE event.chat_id = ? AND event.external_event_id = ? "
                "UNION SELECT task_id FROM conversation_delivery_attempts "
                "WHERE chat_id=? AND message_id=? AND text_status='sent' "
                "UNION SELECT task_id FROM conversation_message_tasks WHERE chat_id=? "
                "AND message_id=?",
                (chat_id, message_id) * 4,
            ).fetchall()
        return {row["task_id"] for row in rows}

    def bind_messages(self, chat_id: str, task_id: str, message_ids) -> None:
        """Persist attachment/quote identity; never store message bodies here."""
        with self.store._connection() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO conversation_message_tasks "
                "SELECT ?,?,task_id FROM conversation_tasks WHERE task_id=? AND chat_id=?",
                [(chat_id, msg_id, task_id, chat_id) for msg_id in message_ids if msg_id],
            )

    def message_ids(self, task_id: str) -> list[str]:
        with self.store._connection() as conn:
            return [row[0] for row in conn.execute(
                "SELECT message_id FROM conversation_message_tasks WHERE task_id=?", (task_id,)
            )]

    def active_message_ids(self, chat_id: str) -> set[str]:
        with self.store._connection() as conn:
            return {row[0] for row in conn.execute(
                "SELECT link.message_id FROM conversation_message_tasks link "
                "JOIN conversation_tasks task ON task.task_id=link.task_id "
                "WHERE task.chat_id=? AND task.state IN "
                "('waiting_quiet','processing','human_active')", (chat_id,),
            )}

    def defer_association(self, chat_id: str, customer_id: str, *, now: str) -> None:
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending = conn.execute(
                "SELECT 1 FROM conversation_tasks task LEFT JOIN conversation_issue_tasks link "
                "ON link.task_id=task.task_id WHERE task.chat_id=? AND task.customer_id=? "
                "AND link.task_id IS NULL "
                "AND task.state IN ('waiting_quiet','human_active') LIMIT 1",
                (chat_id, customer_id),
            ).fetchone()
            if not pending:
                self.prune_association_retries(conn)
                return
            previous = conn.execute(
                "SELECT attempts FROM conversation_association_retries WHERE chat_id=? "
                "AND customer_id=?", (chat_id, customer_id),
            ).fetchone()
            attempts = (previous[0] if previous else 0) + 1
            retry_at = (datetime.fromisoformat(now) + timedelta(
                seconds=min(300, 30 * 2 ** min(attempts - 1, 4))
            )).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO conversation_association_retries VALUES(?,?,?,?)",
                (chat_id, customer_id, attempts, retry_at),
            )

    def clear_association_retry(self, chat_id: str, customer_id: str) -> None:
        with self.store._connection() as conn:
            conn.execute("DELETE FROM conversation_association_retries WHERE chat_id=? "
                         "AND customer_id=?", (chat_id, customer_id))

    def assign(self, task_id: str, *, target_id="", relation="new"):
        from fastgpt_assistant.orchestration.conversation_state import iso_timestamp

        now = iso_timestamp()
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT issue.* FROM conversation_issues issue JOIN conversation_issue_tasks link "
                "ON link.issue_id = issue.issue_id WHERE link.task_id = ?",
                (task_id,),
            ).fetchone()
            if current:
                return dict(current)
            task = conn.execute(
                "SELECT * FROM conversation_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None or task["state"] not in {"waiting_quiet", "human_active"}:
                return None
            target = conn.execute(
                "SELECT * FROM conversation_issues WHERE issue_id = ? AND chat_id = ? "
                "AND customer_id = ? AND state <> 'closed'",
                (target_id, task["chat_id"], task["customer_id"]),
            ).fetchone()
            if relation == "renew" and target and target["soothe_status"] == "none":
                answered = conn.execute(
                    "SELECT 1 FROM conversation_tasks task JOIN conversation_issue_tasks link "
                    "ON link.task_id=task.task_id WHERE link.issue_id=? "
                    "AND task.state='responded' LIMIT 1",
                    (target["issue_id"],),
                ).fetchone()
                if not answered:
                    relation = "continue"
            if relation == "new" or (
                relation == "renew" and target and target["state"] != "human_active"
            ):
                target = None
            if target is None:
                issue_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO conversation_issues "
                    "(issue_id,chat_id,customer_id,state,latest_customer_at,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        issue_id,
                        task["chat_id"],
                        task["customer_id"],
                        "human_active" if task["state"] == "human_active" else "open",
                        task["latest_customer_at"],
                        now,
                        now,
                    ),
                )
                relation = "new"
            else:
                issue_id = target["issue_id"]
                if relation == "resolved":
                    conn.execute(
                        "UPDATE conversation_issues SET state='closed', "
                        "version=version+1,updated_at=? "
                        "WHERE issue_id=?",
                        (now, issue_id),
                    )
                    conn.execute(
                        "UPDATE conversation_tasks SET state='closed',version=version+1, "
                        "updated_at=? WHERE task_id IN "
                        "(SELECT task_id FROM conversation_issue_tasks WHERE issue_id=?) "
                        "AND state IN ('waiting_quiet','processing','human_active')",
                        (now, issue_id),
                    )
                elif relation != "ack":
                    conn.execute(
                        "UPDATE conversation_issues SET "
                        "latest_customer_at=MAX(latest_customer_at,?), "
                        "version=version+1,updated_at=? WHERE issue_id=?",
                        (task["latest_customer_at"], now, issue_id),
                    )
            conn.execute(
                "INSERT INTO conversation_issue_tasks(task_id,issue_id,relation) VALUES(?,?,?)",
                (task_id, issue_id, relation),
            )
            issue = conn.execute(
                "SELECT * FROM conversation_issues WHERE issue_id=?", (issue_id,)
            ).fetchone()
            state = None
            if relation in {"ack", "resolved"}:
                state = "closed"
            elif issue["state"] == "human_active" or task["state"] == "human_active":
                state = "human_active"
                owner = conn.execute(
                    "SELECT task.human_owner_id,task.last_staff_at FROM conversation_tasks task "
                    "JOIN conversation_issue_tasks link ON link.task_id=task.task_id "
                    "WHERE link.issue_id=? AND task.last_staff_at IS NOT NULL "
                    "ORDER BY task.last_staff_at DESC LIMIT 1", (issue_id,),
                ).fetchone()
                conn.execute(
                    "UPDATE conversation_issues SET state='human_active',version=version+1 "
                    "WHERE issue_id=?",
                    (issue_id,),
                )
                conn.execute(
                    "UPDATE conversation_tasks SET state='human_active',version=version+1, "
                    "human_owner_id=COALESCE(?,human_owner_id), "
                    "last_staff_at=COALESCE(?,last_staff_at), "
                    "updated_at=? WHERE task_id IN "
                    "(SELECT task_id FROM conversation_issue_tasks WHERE issue_id=?) "
                    "AND state IN ('waiting_quiet','processing')",
                    (owner["human_owner_id"] if owner else None,
                     owner["last_staff_at"] if owner else None, now, issue_id),
                )
            elif issue["soothe_status"] != "none":
                state = "responded"
            if state:
                conn.execute(
                    "UPDATE conversation_tasks SET state=?,version=version+1,updated_at=? "
                    "WHERE task_id=?",
                    (state, now, task_id),
                )
            return dict(
                conn.execute(
                    "SELECT * FROM conversation_issues WHERE issue_id=?", (issue_id,)
                ).fetchone()
            )

    def can_send(self, issue_id: str, version: int, *, soothe=False) -> bool:
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.prune_association_retries(conn)
            issue = conn.execute(
                "SELECT * FROM conversation_issues WHERE issue_id=?", (issue_id,)
            ).fetchone()
            if not issue or issue["state"] != "open" or issue["version"] != version:
                return False
            if self.store._has_pending_customer_events(
                conn, issue["chat_id"], customer_id=issue["customer_id"]
            ):
                return False
            if conn.execute(
                "SELECT 1 FROM conversation_association_retries WHERE chat_id=? AND customer_id=?",
                (issue["chat_id"], issue["customer_id"]),
            ).fetchone():
                return False
            pending_staff = conn.execute(
                "SELECT 1 FROM conversation_staff_reviews review JOIN conversation_events event "
                "ON event.event_id=review.event_id WHERE review.status='pending' "
                "AND event.chat_id=? AND event.received_at>=(SELECT MIN(task.latest_customer_at) "
                "FROM conversation_tasks task JOIN conversation_issue_tasks link "
                "ON link.task_id=task.task_id WHERE link.issue_id=?) LIMIT 1",
                (issue["chat_id"], issue_id),
            ).fetchone()
            if pending_staff:
                return False
            # Classify every pending supplement from this customer before
            # sending; a long gap alone cannot prove a different issue.
            pending = conn.execute(
                "SELECT 1 FROM conversation_tasks task LEFT JOIN conversation_issue_tasks link "
                "ON link.task_id=task.task_id WHERE link.task_id IS NULL "
                "AND task.chat_id=? AND task.customer_id=? "
                "AND task.state IN ('waiting_quiet','human_active') LIMIT 1",
                (issue["chat_id"], issue["customer_id"]),
            ).fetchone()
            if pending:
                return False
            if soothe:
                changed = conn.execute(
                    "UPDATE conversation_issues SET soothe_status='sending' "
                    "WHERE issue_id=? AND soothe_status='none'",
                    (issue_id,),
                ).rowcount
                return bool(changed)
            return True

    def recover_interrupted(self) -> None:
        """Run once at service startup; an ambiguous send must never be replayed."""
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE conversation_tasks SET state=CASE WHEN task_id IN "
                "(SELECT link.task_id FROM conversation_issue_tasks link "
                "JOIN conversation_issues issue ON issue.issue_id=link.issue_id "
                "WHERE issue.soothe_status<>'none') THEN 'responded' ELSE 'waiting_quiet' END, "
                "version=version+1 WHERE state='processing' AND task_id IN "
                "(SELECT task_id FROM conversation_issue_tasks)"
            )
            conn.execute(
                "UPDATE conversation_issues SET soothe_status='unknown' "
                "WHERE soothe_status='sending'"
            )

    def finish(self, issue_id: str, *, state="responded", soothe_status=None) -> None:
        from fastgpt_assistant.orchestration.conversation_state import iso_timestamp

        with self.store._connection() as conn:
            conn.execute(
                "UPDATE conversation_tasks SET state=?,version=version+1,updated_at=? "
                "WHERE state='processing' AND task_id IN "
                "(SELECT task_id FROM conversation_issue_tasks WHERE issue_id=?)",
                (state, iso_timestamp(), issue_id),
            )
            if soothe_status is not None:
                conn.execute(
                    "UPDATE conversation_issues SET soothe_status=? "
                    "WHERE issue_id=? AND soothe_status='sending'",
                    (soothe_status, issue_id),
                )

    def invalidate(self, conn, task_ids: tuple[str, ...], *, state=None, staff_event_id="") -> None:
        for task_id in task_ids:
            if staff_event_id:
                conn.execute(
                    "INSERT OR IGNORE INTO conversation_staff_tasks VALUES(?,?)",
                    (staff_event_id, task_id),
                )
            if state:
                conn.execute(
                    "UPDATE conversation_issues SET state=?,version=version+1 WHERE issue_id IN "
                    "(SELECT issue_id FROM conversation_issue_tasks WHERE task_id=?)",
                    (state, task_id),
                )
            else:
                conn.execute(
                    "UPDATE conversation_issues SET version=version+1 WHERE issue_id IN "
                    "(SELECT issue_id FROM conversation_issue_tasks WHERE task_id=?)",
                    (task_id,),
                )

    def within_window(self, issue: dict, at: str, seconds=120) -> bool:
        return abs(
            datetime.fromisoformat(at) - datetime.fromisoformat(issue["latest_customer_at"])
        ) <= timedelta(seconds=seconds)

    def pending_staff_reviews(self, chats: list[str], *, now: str):
        if not chats:
            return []
        marks = ",".join("?" for _ in chats)
        with self.store._connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT event.* FROM conversation_staff_reviews review JOIN conversation_events "
                "event ON event.event_id=review.event_id WHERE review.status='pending' "
                f"AND event.chat_id IN ({marks}) AND review.next_attempt_at<=? "
                "ORDER BY event.received_at LIMIT 40", (*chats, now),
            )]

    def queue_staff_reviews(self, chats: list[str], *, since: str) -> None:
        """Backfill previously unmatched staff events when upgrading a running deployment."""
        if not chats:
            return
        marks = ",".join("?" for _ in chats)
        with self.store._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO conversation_staff_reviews(event_id) "
                "SELECT event.event_id FROM conversation_events event "
                f"WHERE event.chat_id IN ({marks}) AND event.sender_role='staff' "
                "AND event.mentioned_bot=0 AND event.received_at>=? "
                "AND NOT EXISTS (SELECT 1 FROM conversation_staff_tasks linked "
                "WHERE linked.event_id=event.event_id) "
                "AND EXISTS (SELECT 1 FROM conversation_tasks task "
                "WHERE task.chat_id=event.chat_id "
                "AND task.state IN ('waiting_quiet','processing') "
                "AND task.latest_customer_at<=event.received_at)", (*chats, since),
            )

    def staff_candidates(self, chat_id: str, *, at: str):
        day_start = datetime.fromisoformat(at).astimezone(timezone(timedelta(hours=8))).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC).isoformat()
        with self.store._connection() as conn:
            return [self.store._task_from_row(row) for row in conn.execute(
                "SELECT * FROM conversation_tasks WHERE chat_id=? AND state IN "
                "('waiting_quiet','processing','human_active','responded') "
                "AND (state<>'responded' OR (latest_customer_at>=? "
                "AND EXISTS (SELECT 1 FROM conversation_issue_tasks link "
                "JOIN conversation_issues issue ON issue.issue_id=link.issue_id "
                "WHERE link.task_id=conversation_tasks.task_id AND issue.state<>'closed'))) "
                "AND latest_customer_at<=? "
                "AND (last_staff_at IS NULL OR last_staff_at<=?) ORDER BY latest_customer_at",
                (chat_id, day_start, at, at),
            )]

    def defer_staff_review(self, event_id: str, *, now: str) -> None:
        with self.store._connection() as conn:
            row = conn.execute("SELECT attempts FROM conversation_staff_reviews WHERE event_id=?",
                               (event_id,)).fetchone()
            if row:
                retry_at = (datetime.fromisoformat(now) + timedelta(
                    seconds=min(300, 30 * 2 ** min(row[0], 4))
                )).isoformat()
                conn.execute("UPDATE conversation_staff_reviews SET attempts=attempts+1, "
                             "next_attempt_at=? WHERE event_id=?", (retry_at, event_id))

    def apply_staff_review(self, event_id: str, task_ids: set[str]) -> tuple[str, ...]:
        """Commit semantic takeover atomically, rejecting other rooms and future work."""
        from .conversation_state import iso_timestamp

        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            event = conn.execute(
                "SELECT event.* FROM conversation_events event JOIN conversation_staff_reviews "
                "review ON review.event_id=event.event_id WHERE event.event_id=? "
                "AND review.status='pending'", (event_id,),
            ).fetchone()
            if not event:
                return ()
            # A customer's earlier callback may still be splitting text. No
            # result, including an empty one, can finish this review yet.
            if self.store._has_pending_customer_events(
                conn, event["chat_id"], at=event["received_at"]
            ):
                return ()
            marks = ",".join("?" for _ in task_ids) or "NULL"
            seeds = {row[0] for row in conn.execute(
                f"SELECT task_id FROM conversation_tasks WHERE task_id IN ({marks}) "
                "AND chat_id=? AND latest_customer_at<=?", (*task_ids, event["chat_id"],
                                                           event["received_at"]),
            )}
            seed_marks = ",".join("?" for _ in seeds) or "NULL"
            targets = {row[0] for row in conn.execute(
                f"SELECT task_id FROM conversation_tasks WHERE task_id IN ({seed_marks}) "
                "OR task_id IN (SELECT task_id FROM conversation_issue_tasks WHERE issue_id IN "
                f"(SELECT issue_id FROM conversation_issue_tasks WHERE task_id IN ({seed_marks})))",
                (*seeds, *seeds),
            )}
            applied = []
            for task_id in targets:
                changed = conn.execute(
                    "UPDATE conversation_tasks SET state=CASE WHEN state='responded' "
                    "THEN state ELSE 'human_active' END,last_staff_at=?, "
                    "human_owner_id=?,version=version+1,updated_at=? WHERE task_id=? "
                    "AND chat_id=? AND state IN "
                    "('waiting_quiet','processing','human_active','responded') "
                    "AND latest_customer_at<=? AND (last_staff_at IS NULL OR last_staff_at<=?) "
                    "AND NOT EXISTS (SELECT 1 FROM conversation_issue_tasks link "
                    "JOIN conversation_issues issue ON issue.issue_id=link.issue_id "
                    "WHERE link.task_id=conversation_tasks.task_id AND issue.state='closed') "
                    "AND NOT EXISTS (SELECT 1 FROM conversation_issue_tasks own "
                    "JOIN conversation_issue_tasks member ON member.issue_id=own.issue_id "
                    "JOIN conversation_tasks newer ON newer.task_id=member.task_id "
                    "WHERE own.task_id=conversation_tasks.task_id AND newer.last_staff_at>?)",
                    (event["received_at"], event["sender_id"], iso_timestamp(), task_id,
                     event["chat_id"], event["received_at"], event["received_at"],
                     event["received_at"]),
                ).rowcount
                if changed:
                    applied.append(task_id)
            self.invalidate(conn, tuple(applied), state="human_active", staff_event_id=event_id)
            conn.execute("UPDATE conversation_staff_reviews SET status='done' WHERE event_id=?",
                         (event_id,))
        return tuple(applied)
