"""Read-only runtime observations; never retain log messages or customer content."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from fastgpt_assistant.core.config import Settings
from fastgpt_assistant.core.connection_config import ConnectionConfigStore
from fastgpt_assistant.core.dotenv_store import InvalidEnvFile
from fastgpt_assistant.core.reply_policy_store import ReplyPolicyStore

TASK_STATES = (
    "waiting_quiet", "processing", "human_active", "responded", "closed", "blocked", "other"
)

# Only constant summaries enter the buffer. Neither formatting a LogRecord nor
# parsing old log files is safe: both can contain keys, customer text and URLs.
ERROR_SUMMARIES = (
    ("Conversation poller stopped unexpectedly", "任务调度", "任务调度器异常停止，请检查服务日志"),
    ("Conversation task handler failed:", "任务调度", "任务处理异常，调度器尝试退回等待状态"),
    ("Delayed conversation task failed:", "需求处理", "延迟需求处理异常，处理器尝试退回等待状态"),
    ("Payload vault key is unavailable", "内容存储", "内容保险库密钥不可用，延迟回复已停用"),
    ("Unable to restore delayed conversation", "内容存储", "延迟任务内容恢复失败"),
    ("Unable to persist delayed group event", "任务存储", "延迟任务元数据保存失败"),
    ("Planner unavailable for chat=", "需求规划", "需求规划服务不可用"),
    ("Image understanding failed", "图片分析", "图片分析失败"),
    ("Failed to send text to", "微信投递", "文字消息投递失败"),
    ("Failed to send attachment", "微信投递", "附件投递失败"),
    ("Owner reminder failed", "微信投递", "负责人提醒投递失败"),
    ("Soothe reply composition failed", "安抚回复", "安抚话术生成失败，使用备用文案"),
)


def normalized_timestamp(value: str | None) -> str | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
        if parsed is None:
            return None
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def task_statistics(path: Path) -> dict:
    """Open an existing database in read-only mode; do not initialise a store."""
    unavailable = {"available": False, "counts": None, "total": None, "last_updated": None}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=0.25)) as connection:
            # One SELECT ensures the counts and latest update share a snapshot.
            rows = connection.execute(
                "SELECT state, COUNT(*), MAX(updated_at) FROM conversation_tasks GROUP BY state"
            ).fetchall()
        counts = dict.fromkeys(TASK_STATES, 0)
        timestamps = []
        for state, count, updated in rows:
            counts[state if state in counts else "other"] += count
            if timestamp := normalized_timestamp(updated):
                timestamps.append(timestamp)
        return {
            "available": True, "counts": counts, "total": sum(counts.values()),
            "last_updated": max(timestamps, default=None),
        }
    except (OSError, sqlite3.Error, ValueError):
        return unavailable


def database_readable(path: Path) -> bool:
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=0.25)) as connection:
            return connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
            ).fetchone() is not None
    except (OSError, sqlite3.Error, ValueError):
        return False


def pending_configuration(reply: ReplyPolicyStore, connections: ConnectionConfigStore) -> dict:
    """Report only scope/count, with both scopes reading the same file revision."""
    scopes = []
    with reply.document.lock:
        try:
            content = reply.document.read()
        except OSError:
            content = None
        for name, store in (("reply", reply), ("connections", connections)):
            try:
                if content is None:
                    raise InvalidEnvFile("配置无法读取")
                snapshot = store._snapshot(content)
                fields = [*snapshot["fields"].values(), *snapshot.get("secrets", {}).values()]
                count = sum(bool(field["pending_restart"]) for field in fields)
                scopes.append({"page": name, "available": True, "count": count})
            except InvalidEnvFile:
                scopes.append({"page": name, "available": False, "count": None})
    return {
        "available": all(scope["available"] for scope in scopes),
        "count": sum(scope["count"] or 0 for scope in scopes)
        if all(scope["available"] for scope in scopes) else None,
        "scopes": scopes,
    }


class RuntimeStatus(logging.Handler):
    """Bounded, process-local error buffer attached only during service lifespan."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self._guard = threading.Lock()
        self._events: deque[dict] = deque(maxlen=50)
        self._total_errors = 0
        self.started_at: str | None = None
        self._started_clock: float | None = None
        self._settings: Settings | None = None
        self._poller: asyncio.Task | None = None
        self._phase = "starting"

    def start(self, settings: Settings) -> None:
        with self._guard:
            self._events.clear()
            self._total_errors = 0
            self.started_at = datetime.now(UTC).isoformat()
            self._started_clock = time.monotonic()
            self._settings = settings
            self._poller = None
            self._phase = "starting"
        for name in ("fastgpt_assistant", "uvicorn.error"):
            logging.getLogger(name).addHandler(self)

    def ready(self, poller: asyncio.Task) -> None:
        self._poller = poller
        self._phase = "running"
        poller.add_done_callback(self._poller_completed)

    @staticmethod
    def _poller_completed(poller: asyncio.Task) -> None:
        if not poller.cancelled() and poller.exception() is not None:
            logging.getLogger("fastgpt_assistant").error("Conversation poller stopped unexpectedly")

    def stop(self) -> None:
        self._phase = "stopped"
        for name in ("fastgpt_assistant", "uvicorn.error"):
            logging.getLogger(name).removeHandler(self)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR:
            return
        category, summary = "客服服务", "服务记录了异常，请在服务器本机查看对应时间的日志"
        if record.name.startswith("uvicorn"):
            category, summary = "HTTP 服务", "HTTP 请求处理异常"
        elif isinstance(record.msg, str):
            for prefix, known_category, known_summary in ERROR_SUMMARIES:
                if record.msg.startswith(prefix):
                    category, summary = known_category, known_summary
                    break
        with self._guard:
            self._total_errors += 1
            self._events.append({
                "id": self._total_errors,
                "occurred_at": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "level": "critical" if record.levelno >= logging.CRITICAL else "error",
                "category": category, "summary": summary,
            })

    def snapshot(self) -> dict:
        settings = self._settings
        tasks = task_statistics(settings.conversation_state_db_path) if settings else {
            "available": False, "counts": None, "total": None, "last_updated": None
        }
        product_db = database_readable(settings.db_path) if settings else False
        poller_running = self._poller is not None and not self._poller.done()
        poller_status = "running" if poller_running else "stopped" if self._poller else "starting"
        status = self._phase
        if status == "running":
            healthy = poller_running and tasks["available"] and product_db
            status = "healthy" if healthy else "degraded"
        with self._guard:
            errors = {
                "total": self._total_errors,
                "items": [dict(event) for event in reversed(self._events)],
                "limit": self._events.maxlen,
                "since": self.started_at,
            }
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "service": {
                "status": status, "started_at": self.started_at,
                "uptime_seconds": max(0, int(time.monotonic() - self._started_clock))
                if self._started_clock is not None else None,
                "poller": poller_status, "product_database_available": product_db,
            },
            "tasks": tasks, "errors": errors,
        }
