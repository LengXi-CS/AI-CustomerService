"""Sliding-window context buffer for inbound multi-modal media and chat history."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class InboundMediaItem:
    """An image or file received from WeChat before a text inquiry."""

    msg_id: str
    chat_id: str
    sender_id: str
    kind: str  # "image" | "file"
    local_path: str
    media_id: str
    image_data: bytes | None = None
    mime_type: str = ""
    visual_analysis: dict[str, object] | None = None
    bound_task_ids: set[str] = field(default_factory=set, repr=False)
    timestamp: float = field(default_factory=time.time)


@dataclass(slots=True)
class ChatMessage:
    """A conversational turn in a session."""

    role: str  # "user" | "assistant"
    content: str
    timestamp: float = field(default_factory=time.time)


class ContextBuffer:
    """Thread-safe in-memory sliding window manager."""

    def __init__(
        self,
        media_ttl_seconds: float | None = None,
        history_ttl_seconds: float = 3600.0,
        max_history_turns: int = 10,
        max_media_items: int = 60,
    ) -> None:
        del media_ttl_seconds, history_ttl_seconds
        if max_media_items <= 0:
            raise ValueError("max_media_items must be positive")
        self.max_history_turns = max_history_turns
        self.max_media_items = max_media_items
        self._lock = threading.Lock()
        self._media_buffer: dict[str, list[InboundMediaItem]] = {}
        self._history_buffer: dict[str, list[ChatMessage]] = {}

    def _session_key(self, chat_id: str, sender_id: str, task_id: str | None = None) -> str:
        base = f"{chat_id.strip()}:{sender_id.strip()}"
        return f"{base}:task:{task_id.strip()}" if task_id and task_id.strip() else base

    def push_media(
        self,
        *,
        msg_id: str,
        chat_id: str,
        sender_id: str,
        kind: str,
        local_path: str,
        media_id: str,
        image_data: bytes | None = None,
        mime_type: str = "",
        bound_task_ids: Iterable[str] = (),
    ) -> InboundMediaItem:
        """Store an inbound image/file into the session's sliding window."""
        key = self._session_key(chat_id, sender_id)
        normalized_task_ids = {
            str(task_id).strip() for task_id in bound_task_ids if str(task_id).strip()
        }
        item = InboundMediaItem(
            msg_id=msg_id,
            chat_id=chat_id,
            sender_id=sender_id,
            kind=kind,
            local_path=local_path,
            media_id=media_id,
            image_data=image_data,
            mime_type=mime_type,
            bound_task_ids=normalized_task_ids,
        )
        with self._lock:
            items = self._media_buffer.setdefault(key, [])
            latest_user_message = max(
                (
                    message.timestamp
                    for message in self._history_buffer.get(key, [])
                    if message.role == "user"
                ),
                default=0.0,
            )
            if latest_user_message and any(
                item.timestamp <= latest_user_message for item in items
            ):
                # A new image after a completed text turn starts a new media batch.
                # Consecutive images before the next text turn remain together.
                items = []
            items.append(item)
            self._media_buffer[key] = items[-self.max_media_items :]
        logger.info("Buffered inbound media for session %s: media_id=%s", key, media_id)
        return item

    def get_recent_media(
        self,
        chat_id: str,
        sender_id: str,
        window_seconds: float | None = None,
    ) -> list[InboundMediaItem]:
        """Fetch media items retained in the count-bounded session window."""
        key = self._session_key(chat_id, sender_id)
        with self._lock:
            return list(self._media_buffer.get(key, []))

    def get_media_by_message_id(
        self,
        *,
        chat_id: str,
        sender_id: str,
        message_id: str,
        window_seconds: float | None = None,
    ) -> list[InboundMediaItem]:
        """Find media belonging to one original message in the session window."""
        key = self._session_key(chat_id, sender_id)
        with self._lock:
            return [
                item for item in self._media_buffer.get(key, []) if item.msg_id == message_id
            ]

    def bind_media_by_message_id(
        self,
        *,
        chat_id: str,
        sender_id: str,
        message_id: str,
        task_id: str,
    ) -> int:
        """Bind a quoted original message's media to the current task."""
        normalized_id = task_id.strip()
        if not normalized_id:
            return 0
        key = self._session_key(chat_id, sender_id)
        bound = 0
        with self._lock:
            items = self._media_buffer.get(key, [])
            for item in items:
                if item.msg_id == message_id and normalized_id not in item.bound_task_ids:
                    item.bound_task_ids.add(normalized_id)
                    bound += 1
        return bound

    def bind_pending_media(
        self,
        *,
        chat_id: str,
        sender_id: str,
        task_ids: Iterable[str],
    ) -> int:
        """Bind the current unassigned media batch to one or more tasks.

        Images arrive before the text that describes them.  Binding at task
        creation time makes the later quiet-window worker deterministic: the
        second task cannot consume the first task's image merely because both
        belong to the same WeChat group.
        """

        normalized_ids = {str(task_id).strip() for task_id in task_ids if str(task_id).strip()}
        if not normalized_ids:
            return 0
        key = self._session_key(chat_id, sender_id)
        bound = 0
        with self._lock:
            items = self._media_buffer.get(key, [])
            for item in items:
                if item.bound_task_ids:
                    continue
                item.bound_task_ids.update(normalized_ids)
                bound += 1
        if bound:
            logger.info(
                "Bound inbound media batch chat=%s tasks=%s count=%d",
                chat_id,
                ",".join(sorted(normalized_ids)),
                bound,
            )
        return bound

    def get_task_media(
        self,
        *,
        chat_id: str,
        sender_id: str,
        task_id: str,
        window_seconds: float | None = None,
    ) -> list[InboundMediaItem]:
        """Return only media explicitly bound to one task."""

        key = self._session_key(chat_id, sender_id)
        with self._lock:
            return [
                item
                for item in self._media_buffer.get(key, [])
                if task_id in item.bound_task_ids
            ]

    def clear_task_media(self, task_id: str) -> None:
        """Retain historical bindings after a task reaches a terminal state.

        Media remains in the count-bounded group window so an explicit quote can
        rehydrate it later. Removing the binding here would incorrectly turn an
        old image into the next task's unassigned pending batch; keeping the task
        id makes ordinary ``bind_pending_media`` skip it while
        ``bind_media_by_message_id`` can still attach a quoted image explicitly.
        """

        del task_id

    def set_visual_analysis(
        self,
        *,
        chat_id: str,
        sender_id: str,
        msg_id: str,
        analysis: dict[str, object],
    ) -> bool:
        """Attach a sanitized model result to a still-live media item."""
        key = self._session_key(chat_id, sender_id)
        with self._lock:
            for item in self._media_buffer.get(key, []):
                if item.msg_id == msg_id:
                    item.visual_analysis = dict(analysis)
                    return True
        return False

    def push_message(
        self,
        chat_id: str,
        sender_id: str,
        role: str,
        content: str,
        task_id: str | None = None,
    ) -> None:
        """Append a conversational turn to the session history."""
        key = self._session_key(chat_id, sender_id, task_id)
        msg = ChatMessage(role=role, content=content)
        with self._lock:
            history = self._history_buffer.setdefault(key, [])
            history.append(msg)
            self._history_buffer[key] = history[-self.max_history_turns :]

    def get_history(
        self,
        chat_id: str,
        sender_id: str,
        limit: int = 6,
        task_id: str | None = None,
    ) -> list[dict[str, str]]:
        """Get formatted chat history suitable for LLM prompts."""
        key = self._session_key(chat_id, sender_id, task_id)
        with self._lock:
            history = self._history_buffer.get(key, [])
            self._history_buffer[key] = history[-self.max_history_turns :]
            selected = history[-limit:]
            return [{"role": m.role, "content": m.content} for m in selected]

    def clear(self, chat_id: str, sender_id: str, task_id: str | None = None) -> None:
        """Clear state for a session."""
        key = self._session_key(chat_id, sender_id, task_id)
        with self._lock:
            self._media_buffer.pop(key, None)
            self._history_buffer.pop(key, None)
