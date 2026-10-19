"""Runtime dashboard reports observations without executing customer-service work."""

import asyncio
import json
import logging
import sqlite3
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastgpt_assistant.core.admin_auth import configure_admin
from fastgpt_assistant.core.config import Settings
from fastgpt_assistant.core.connection_config import ConnectionConfigStore
from fastgpt_assistant.core.reply_policy_store import ReplyPolicyStore
from fastgpt_assistant.core.runtime_status import (
    RuntimeStatus,
    database_readable,
    pending_configuration,
    task_statistics,
)
from fastgpt_assistant.server.admin import install_admin


def make_tasks(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE conversation_tasks(state TEXT, updated_at TEXT)")
        connection.executemany(
            "INSERT INTO conversation_tasks VALUES (?, ?)",
            [("waiting_quiet", "2026-10-12T01:00:00+00:00"),
             ("waiting_quiet", "2026-10-12T02:00:00+00:00"),
             ("processing", "2026-10-12T03:00:00+00:00"),
             ("human_active", "2026-10-12T04:00:00+00:00"),
             ("responded", "2026-10-12T05:00:00+00:00"),
             ("closed", "2026-10-12T06:00:00+00:00"),
             ("blocked", "2026-10-12T07:00:00+00:00"),
             ("private-user-text", "private-api-key")],
        )
        connection.commit()


def test_task_statistics_is_read_only_and_hides_unrecognised_metadata(tmp_path):
    path = tmp_path / "中文 #tasks.db"
    make_tasks(path)
    before = path.read_bytes()
    result = task_statistics(path)
    assert result["available"] is True
    assert result["total"] == 8
    assert result["counts"]["waiting_quiet"] == 2
    assert result["counts"]["other"] == 1
    assert result["last_updated"] == "2026-10-12T07:00:00+00:00"
    assert "private" not in json.dumps(result)
    assert path.read_bytes() == before


@pytest.mark.parametrize("content", [None, b"not sqlite", b""])
def test_missing_or_invalid_database_reports_unknown_and_never_creates_it(tmp_path, content):
    path = tmp_path / "missing.db"
    if content is not None:
        path.write_bytes(content)
    result = task_statistics(path)
    assert result["available"] is False
    assert result["counts"] is None and result["total"] is None
    assert database_readable(path) is False
    if content is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == content


def test_locked_database_is_unavailable_not_zero(tmp_path):
    path = tmp_path / "locked.db"
    make_tasks(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("BEGIN EXCLUSIVE")
        assert task_statistics(path)["counts"] is None
        connection.rollback()


def stores(tmp_path):
    settings = Settings(_env_file=None)
    reply = ReplyPolicyStore(tmp_path / ".env", tmp_path / "backups",
                             runtime_settings=settings, environ={})
    connections = ConnectionConfigStore(reply.document, runtime_settings=settings, environ={})
    return reply, connections


def test_pending_counts_include_secret_change_but_no_values(tmp_path):
    reply, connections = stores(tmp_path)
    reply.path.write_text(
        'CONVERSATION_QUIET_WINDOW_SECONDS=17\nLLM_MODEL="private-model-name"\n'
        'LLM_API_KEY="private-key-value"\n', encoding="utf-8"
    )
    result = pending_configuration(reply, connections)
    assert result == {"available": True, "count": 3, "scopes": [
        {"page": "reply", "available": True, "count": 1},
        {"page": "connections", "available": True, "count": 2},
    ]}
    assert "private" not in json.dumps(result)
    assert not (tmp_path / "backups").exists()


def test_invalid_configuration_retains_other_scope_and_reports_total_unknown(tmp_path):
    reply, connections = stores(tmp_path)
    reply.path.write_text("CONVERSATION_QUIET_WINDOW_SECONDS=-1\nLLM_MODEL=changed\n")
    result = pending_configuration(reply, connections)
    assert result["available"] is False and result["count"] is None
    assert result["scopes"][0]["available"] is False
    assert result["scopes"][1]["count"] == 1


def test_unreadable_configuration_is_unknown_without_echoing_exception(tmp_path, monkeypatch):
    reply, connections = stores(tmp_path)

    def denied():
        raise PermissionError("private-path-and-key")

    monkeypatch.setattr(reply.document, "read", denied)
    result = pending_configuration(reply, connections)
    assert result["available"] is False and result["count"] is None
    assert "private" not in json.dumps(result)


def test_error_buffer_never_formats_records_or_exceptions_and_is_bounded():
    monitor = RuntimeStatus()

    class CannotFormat:
        def __str__(self):
            raise AssertionError("must never format log arguments")

    for _ in range(60):
        record = logging.LogRecord("fastgpt_assistant.orchestration.engine", logging.ERROR,
                                   "private-filename", 1, "Failed to send text to %s: %s",
                                   (CannotFormat(), "private-message-and-key"),
                                   (ValueError, ValueError("private-exception"), None))
        monitor.handle(record)
    monitor.handle(logging.LogRecord("fastgpt_assistant", logging.WARNING, "", 1,
                                     "private-warning", (), None))
    result = monitor.snapshot()["errors"]
    assert result["total"] == 60 and len(result["items"]) == 50
    assert result["items"][0]["id"] == 60 and result["items"][-1]["id"] == 11
    assert result["items"][0]["summary"] == "文字消息投递失败"
    assert "private" not in json.dumps(result)
    # API callers cannot mutate the in-memory history.
    result["items"][0]["summary"] = "changed"
    assert monitor.snapshot()["errors"]["items"][0]["summary"] == "文字消息投递失败"


def test_error_buffer_unknown_message_and_logger_name_are_not_returned():
    monitor = RuntimeStatus()
    monitor.handle(logging.LogRecord("private-logger-key", logging.CRITICAL, "", 1,
                                     "private-customer-message", (), None))
    monitor.handle(logging.LogRecord("uvicorn.error", logging.ERROR, "", 1,
                                     "private-request-url", (), None))
    result = monitor.snapshot()["errors"]
    assert result["items"][0]["category"] == "HTTP 服务"
    assert result["items"][1]["level"] == "critical"
    assert "private" not in json.dumps(result)


def test_start_stop_attaches_once_and_collects_only_during_lifespan(tmp_path):
    monitor = RuntimeStatus()
    settings = Settings(_env_file=None, conversation_state_db_path=tmp_path / "tasks.db",
                        db_path=tmp_path / "products.db")
    try:
        monitor.start(settings)
        for name in ("fastgpt_assistant", "uvicorn.error"):
            logger = logging.getLogger(name)
            assert logger.handlers.count(monitor) == 1
            logger.error("private-message")
        assert monitor.snapshot()["errors"]["total"] == 2
        assert monitor.snapshot()["service"]["started_at"] is not None
    finally:
        monitor.stop()
    logging.getLogger("fastgpt_assistant").error("private-after-stop")
    assert monitor.snapshot()["errors"]["total"] == 2
    for name in ("fastgpt_assistant", "uvicorn.error"):
        assert monitor not in logging.getLogger(name).handlers
    monitor.start(settings)
    try:
        assert monitor.snapshot()["errors"]["total"] == 0
    finally:
        monitor.stop()


@pytest.mark.asyncio
async def test_finished_poller_degrades_status_and_records_safe_summary(tmp_path):
    path = tmp_path / "tasks.db"
    make_tasks(path)
    settings = Settings(_env_file=None, conversation_state_db_path=path, db_path=path)
    monitor = RuntimeStatus()
    monitor.start(settings)

    async def fail():
        await asyncio.sleep(0)
        raise RuntimeError("private-failure")

    task = asyncio.create_task(fail())
    monitor.ready(task)
    try:
        assert monitor.snapshot()["service"]["status"] == "healthy"
        with pytest.raises(RuntimeError):
            await task
        result = monitor.snapshot()
        assert result["service"]["status"] == "degraded"
        assert result["service"]["poller"] == "stopped"
        assert result["errors"]["items"][0]["category"] == "任务调度"
        assert "private" not in json.dumps(result)
    finally:
        monitor.stop()


def test_status_requires_login_and_refresh_does_not_write_or_dispatch(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    configure_admin(auth, "test-password-123")
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=private-key\n")
    db = tmp_path / "tasks.db"
    make_tasks(db)
    app = FastAPI()
    install_admin(app, groups_path=tmp_path / "absent.toml", auth_path=auth,
                  env_path=env, backup_dir=tmp_path / "backups")
    settings = Settings(_env_file=None, db_path=db, conversation_state_db_path=db)
    app.state.runtime_status.start(settings)
    docker_calls = []
    aidnet_calls = []

    def docker_snapshot():
        docker_calls.append(True)
        return {"status": "degraded", "engine": "unavailable"}

    monkeypatch.setattr(app.state.docker_status, "snapshot", docker_snapshot)

    def aidnet_snapshot():
        aidnet_calls.append(True)
        return {"status": "healthy", "app": "running", "frpc": "running"}

    monkeypatch.setattr(app.state.aidnet_status, "snapshot", aidnet_snapshot)
    try:
        client = TestClient(app)
        assert client.get("/api/admin/status").status_code == 401
        assert docker_calls == []
        assert aidnet_calls == []
        client.post("/api/admin/login", json={"password": "test-password-123"})
        before = {file.name: file.read_bytes() for file in tmp_path.iterdir() if file.is_file()}
        for _ in range(3):
            response = client.get("/api/admin/status")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            payload = response.json()
            assert payload["tasks"]["total"] == 8
            assert payload["docker"] == {"status": "degraded", "engine": "unavailable"}
            assert payload["aidnet"]["app"] == "running"
            assert "private" not in response.text
        after = {file.name: file.read_bytes() for file in tmp_path.iterdir() if file.is_file()}
        assert after == before
        assert len(docker_calls) == 3
        assert len(aidnet_calls) == 3
        assert client.post("/api/admin/status").status_code == 405
    finally:
        app.state.runtime_status.stop()


@pytest.mark.asyncio
async def test_real_application_lifespan_wires_monitor_and_cleans_handlers(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastgpt_assistant.server import app as module

    db = tmp_path / "tasks.db"
    make_tasks(db)
    settings = Settings(_env_file=None, db_path=db, conversation_state_db_path=db)

    class IdlePoller:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, stop):
            await stop.wait()

    engine = SimpleNamespace(
        settings=settings,
        conversation_state=SimpleNamespace(
            issues=SimpleNamespace(recover_interrupted=lambda: None)
        ),
        reconcile_customer_issues=lambda **kwargs: [],
        recover_pending_customer_events=lambda: None,
        conversation_quiet_windows=lambda: {},
        process_conversation_task=lambda _: pytest.fail("must not dispatch"),
    )
    monkeypatch.setattr(module, "settings", settings)
    monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setattr(module, "ConversationPoller", IdlePoller)
    monitor = RuntimeStatus()
    monkeypatch.setattr(module.app.state, "runtime_status", monitor)
    async with module.lifespan(module.app):
        payload = monitor.snapshot()
        assert payload["service"]["status"] == "healthy"
        assert payload["tasks"]["total"] == 8
        assert monitor in logging.getLogger("fastgpt_assistant").handlers
    assert module._poller_task is None and module._poller_stop is None
    assert monitor not in logging.getLogger("fastgpt_assistant").handlers


@pytest.mark.asyncio
async def test_startup_failure_still_detaches_monitor(tmp_path, monkeypatch):
    from fastgpt_assistant.server import app as module

    def fail():
        raise RuntimeError("startup-failed")

    monitor = RuntimeStatus()
    monkeypatch.setattr(module.app.state, "runtime_status", monitor)
    monkeypatch.setattr(module, "get_engine", fail)
    with pytest.raises(RuntimeError, match="startup-failed"):
        async with module.lifespan(module.app):
            pytest.fail("startup must fail")
    assert monitor not in logging.getLogger("fastgpt_assistant").handlers
    assert monitor not in logging.getLogger("uvicorn.error").handlers
