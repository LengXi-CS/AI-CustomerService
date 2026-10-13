"""Historical chat replay is scoped, private and independent of the reply engine."""

import base64
import json
import sqlite3
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from fastgpt_assistant.core.admin_auth import configure_admin
from fastgpt_assistant.core.chat_archive_reader import (
    ChatArchiveReader,
    ChatArchiveUnavailable,
    ChatMessagesRequest,
    ChatRoomsRequest,
)
from fastgpt_assistant.core.chat_log import ChatLogEntry, ChatLogStore
from fastgpt_assistant.core.config import Settings
from fastgpt_assistant.orchestration.inbound_media_store import InboundMediaStore
from fastgpt_assistant.server.admin import install_admin

ROOM = "demo@chatroom"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6LcAAAAASUVORK5CYII="
)


@pytest.fixture
def archive(tmp_path):
    settings = Settings(_env_file=None)
    # Existing aliased settings ignore lowercase constructor keywords.
    # Assign every storage path explicitly so tests never touch production data.
    settings.chat_log_db_path = tmp_path / "聊天 #记录.db"
    settings.db_path = tmp_path / "catalog.db"
    settings.media_dir = tmp_path / "resources/attachments"
    settings.vault_storage_dir = tmp_path / "vault"
    return settings, ChatLogStore(settings.chat_log_db_path), ChatArchiveReader(settings)


def record(store, **changes):
    values = dict(
        chat_id=ROOM,
        chat_name="演示群",
        direction="inbound",
        sender_id="demo-customer",
        sender_role="customer",
        kind="text",
        text="演示内容",
    )
    values.update(changes)
    assert store.record(ChatLogEntry(**values))


def messages(reader, **values):
    return reader.messages(ChatMessagesRequest(chat_id=ROOM, **values))


@pytest.mark.parametrize("content", [None, b"broken database", b""])
def test_missing_invalid_archive_never_created_or_modified(tmp_path, content):
    path = tmp_path / "absent.db"
    if content is not None:
        path.write_bytes(content)
    reader = ChatArchiveReader(Settings(_env_file=None, chat_log_db_path=path))
    with pytest.raises(ChatArchiveUnavailable) as error:
        reader.rooms(ChatRoomsRequest())
    assert str(path) not in str(error.value)
    assert path.read_bytes() == content if content is not None else not path.exists()
    assert not (tmp_path / "absent.db-wal").exists()


def test_missing_settings_do_not_fall_back_to_production_archive():
    with pytest.raises(ChatArchiveUnavailable):
        ChatArchiveReader(None).rooms(ChatRoomsRequest())


def test_group_names_merged_removed_groups_retained_private_chats_excluded(archive):
    settings, store, reader = archive
    record(store, chat_name="旧名称")
    record(store, chat_name="新名称")
    record(store, chat_name="")
    record(store, chat_id="removed@chatroom", chat_name="历史已移除群")
    record(store, chat_id="private-user", text="private-hidden")
    settings.chat_log_enabled = False
    result = reader.rooms(ChatRoomsRequest())
    assert result["total_messages"] == 4 and result["total_rooms"] == 2
    assert result["recording_enabled"] is False
    assert result["items"][1]["chat_name"] == "新名称"
    assert result["items"][1]["message_count"] == 3
    assert result["items"][0]["chat_id"] == "removed@chatroom"
    assert "private-hidden" not in json.dumps(result)


def test_group_paging_snapshot_remains_stable_with_new_messages(archive):
    _, store, reader = archive
    for index in range(4):
        record(store, chat_id=f"g{index}@chatroom")
    first = reader.rooms(ChatRoomsRequest(limit=2))
    record(store, chat_id="g0@chatroom", chat_name="renamed-after-snapshot")
    record(store, chat_id="new@chatroom")
    second = reader.rooms(
        ChatRoomsRequest(limit=2, before_id=first["next_cursor"], snapshot_id=first["snapshot_id"])
    )
    assert [item["chat_id"] for item in first["items"] + second["items"]] == [
        "g3@chatroom",
        "g2@chatroom",
        "g1@chatroom",
        "g0@chatroom",
    ]
    assert second["next_cursor"] is None and second["total_rooms"] == 4


