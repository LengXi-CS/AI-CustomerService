"""Bounded durable storage for inbound WeChat media handles.

The normal conversation state stores only opaque identifiers.  This store keeps
the corresponding bytes in a controlled directory so a later WeChat quote can
resolve the original image by message ID, including after the API process has
restarted.  Retention is controlled by the caller's message window, never by
wall-clock expiry.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StoredInboundMedia:
    media_id: str
    message_id: str
    chat_id: str
    sender_id: str
    kind: str
    mime_type: str
    path: Path


class InboundMediaStore:
    """Persist bounded inbound media by its opaque platform message ID."""

    def __init__(
        self,
        root: str | Path,
        index_database: str | Path,
        *,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_database = Path(index_database).resolve()
        self.index_database.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.index_database)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inbound_media (
                    media_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    UNIQUE(chat_id, sender_id, message_id)
                )
                """
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_inbound_media_lookup
                   ON inbound_media(chat_id, sender_id, message_id, expires_at)"""
            )
    def _file_name(self, *, chat_id: str, sender_id: str, message_id: str) -> str:
        digest = hashlib.sha256(
            "\x1f".join((chat_id, sender_id, message_id)).encode("utf-8")
        ).hexdigest()
        return f"inbound_{digest}.bin"

    def _from_row(self, row: sqlite3.Row) -> StoredInboundMedia:
        path = self.root / str(row["file_name"])
        if path.parent != self.root:
            raise ValueError("inbound media path escaped store root")
        return StoredInboundMedia(
            media_id=str(row["media_id"]),
            message_id=str(row["message_id"]),
            chat_id=str(row["chat_id"]),
            sender_id=str(row["sender_id"]),
            kind=str(row["kind"]),
            mime_type=str(row["mime_type"]),
            path=path,
        )

    def stage(
        self,
        *,
        media_id: str,
        message_id: str,
        chat_id: str,
        sender_id: str,
        kind: str,
        mime_type: str,
        content: bytes,
    ) -> StoredInboundMedia:
        """Store one bounded media body and index it by the original message ID."""
        fields = (media_id, message_id, chat_id, sender_id, kind, mime_type)
        if any(not isinstance(value, str) or not value.strip() for value in fields):
            raise ValueError("inbound media identifiers and type fields are required")
        if not isinstance(content, bytes) or not content or len(content) > self.max_bytes:
            raise ValueError("inbound media body is empty or too large")
        file_name = self._file_name(
            chat_id=chat_id.strip(), sender_id=sender_id.strip(), message_id=message_id.strip()
        )
        path = self.root / file_name
        temporary = self.root / f".{file_name}.tmp"
        temporary.write_bytes(content)
        temporary.replace(path)
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """INSERT INTO inbound_media(
                           media_id, message_id, chat_id, sender_id, kind,
                           mime_type, file_name, expires_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(chat_id, sender_id, message_id) DO UPDATE SET
                           media_id=excluded.media_id,
                           kind=excluded.kind,
                           mime_type=excluded.mime_type,
                           file_name=excluded.file_name,
                           expires_at=excluded.expires_at""",
                    (
                        media_id.strip(),
                        message_id.strip(),
                        chat_id.strip(),
                        sender_id.strip(),
                        kind.strip(),
                        mime_type.strip(),
                        file_name,
                        0.0,
                    ),
                )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return StoredInboundMedia(
            media_id=media_id.strip(),
            message_id=message_id.strip(),
            chat_id=chat_id.strip(),
            sender_id=sender_id.strip(),
            kind=kind.strip(),
            mime_type=mime_type.strip(),
            path=path,
        )

    def find_by_message(
        self,
        *,
        chat_id: str,
        sender_id: str,
        message_id: str,
    ) -> StoredInboundMedia | None:
        """Find media for an original platform message in the message window."""
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """SELECT * FROM inbound_media
                   WHERE chat_id = ? AND sender_id = ? AND message_id = ?""",
                (chat_id, sender_id, message_id),
            ).fetchone()
        if row is None:
            return None
        media = self._from_row(row)
        return media if media.path.is_file() else None

    def read(self, media: StoredInboundMedia) -> bytes:
        """Read a previously indexed media body after checking its safe path."""
        if media.path.parent != self.root:
            raise FileNotFoundError(media.media_id)
        content = media.path.read_bytes()
        if len(content) > self.max_bytes:
            raise ValueError("stored inbound media is too large")
        return content

    def retain_message_ids(self, *, chat_id: str, message_ids: set[str]) -> int:
        """Keep only media whose source messages remain in a chat's window."""
        normalized_ids = {str(item).strip() for item in message_ids if str(item).strip()}
        removed = 0
        with closing(self._connect()) as connection, connection:
            if normalized_ids:
                placeholders = ",".join("?" for _ in normalized_ids)
                rows = connection.execute(
                    f"SELECT file_name FROM inbound_media "
                    f"WHERE chat_id = ? AND message_id NOT IN ({placeholders})",
                    (chat_id, *sorted(normalized_ids)),
                ).fetchall()
                connection.execute(
                    f"DELETE FROM inbound_media WHERE chat_id = ? "
                    f"AND message_id NOT IN ({placeholders})",
                    (chat_id, *sorted(normalized_ids)),
                )
            else:
                rows = connection.execute(
                    "SELECT file_name FROM inbound_media WHERE chat_id = ?", (chat_id,)
                ).fetchall()
                connection.execute(
                    "DELETE FROM inbound_media WHERE chat_id = ?", (chat_id,)
                )
            removed = len(rows)
        for row in rows:
            file_name = str(row["file_name"])
            path = self.root / file_name
            if path.parent == self.root:
                path.unlink(missing_ok=True)
        return removed
