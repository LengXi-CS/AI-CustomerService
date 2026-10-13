"""Read-only replay of the existing chat archive and locally retained media."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, date, datetime, timedelta, timezone
from datetime import time as day_time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fastgpt_assistant.core.config import Settings

MAX_ID = 2**63 - 1
ROLE = Literal["customer", "staff", "assistant"]
KIND = Literal["text", "image", "file", "voice", "video", "other"]
KINDS = {"text", "image", "file", "voice", "video", "other"}
ROLES = {"customer", "staff", "assistant"}
LABELS = {
    "text": "文字",
    "image": "图片",
    "file": "文件或卡片",
    "voice": "语音",
    "video": "视频",
    "other": "其他",
}
XML_MARKER = re.compile(r"<(?:\?xml|msg\b|img\b|appmsg\b|voicemsg\b|videomsg\b|filemsg\b)", re.I)
BEIJING = timezone(timedelta(hours=8))


class ChatArchiveUnavailable(Exception):
    """Never echo SQL diagnostics, physical paths or customer text."""


class ChatRoomsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(default="", max_length=128)
    before_id: int | None = Field(default=None, ge=1, le=MAX_ID)
    snapshot_id: int | None = Field(default=None, ge=0, le=MAX_ID)
    limit: int = Field(default=20, ge=1, le=50)


class ChatMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: str = Field(max_length=128, pattern=r"^[^\s@]+@chatroom$")
    query: str = Field(default="", max_length=200)
    role: ROLE | None = None
    kind: KIND | None = None
    from_date: date | None = None
    to_date: date | None = None
    before_id: int | None = Field(default=None, ge=1, le=MAX_ID)
    after_id: int | None = Field(default=None, ge=0, le=MAX_ID)
    snapshot_id: int | None = Field(default=None, ge=0, le=MAX_ID)
    limit: int = Field(default=50, ge=1, le=100)

    @model_validator(mode="after")
    def valid_window(self):
        if self.before_id is not None and self.after_id is not None:
            raise ValueError("不能同时向前和向后翻页")
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("开始日期不能晚于结束日期")
        if self.to_date == date.max or self.from_date == date.min:
            raise ValueError("日期超出可查询范围")
        return self


def iso_time(value) -> str | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def like_pattern(query: str) -> str:
    return "%" + query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def public_body(text: str, kind: str) -> tuple[str, str]:
    """Render message envelopes as cards, rather than exposing CDN/AES XML."""
    marker = XML_MARKER.search(text)
    if not marker or kind == "text":
        return text, ""
    title = ""
    xml = text[marker.start() :]
    if len(xml) <= 65536 and "<!DOCTYPE" not in xml.upper() and "<!ENTITY" not in xml.upper():
        try:
            root = ET.fromstring(xml)
            node = root if root.tag == "appmsg" else root.find(".//appmsg")
            if node is not None:
                title = (node.findtext("title") or "").strip()[:1000]
                description = (node.findtext("des") or "").strip()[:1000]
                return "\n".join(part for part in (title, description) if part), title
        except (ET.ParseError, ValueError):
            pass
    return "", title


def public_quote(text: str, kind: str) -> str:
    marker = XML_MARKER.search(text)
    if not marker:
        return text
    types = {
        "3": "image",
        "image": "image",
        "49": "file",
        "file": "file",
        "34": "voice",
        "voice": "voice",
        "43": "video",
        "video": "video",
    }
    quoted_kind = types.get(kind, "other")
    body, _ = public_body(text[marker.start() :], quoted_kind)
    return "\n".join(
        part
        for part in (text[: marker.start()].strip(), body or f"[{LABELS[quoted_kind]}消息]")
        if part
    )


def image_mime(path: Path) -> str | None:
    with path.open("rb") as handle:
        header = handle.read(16)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"BM"):
        return "image/bmp"
    return None


def safe_name(value: str, fallback: str = "历史附件") -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(char for char in name if ord(char) >= 32 and ord(char) != 127)[:180] or fallback


ROOMS_CTE = """
WITH rooms AS (
  SELECT chat_id, MAX(id) AS last_id, COUNT(*) AS message_count
  FROM chat_messages WHERE id <= :snapshot AND chat_id LIKE '%@chatroom' GROUP BY chat_id
), named AS (
  SELECT rooms.*, COALESCE((SELECT chat_name FROM chat_messages names
    WHERE names.chat_id=rooms.chat_id AND names.id <= :snapshot AND names.chat_name != ''
    ORDER BY names.id DESC LIMIT 1), '') AS chat_name FROM rooms
)
"""


class ChatArchiveReader:
    def __init__(self, settings: Settings | None) -> None:
        self.settings = settings

    @staticmethod
    def _connect(path: Path, *, timeout: float = 1.0) -> sqlite3.Connection:
        connection = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 3
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        return connection

    @contextmanager
    def _archive(self):
        if self.settings is None:
            raise ChatArchiveUnavailable("服务尚未提供聊天归档配置，请重启服务后重试。")
        try:
            with closing(self._connect(self.settings.chat_log_db_path)) as connection:
                connection.execute("BEGIN")
                yield connection
        except (OSError, sqlite3.Error) as exc:
            raise ChatArchiveUnavailable(
                "聊天记录库暂不可读取，请检查文件、权限或数据库占用。"
            ) from exc

    def rooms(self, request: ChatRoomsRequest) -> dict:
        with self._archive() as connection:
            snapshot = (
                request.snapshot_id
                if request.snapshot_id is not None
                else connection.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM chat_messages"
                ).fetchone()[0]
            )
            params = {
                "snapshot": snapshot,
                "query": like_pattern(request.query),
                "before": request.before_id,
                "limit": request.limit + 1,
            }
            totals = connection.execute(
                ROOMS_CTE + "SELECT COUNT(*), COALESCE(SUM(message_count),0) FROM rooms", params
            ).fetchone()
            condition = "(chat_id LIKE :query ESCAPE '\\' OR chat_name LIKE :query ESCAPE '\\')"
            matched = connection.execute(
                ROOMS_CTE + "SELECT COUNT(*) FROM named WHERE " + condition, params
            ).fetchone()[0]
            rows = connection.execute(
                ROOMS_CTE
                + """
                SELECT named.*, m.sender_role, m.sender_name, m.kind, m.text, m.occurred_at
                FROM named JOIN chat_messages m ON m.id=named.last_id
                WHERE (named.chat_id LIKE :query ESCAPE '\\'
                  OR named.chat_name LIKE :query ESCAPE '\\')
                AND (:before IS NULL OR named.last_id < :before)
                ORDER BY named.last_id DESC LIMIT :limit
            """,
                params,
            ).fetchall()
            items = []
            for row in rows[: request.limit]:
                kind = row["kind"] if row["kind"] in KINDS else "other"
                body, _ = public_body(str(row["text"] or ""), kind)
                items.append(
                    {
                        "chat_id": row["chat_id"],
                        "chat_name": row["chat_name"],
                        "message_count": row["message_count"],
                        "last_id": row["last_id"],
                        "last_at": iso_time(row["occurred_at"]),
                        "last_kind": kind,
                        "preview": (body or f"[{LABELS[kind]}消息]")[:160],
                        "last_role": row["sender_role"]
                        if row["sender_role"] in ROLES
                        else "unknown",
                    }
                )
            return {
                "items": items,
                "snapshot_id": snapshot,
                "total_rooms": totals[0],
                "total_messages": totals[1],
                "matched_rooms": matched,
                "recording_enabled": self.settings.chat_log_enabled,
                "next_cursor": rows[request.limit - 1]["last_id"]
                if len(rows) > request.limit
                else None,
            }

    def messages(self, request: ChatMessagesRequest) -> dict:
        with self._archive() as connection:
            snapshot = (
                request.snapshot_id
                if request.snapshot_id is not None
                else connection.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM chat_messages"
                ).fetchone()[0]
            )
            conditions, params = ["chat_id=?", "id<=?"], [request.chat_id, snapshot]
            if request.query.strip():
                conditions.append(
                    "(text LIKE ? ESCAPE '\\' OR quoted_text LIKE ? ESCAPE '\\' "
                    "OR media_name LIKE ? ESCAPE '\\')"
                )
                params.extend([like_pattern(request.query)] * 3)
            if request.role:
                conditions.append("sender_role=?")
                params.append(request.role)
            if request.kind:
                conditions.append("kind=?")
                params.append(request.kind)
            for operator, value in ((">=", request.from_date), ("<", request.to_date)):
                if value:
                    day = value if operator == ">=" else value + timedelta(days=1)
                    boundary = (
                        datetime.combine(day, day_time(), BEIJING).astimezone(UTC).isoformat()
                    )
                    conditions.append("occurred_at " + operator + " ?")
                    params.append(boundary)
            where = " AND ".join(conditions)
            total = connection.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE " + where, params
            ).fetchone()[0]
            cursor_condition, cursor_params = "", []
            if request.before_id is not None:
                cursor_condition, cursor_params = " AND id < ?", [request.before_id]
            elif request.after_id is not None:
                cursor_condition, cursor_params = " AND id > ?", [request.after_id]
            forward = request.after_id is not None
            rows = connection.execute(
                "SELECT * FROM chat_messages WHERE "
                + where
                + cursor_condition
                + (" ORDER BY id ASC" if forward else " ORDER BY id DESC")
                + " LIMIT ?",
                [*params, *cursor_params, request.limit + 1],
            ).fetchall()
            page = rows[: request.limit] if forward else list(reversed(rows[: request.limit]))
            has_more = len(rows) > request.limit
            if forward:
                has_older = False
            else:
                oldest = page[0]["id"] if page else 0
                has_older = has_more
                if not has_more and request.before_id is None and oldest:
                    has_older = (
                        connection.execute(
                            "SELECT 1 FROM chat_messages WHERE " + where + " AND id < ? LIMIT 1",
                            [*params, oldest],
                        ).fetchone()
                        is not None
                    )
            with ExitStack() as stack:
                indexes = {}
                items = [self._present(row, indexes, stack) for row in page]
            return {
                "items": items,
                "snapshot_id": snapshot,
                "total_matching": total,
                "older_cursor": page[0]["id"] if page else None,
                "has_older": has_older,
                "newer_cursor": page[-1]["id"] if forward and has_more else snapshot,
                "has_newer": forward and has_more,
            }

    def _optional_index(self, name: str, path: Path, indexes: dict, stack: ExitStack):
        if name not in indexes:
            try:
                indexes[name] = stack.enter_context(closing(self._connect(path, timeout=0.2)))
            except (OSError, sqlite3.Error):
                indexes[name] = None
        return indexes[name]

    def _resolve_media(self, row, indexes: dict, stack: ExitStack):
        if (
            self.settings is None
            or not row["media_ref"]
            or row["kind"] not in KINDS - {"text", "other"}
        ):
            return None, "unrecorded"
        settings = self.settings
        try:
            if row["direction"] == "inbound":
                index = self._optional_index(
                    "inbound", settings.vault_storage_dir / "inbound-media-index.db", indexes, stack
                )
                if index is None:
                    return None, "missing"
                media = index.execute(
                    "SELECT * FROM inbound_media WHERE chat_id=? AND message_id=? "
                    "AND sender_id IN (?, ?) ORDER BY sender_id=? DESC LIMIT 1",
                    (
                        row["chat_id"],
                        row["message_id"],
                        row["sender_id"],
                        row["chat_id"],
                        row["sender_id"],
                    ),
                ).fetchone()
                if media is None:
                    return None, "missing"
                expected = (
                    "inbound_"
                    + hashlib.sha256(
                        "\x1f".join(
                            (row["chat_id"], media["sender_id"], row["message_id"])
                        ).encode()
                    ).hexdigest()
                    + ".bin"
                )
                if media["file_name"] != expected:
                    return None, "missing"
                root = (settings.vault_storage_dir / "inbound-media").resolve()
                path = (root / expected).resolve()
                name = str(row["media_name"] or "")
            else:
                index = self._optional_index("catalog", settings.db_path, indexes, stack)
                if index is None:
                    return None, "unavailable"
                media = index.execute(
                    "SELECT archive_file, status, display_name FROM attachments WHERE media_id=?",
                    (row["media_ref"],),
                ).fetchone()
                if media is None or media["status"] != "available" or not media["archive_file"]:
                    return None, "missing"
                root = settings.media_dir.resolve()
                archive = Path(str(media["archive_file"]))
                candidates = (
                    [archive]
                    if archive.is_absolute()
                    else [
                        settings.db_path.resolve().parent / archive,
                        settings.db_path.resolve().parent.parent / archive,
                        root / archive,
                    ]
                )
                path = None
                for candidate in candidates:
                    resolved = candidate.resolve()
                    if resolved.is_relative_to(root) and resolved.is_file():
                        path = resolved
                        break
                if path is None:
                    return None, "missing"
                name = str(row["media_name"] or media["display_name"] or "")
            if not path.is_relative_to(root) or not path.is_file():
                return None, "missing"
            mime = image_mime(path) if row["kind"] == "image" else None
            extension = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/gif": ".gif",
                "image/webp": ".webp",
                "image/bmp": ".bmp",
            }.get(mime, "")
            return {
                "path": path,
                "name": safe_name(name, "历史图片" + extension if mime else "历史附件"),
                "size_bytes": path.stat().st_size,
                "mime_type": mime or "application/octet-stream",
                "inline_image": mime is not None,
            }, "available"
        except (OSError, sqlite3.Error, ValueError):
            return None, "unavailable"

    def _present(self, row, indexes: dict, stack: ExitStack) -> dict:
        kind = row["kind"] if row["kind"] in KINDS else "other"
        text, title = public_body(str(row["text"] or ""), kind)
        try:
            metadata = json.loads(str(row["metadata"] or "{}"))
        except (ValueError, TypeError):
            metadata = {}
        metadata = metadata if isinstance(metadata, dict) else {}
        mentions = metadata.get("mentions")
        mentions = (
            [item[:128] for item in mentions[:200] if isinstance(item, str)]
            if isinstance(mentions, list)
            else []
        )
        asset, state = self._resolve_media(row, indexes, stack)
        try:
            size = max(0, int(row["media_size"] or 0))
        except (ValueError, TypeError):
            size = 0
        media = {
            "status": state,
            "available": asset is not None,
            "inline_image": False,
            "name": safe_name(str(row["media_name"] or title or "")),
            "size_bytes": size,
            "url": None,
            "download_url": None,
        }
        if asset:
            media.update({key: value for key, value in asset.items() if key != "path"})
            media.update(
                url=f"/api/admin/chats/messages/{row['id']}/media",
                download_url=f"/api/admin/chats/messages/{row['id']}/media?download=true",
            )
        return {
            "id": row["id"],
            "chat_id": row["chat_id"],
            "message_id": row["message_id"],
            "direction": "outbound" if row["direction"] == "outbound" else "inbound",
            "sender_id": row["sender_id"],
            "sender_name": row["sender_name"],
            "sender_role": row["sender_role"] if row["sender_role"] in ROLES else "unknown",
            "kind": kind,
            "text": text,
            "occurred_at": iso_time(row["occurred_at"]),
            "recorded_at": iso_time(row["recorded_at"]),
            "quoted_msg_id": row["quoted_msg_id"],
            "quoted_type": row["quoted_type"],
            "quoted_text": public_quote(
                str(row["quoted_text"] or ""), str(row["quoted_type"] or "")
            ),
            "mentioned_bot": metadata.get("mentioned_bot") is True,
            "mentions": mentions,
            "channel_used": row["channel_used"],
            "task_id": row["task_id"],
            "media": media,
        }

    def media(self, row_id: int) -> dict | None:
        with self._archive() as connection:
            row = connection.execute(
                "SELECT * FROM chat_messages WHERE id=? AND chat_id LIKE '%@chatroom'", (row_id,)
            ).fetchone()
            if row is None:
                return None
            with ExitStack() as stack:
                asset, _ = self._resolve_media(row, {}, stack)
                return asset