@pytest.mark.parametrize("query", ["%", "_", "' OR 1=1 --"])
def test_search_wildcards_and_sql_are_literal(archive, query):
    _, store, reader = archive
    record(store, text="ordinary", chat_name="ordinary")
    record(store, text=f"literal {query}", chat_name=f"literal {query}")
    rooms = reader.rooms(ChatRoomsRequest(query=query))
    assert rooms["items"][0]["chat_name"] == f"literal {query}"
    result = messages(reader, query=query)
    assert len(result["items"]) == 1 and result["items"][0]["text"] == f"literal {query}"


def test_message_paging_arrival_order_and_forward_drain_do_not_skip(archive):
    _, store, reader = archive
    for index in range(7):
        record(store, text=str(index), occurred_at=f"2026-10-12T0{6 - index}:00:00+00:00")
    first = messages(reader, limit=3)
    assert [item["text"] for item in first["items"]] == ["4", "5", "6"]
    assert first["has_older"]
    record(store, text="7")
    older = messages(
        reader, limit=3, before_id=first["older_cursor"], snapshot_id=first["snapshot_id"]
    )
    assert [item["text"] for item in older["items"]] == ["1", "2", "3"]
    oldest = messages(reader, before_id=older["older_cursor"], snapshot_id=first["snapshot_id"])
    assert [item["text"] for item in oldest["items"]] == ["0"]
    assert not oldest["has_older"]
    for index in range(8, 13):
        record(store, text=str(index))
    cursor, received = first["newer_cursor"], []
    while True:
        result = messages(reader, after_id=cursor, limit=2)
        received.extend(item["text"] for item in result["items"])
        cursor = result["newer_cursor"]
        if not result["has_newer"]:
            break
    assert received == [str(index) for index in range(7, 13)]
    assert messages(reader, after_id=cursor)["items"] == []


def test_dates_are_inclusive_beijing_and_filters_apply_to_quotes_and_names(archive):
    _, store, reader = archive
    for timestamp in [
        "2026-10-11T15:59:59+00:00",
        "2026-10-11T16:00:00+00:00",
        "2026-10-12T15:59:59.999999+00:00",
        "2026-10-12T16:00:00+00:00",
    ]:
        record(store, occurred_at=timestamp)
    result = messages(reader, from_date="2026-10-12", to_date="2026-10-12")
    assert [item["id"] for item in result["items"]] == [2, 3]
    record(store, sender_role="staff", kind="file", quoted_text="核对关键词", media_name="报价.pdf")
    record(store, sender_role="assistant", direction="outbound", text="核对关键词")
    assert messages(reader, query="关键词", role="staff", kind="file")["total_matching"] == 1
    assert messages(reader, query="报价.pdf")["total_matching"] == 1
    assert messages(reader, role="assistant")["items"][0]["direction"] == "outbound"


@pytest.mark.parametrize(
    "values",
    [
        {"chat_id": "private-user"},
        {"limit": 101},
        {"before_id": 1, "after_id": 2},
        {"from_date": "2026-10-13", "to_date": "2026-10-12"},
        {"from_date": "0001-01-01"},
        {"to_date": "9999-12-31"},
        {"before_id": 2**63},
        {"query": "x" * 201},
    ],
)
def test_query_validation_bounds(values):
    with pytest.raises(ValidationError):
        ChatMessagesRequest(**{"chat_id": ROOM, **values})


