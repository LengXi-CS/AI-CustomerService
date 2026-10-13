"""Polling primitives for quiet-window conversation processing."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from fastgpt_assistant.orchestration.conversation_state import (
    ConversationStateStore,
    ConversationTask,
)

logger = logging.getLogger(__name__)

TaskHandler = Callable[[ConversationTask], Awaitable[None] | None]


class ConversationPoller:
    """Poll durable state and hand claimed quiet tasks to a caller."""

    def __init__(
        self,
        store: ConversationStateStore,
        handler: TaskHandler,
        *,
        quiet_window_seconds: int = 120,
        poll_interval_seconds: int = 30,
        batch_size: int = 20,
        reconciler: Callable | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.store = store
        self.handler = handler
        self.quiet_window_seconds = quiet_window_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.batch_size = batch_size
        self.reconciler = reconciler

    async def poll_once(self, *, now: datetime | str | None = None) -> list[str]:
        """Claim and handle one batch, returning successfully handed-off IDs."""
        require_issue_chats = ()
        if self.reconciler is not None:
            try:
                require_issue_chats = tuple(await asyncio.to_thread(self.reconciler, now=now) or ())
            except Exception:
                logger.exception("Issue reconciliation failed; postponing this poll")
                return []
        tasks = self.store.claim_ready_tasks(
            now=now,
            quiet_window_seconds=self.quiet_window_seconds,
            limit=self.batch_size,
            require_issue_chats=require_issue_chats,
        )
        handled: list[str] = []
        for task in tasks:
            try:
                if inspect.iscoroutinefunction(self.handler):
                    result = self.handler(task)
                    await result
                else:
                    await asyncio.to_thread(self.handler, task)
                handled.append(task.task_id)
            except Exception:
                logger.exception("Conversation task handler failed: task_id=%s", task.task_id)
                issue = self.store.issues.get(task.task_id)
                if issue:
                    self.store.issues.finish(issue["issue_id"], state="waiting_quiet")
                else:
                    self.store.set_state(task.task_id, "waiting_quiet")
        return handled

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run at the configured interval until the application shuts down."""
        while not stop_event.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.poll_interval_seconds)
            except TimeoutError:
                continue
