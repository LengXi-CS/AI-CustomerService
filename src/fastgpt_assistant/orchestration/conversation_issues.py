"""Metadata-only associations and durable acknowledgement receipts for demand tasks."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta


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
            """)

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

    def unassigned(self, chat_ids: list[str], cutoff: str, *, limit=40):
        if not chat_ids:
            return []
        marks = ",".join("?" for _ in chat_ids)
        with self.store._connection() as conn:
            rows = conn.execute(
                "SELECT task.* FROM conversation_tasks task "
                "LEFT JOIN conversation_issue_tasks link "
                "ON link.task_id = task.task_id WHERE link.task_id IS NULL "
                "AND task.state IN ('waiting_quiet','human_active') "
                "AND task.latest_customer_at <= ? "
                f"AND task.chat_id IN ({marks}) ORDER BY task.latest_customer_at LIMIT ?",
                (cutoff, *chat_ids, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def candidates(self, chat_id: str, customer_id: str, *, limit=8):
        with self.store._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM conversation_issues WHERE chat_id = ? AND customer_id = ? "
                "AND state <> 'closed' ORDER BY updated_at DESC LIMIT ?",
                (chat_id, customer_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def referenced_tasks(self, chat_id: str, message_id: str) -> set[str]:
        if not message_id:
            return set()
        with self.store._connection() as conn:
            rows = conn.execute(
                "SELECT link.task_id FROM conversation_task_events link "
                "JOIN conversation_events event ON event.event_id = link.event_id "
                "WHERE event.chat_id = ? AND event.external_event_id = ? "
                "UNION SELECT link.task_id FROM conversation_staff_tasks link "
                "JOIN conversation_events event ON event.event_id = link.event_id "
                "WHERE event.chat_id = ? AND event.external_event_id = ? "
                "UNION SELECT task_id FROM conversation_delivery_attempts "
                "WHERE chat_id=? AND message_id=? AND text_status='sent'",
                (chat_id, message_id, chat_id, message_id, chat_id, message_id),
            ).fetchall()
        return {row["task_id"] for row in rows}

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
                conn.execute(
                    "UPDATE conversation_issues SET state='human_active',version=version+1 "
                    "WHERE issue_id=?",
                    (issue_id,),
                )
                conn.execute(
                    "UPDATE conversation_tasks SET state='human_active',version=version+1, "
                    "updated_at=? WHERE task_id IN "
                    "(SELECT task_id FROM conversation_issue_tasks WHERE issue_id=?) "
                    "AND state IN ('waiting_quiet','processing')",
                    (now, issue_id),
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
            issue = conn.execute(
                "SELECT * FROM conversation_issues WHERE issue_id=?", (issue_id,)
            ).fetchone()
            if not issue or issue["state"] != "open" or issue["version"] != version:
                return False
            # A callback received during planning must be classified before this
            # issue is acknowledged. Only a nearby, unassigned burst fences it.
            since = (
                datetime.fromisoformat(issue["latest_customer_at"]) - timedelta(seconds=120)
            ).isoformat()
            pending = conn.execute(
                "SELECT 1 FROM conversation_tasks task LEFT JOIN conversation_issue_tasks link "
                "ON link.task_id=task.task_id WHERE link.task_id IS NULL "
                "AND task.chat_id=? AND task.customer_id=? AND task.state='waiting_quiet' "
                "AND task.latest_customer_at >= ? LIMIT 1",
                (issue["chat_id"], issue["customer_id"], since),
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