def test_public_messages_hide_transport_xml_and_secrets_but_keep_plain_text(archive):
    settings, store, reader = archive
    xml = (
        "<msg><appmsg><title>报价.pdf</title><des>产品报价</des><aeskey>SECRET</aeskey>"
        "<cdnattachurl>https://invalid/SECRET</cdnattachurl></appmsg></msg>"
    )
    record(
        store,
        kind="file",
        text=xml,
        quoted_text=xml,
        quoted_type="49",
        metadata={
            "quoted_media_xml": "SECRET",
            "api_key": "SECRET",
            "mentioned_bot": True,
            "mentions": ["演示客服", 123],
        },
    )
    literal = '<script>alert("demo")</script>'
    record(store, text=literal)
    result = messages(reader)
    assert "SECRET" not in json.dumps(result) and "cdnattachurl" not in json.dumps(result)
    assert result["items"][0]["text"] == "报价.pdf\n产品报价"
    assert result["items"][0]["mentioned_bot"] is True
    assert result["items"][0]["mentions"] == ["演示客服"]
    assert result["items"][1]["text"] == literal
    with closing(sqlite3.connect(settings.chat_log_db_path)) as connection:
        connection.execute(
            "UPDATE chat_messages SET metadata='broken',media_size='broken' WHERE id=2"
        )
        connection.commit()
    assert messages(reader)["items"][1]["media"]["size_bytes"] == 0


def stage_inbound(settings, *, chat_id=ROOM, sender_id=ROOM, message_id="image-demo", content=PNG):
    inbound = InboundMediaStore(
        settings.vault_storage_dir / "inbound-media",
        settings.vault_storage_dir / "inbound-media-index.db",
    )
    return inbound.stage(
        media_id=f"inm_{message_id}",
        message_id=message_id,
        chat_id=chat_id,
        sender_id=sender_id,
        kind="image",
        mime_type="image/png",
        content=content,
    )


def test_inbound_group_scoping_sender_fallback_and_missing_attachment(archive):
    settings, store, reader = archive
    image = stage_inbound(settings)
    record(store, kind="image", text="", message_id=image.message_id, media_ref=image.media_id)
    record(
        store,
        chat_id="other@chatroom",
        kind="image",
        text="",
        message_id=image.message_id,
        media_ref=image.media_id,
    )
    first = messages(reader)["items"][0]
    assert first["media"]["inline_image"] and first["media"]["mime_type"] == "image/png"
    assert str(image.path) not in json.dumps(first) and "inbound_" not in json.dumps(first)
    assert reader.media(1)["path"] == image.path
    assert reader.media(2) is None
    image.path.unlink()
    assert messages(reader)["items"][0]["media"]["status"] == "missing"


def catalog_asset(settings, path, *, media_id="asset", name="演示文件.pdf"):
    with closing(sqlite3.connect(settings.db_path)) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS attachments "
            "(media_id TEXT, display_name TEXT, status TEXT, archive_file TEXT)"
        )
        connection.execute(
            "INSERT INTO attachments VALUES (?, ?, 'available', ?)", (media_id, name, str(path))
        )
        connection.commit()


@pytest.mark.parametrize("relative", [True, False])
def test_outbound_retained_files_and_no_raw_paths(archive, relative):
    settings, store, reader = archive
    settings.media_dir.mkdir(parents=True)
    path = settings.media_dir / "opaque-token.bin"
    path.write_bytes(b"%PDF-demo")
    catalog_asset(settings, path.relative_to(settings.db_path.parent) if relative else path)
    record(store, kind="file", media_ref="asset", direction="outbound", sender_role="assistant")
    result = messages(reader)["items"][0]["media"]
    assert result["name"] == "演示文件.pdf" and result["available"]
    assert not result["inline_image"]
    assert "opaque-token" not in json.dumps(result) and "path" not in result


def test_catalog_cannot_download_files_outside_media_root(archive, tmp_path):
    settings, store, reader = archive
    outside = tmp_path / "private.env"
    outside.write_text("SECRET")
    catalog_asset(settings, outside)
    record(store, kind="file", media_ref="asset", direction="outbound")
    assert reader.media(1) is None
    assert not messages(reader)["items"][0]["media"]["available"]


def test_inbound_index_path_tampering_is_rejected(archive):
    settings, store, reader = archive
    image = stage_inbound(settings)
    record(store, kind="image", message_id=image.message_id, media_ref=image.media_id)
    index_path = settings.vault_storage_dir / "inbound-media-index.db"
    with closing(sqlite3.connect(index_path)) as connection:
        connection.execute("UPDATE inbound_media SET file_name='../secret.env'")
        connection.commit()
    assert reader.media(1) is None


