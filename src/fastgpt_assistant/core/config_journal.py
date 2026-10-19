"""Durable change metadata without configuration values or credentials."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

FIELD_LABELS = {
    "global.enabled": "全局群处理",
    "global.soothe_mode": "安抚话术来源",
    "global.soothe_fixed_reply": "固定安抚文案",
    "groups.added": "新增客户群",
    "groups.removed": "移除客户群",
    "groups.order": "客户群顺序",
    "groups.group_name": "群名称",
    "groups.mode": "群运行模式",
    "groups.owner_id": "群负责人",
    "groups.customer_service_ids": "群客服名单",
    "groups.bot_aliases": "机器人昵称",
    "groups.require_customer_mention": "客户必须 @",
    "groups.allow_images": "允许图片",
    "groups.allow_files": "允许文件",
    "groups.reply_to_staff_when_mentioned": "回复客服 @",
    "groups.native_quote_replies": "引用原问题回复",
    "groups.associate_customer_demands": "同一事项只回复一次",
    "groups.conversation_quiet_window_seconds": "群客户发言后等待时间",
    "conversation_quiet_window_seconds": "客户停止发言后等待时间",
    "conversation_poll_interval_seconds": "任务扫描间隔",
    "attachment_dispatch_interval_seconds": "附件发送间隔",
    "routing_normal_confidence_threshold": "正常回复阈值",
    "routing_human_confidence_threshold": "人工处理阈值",
    "owner_notify_enabled": "负责人通知",
    "llm_base_url": "模型服务地址",
    "llm_model": "主模型",
    "llm_vision_model": "视觉模型",
    "fastgpt_api_url": "FastGPT 服务地址",
    "fastgpt_dataset_id": "知识库 ID",
    "gewe_api_url": "GeWe 服务地址",
    "gewe_app_id": "GeWe 实例 ID",
    "lark_app_id": "飞书应用 ID",
    "lark_bitable_app_token": "飞书表格标识",
    "llm_api_key": "模型 API Key",
    "fastgpt_api_key": "FastGPT API Key",
    "gewe_token": "GeWe Token",
    "lark_app_secret": "飞书 App Secret",
    "formatting": "配置格式",
}
BACKUP_PATTERN = r"[0-9]{8}T[0-9]{12}Z-[a-f0-9]{12}\.(?:env|toml)"
SCOPES = {"groups", "soothe", "reply", "connections", "environment"}
logger = logging.getLogger(__name__)


class HistoryUnavailable(Exception):
    """Generic errors only; SQLite diagnostics can include local paths."""


class ConfigJournal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _update(self, sql: str, args: tuple, *, create: bool = False) -> None:
        try:
            if create:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=2)) as connection:
                if create:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS config_changes ("
                        "id TEXT PRIMARY KEY, created_at TEXT NOT NULL, completed_at TEXT, "
                        "target TEXT NOT NULL, scope TEXT NOT NULL, operation TEXT NOT NULL, "
                        "fields TEXT NOT NULL, before_revision TEXT NOT NULL, "
                        "after_revision TEXT NOT NULL, backup_id TEXT, source_backup_id TEXT, "
                        "status TEXT NOT NULL)"
                    )
                connection.execute(sql, args)
                connection.commit()
        except (OSError, sqlite3.Error) as exc:
            raise HistoryUnavailable("无法写入修改记录，请检查管理目录权限或数据库占用。") from exc

    def commit(
        self,
        *,
        path: Path,
        original: bytes,
        candidate: bytes,
        backup_dir: Path,
        target: str,
        scope: str,
        fields: list[str],
        write,
        operation: str = "save",
        source_backup_id: str | None = None,
    ) -> dict:
        from fastgpt_assistant.core.group_config_store import ConfigConflict, revision_of

        if candidate == original:
            return {"backup_id": None, "history_pending": False}
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = b""
        if current != original:
            raise ConfigConflict("配置已被其他操作修改，请重新加载。")
        if scope not in SCOPES or target not in {"groups", "environment"}:
            raise ValueError("Invalid history scope")
        fields = sorted(set(fields)) or ["formatting"]
        if any(field not in FIELD_LABELS for field in fields):
            raise ValueError("Invalid history field")
        event_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        suffix = "toml" if target == "groups" else "env"
        backup_id = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{revision_of(original)[:12]}.{suffix}"
        self._update(
            "INSERT INTO config_changes VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared')",
            (
                event_id,
                now.isoformat(),
                target,
                scope,
                operation,
                json.dumps(fields),
                revision_of(original),
                revision_of(candidate),
                backup_id,
                source_backup_id,
            ),
            create=True,
        )
        try:
            # Including an empty source allows the first edit to be reverted to
            # managed defaults without deleting the whole configuration file.
            write(backup_dir / backup_id, original)
            write(path, candidate, expected_content=original)
        except Exception:
            self._finish(event_id, "failed")
            raise
        pending = not self._finish(event_id, "applied")
        return {"backup_id": backup_id, "history_pending": pending}

    def _finish(self, event_id: str, status: str) -> bool:
        try:
            self._update(
                "UPDATE config_changes SET status=?, completed_at=? WHERE id=?",
                (status, datetime.now(UTC).isoformat(), event_id),
            )
            return True
        except HistoryUnavailable:
            # The intent already exists durably. Do not tell an API caller that
            # an applied config failed, and do not discard the uncertain record.
            logger.warning("Configuration history completion unavailable")
            return False

    def list(self, *, before: str | None = None, limit: int = 20) -> dict:
        if not self.path.exists():
            return {"items": [], "next_cursor": None}
        try:
            with closing(
                sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
            ) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT * FROM config_changes WHERE (? IS NULL OR rowid < "
                    "(SELECT rowid FROM config_changes WHERE id=?)) ORDER BY rowid DESC LIMIT ?",
                    (before, before, limit + 1),
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise HistoryUnavailable("无法读取修改记录，请检查管理目录权限或数据库占用。") from exc
        items = []
        for row in rows[:limit]:
            try:
                fields = json.loads(row["fields"])
                if (
                    not isinstance(fields, list)
                    or len(fields) > len(FIELD_LABELS)
                    or any(field not in FIELD_LABELS for field in fields)
                    or row["scope"] not in SCOPES
                    or row["target"] not in {"groups", "environment"}
                    or row["operation"] not in {"save", "restore"}
                    or row["status"] not in {"applied", "failed", "prepared"}
                    or not re.fullmatch(r"[a-f0-9]{32}", row["id"])
                ):
                    raise ValueError
                created = datetime.fromisoformat(row["created_at"]).astimezone(UTC).isoformat()
                backup_ids = {}
                for key in ("backup_id", "source_backup_id"):
                    value = row[key]
                    backup_ids[key] = (
                        value if value and re.fullmatch(BACKUP_PATTERN, value) else None
                    )
                items.append(
                    {
                        "id": row["id"],
                        "created_at": created,
                        "target": row["target"],
                        "scope": row["scope"],
                        "operation": row["operation"],
                        "fields": [
                            {"key": field, "label": FIELD_LABELS[field]} for field in fields
                        ],
                        "status": row["status"],
                        **backup_ids,
                    }
                )
            except (ValueError, TypeError):
                raise HistoryUnavailable("修改记录格式无效，请在本机检查记录数据库。") from None
        return {"items": items, "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}