@pytest.mark.parametrize("content", [b'<svg onload="alert(1)"></svg>', b"<html>demo</html>"])
def test_fake_image_mime_forces_attachment_download(archive, content):
    settings, store, reader = archive
    image = stage_inbound(settings, content=content)
    record(store, kind="image", message_id=image.message_id, media_ref=image.media_id)
    assert reader.media(1)["mime_type"] == "application/octet-stream"
    assert messages(reader)["items"][0]["media"]["inline_image"] is False


def test_reads_do_not_modify_archive_and_see_uncheckpointed_wal(archive):
    settings, store, reader = archive
    record(store)
    before = settings.chat_log_db_path.read_bytes()
    with closing(sqlite3.connect(settings.chat_log_db_path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE chat_messages SET text='WAL-visible' WHERE id=1")
        writer.commit()
        assert messages(reader)["items"][0]["text"] == "WAL-visible"
        assert settings.chat_log_db_path.read_bytes() == before
    before = settings.chat_log_db_path.read_bytes()
    messages(reader)
    reader.rooms(ChatRoomsRequest())
    assert settings.chat_log_db_path.read_bytes() == before


def client_for(tmp_path, settings):
    auth = tmp_path / "auth.json"
    configure_admin(auth, "test-password-123")
    app = FastAPI()
    install_admin(
        app,
        groups_path=tmp_path / "absent.toml",
        auth_path=auth,
        backup_dir=tmp_path / "backups",
        env_path=tmp_path / ".env",
        runtime_settings=settings,
    )
    return TestClient(app)


def test_admin_routes_require_session_csrf_and_same_origin(archive, tmp_path):
    settings, store, _ = archive
    record(store)
    client = client_for(tmp_path, settings)
    assert client.post("/api/admin/chats/rooms", json={}).status_code == 401
    assert client.get("/api/admin/chats/messages/1/media").status_code == 401
    login = client.post("/api/admin/login", json={"password": "test-password-123"})
    headers = {"X-CSRF-Token": login.json()["csrf_token"]}
    assert client.post("/api/admin/chats/rooms", json={}).status_code == 403
    cross_origin = client.post(
        "/api/admin/chats/rooms",
        json={},
        headers={**headers, "Origin": "https://untrusted.invalid"},
    )
    assert cross_origin.status_code == 403
    before = settings.chat_log_db_path.read_bytes()
    for path, payload in [("rooms", {}), ("messages", {"chat_id": ROOM})]:
        response = client.post(f"/api/admin/chats/{path}", json=payload, headers=headers)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert settings.chat_log_db_path.read_bytes() == before
    assert not (tmp_path / "history.db").exists() and not (tmp_path / "backups").exists()
    assert (
        client.post(
            "/api/admin/chats/messages", json={"chat_id": "private"}, headers=headers
        ).status_code
        == 422
    )


def test_media_serving_auth_headers_and_download_disposition(archive, tmp_path):
    settings, store, _ = archive
    image = stage_inbound(settings)
    record(store, kind="image", message_id=image.message_id, media_ref=image.media_id)
    client = client_for(tmp_path, settings)
    client.post("/api/admin/login", json={"password": "test-password-123"})
    response = client.get("/api/admin/chats/messages/1/media")
    assert response.status_code == 200 and response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert (
        client.get("/api/admin/chats/messages/1/media?download=true")
        .headers["content-disposition"]
        .startswith("attachment")
    )
    assert client.get("/api/admin/chats/messages/99999/media").status_code == 404
    image.path.unlink()
    assert client.get("/api/admin/chats/messages/1/media").status_code == 404


def test_unavailable_archive_api_reports_safe_error(archive, tmp_path):
    settings, _, _ = archive
    settings.chat_log_db_path.unlink()
    client = client_for(tmp_path, settings)
    login = client.post("/api/admin/login", json={"password": "test-password-123"})
    response = client.post(
        "/api/admin/chats/rooms", json={}, headers={"X-CSRF-Token": login.json()["csrf_token"]}
    )
    assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
    assert str(tmp_path) not in response.text
    assert not settings.chat_log_db_path.exists()
