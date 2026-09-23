"""Tests for attachment de-duplication, serial dispatch, and media streaming."""

from __future__ import annotations

import logging
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient

from fastgpt_assistant.core.models import Deliverable
from fastgpt_assistant.core.query_engine import ReadModel
from fastgpt_assistant.orchestration import engine as engine_module
from fastgpt_assistant.orchestration.engine import (
    AssistantEngine,
    ReplyDeliverable,
    dedupe_deliverables,
)
from fastgpt_assistant.server import app as app_module


class _RecordingWechat:
    """Capture outbound calls without touching the network."""

    def __init__(self) -> None:
        self.files: list[tuple[str, str, str]] = []
        self.images: list[tuple[str, str]] = []
        self.texts: list[tuple[str, str]] = []

    def send_text(self, *, to_wxid: str, content: str) -> None:
        self.texts.append((to_wxid, content))

    def send_file(self, *, to_wxid: str, file_url: str, file_name: str) -> None:
        self.files.append((to_wxid, file_url, file_name))

    def send_image(self, *, to_wxid: str, img_url: str) -> None:
        self.images.append((to_wxid, img_url))


def _build_engine(interval: float, wechat: _RecordingWechat) -> AssistantEngine:
    engine = object.__new__(AssistantEngine)
    engine.wechat = wechat
    engine.settings = SimpleNamespace(attachment_dispatch_interval_seconds=interval)
    return engine


def _deliverable(media_id: str, suffix: str = ".pdf", url: str | None = None) -> Deliverable:
    return Deliverable(
        media_id=media_id,
        filename=f"{media_id}{suffix}",
        download_url=f"https://public.example/v1/media/{media_id}{suffix}" if url is None else url,
    )


def test_dedupe_keeps_first_seen_order() -> None:
    first = _deliverable("med_a")
    second = _deliverable("med_b")

    assert dedupe_deliverables([first, second, first, second, first]) == [first, second]


def test_dedupe_treats_two_skus_of_one_category_as_one_file() -> None:
    """The regression that triggered the GeWe postFile timeout: 2 SKUs, 2 copies."""
    shared = _deliverable("med_shared")

    assert len(dedupe_deliverables([shared, shared, shared, shared])) == 1


def test_dedupe_falls_back_to_download_url_without_media_id() -> None:
    same_url_a = Deliverable(media_id="", filename="a.pdf", download_url="https://x/1.pdf")
    same_url_b = Deliverable(media_id="", filename="b.pdf", download_url="https://x/1.pdf")

    assert dedupe_deliverables([same_url_a, same_url_b]) == [same_url_a]


def test_dedupe_keeps_entries_with_no_identity() -> None:
    blank_a = Deliverable(media_id="", filename="a.pdf")
    blank_b = Deliverable(media_id="", filename="b.pdf")

    assert dedupe_deliverables([blank_a, blank_b]) == [blank_a, blank_b]


def test_dispatch_sends_each_file_once_with_a_pause_between(monkeypatch) -> None:
    wechat = _RecordingWechat()
    engine = _build_engine(interval=5.0, wechat=wechat)
    slept: list[float] = []
    monkeypatch.setattr(engine_module.time, "sleep", slept.append)

    duplicate = _deliverable("med_a")
    distinct = _deliverable("med_b")
    engine.dispatch_outbound(
        "room@chatroom",
        ReplyDeliverable(text="report", deliverables=[duplicate, duplicate, distinct]),
    )

    assert [url for _, url, _ in wechat.files] == [
        "https://public.example/v1/media/med_a.pdf",
        "https://public.example/v1/media/med_b.pdf",
    ]
    # Exactly one pause: the first attachment must go out immediately.
    assert slept == [5.0]


def test_dispatch_does_not_delay_the_first_sendable_attachment(monkeypatch) -> None:
    wechat = _RecordingWechat()
    engine = _build_engine(interval=5.0, wechat=wechat)
    slept: list[float] = []
    monkeypatch.setattr(engine_module.time, "sleep", slept.append)

    unreachable = Deliverable(media_id="med_x", filename="x.pdf", download_url="")
    sendable = _deliverable("med_y")
    engine.dispatch_outbound(
        "room@chatroom",
        ReplyDeliverable(text="report", deliverables=[unreachable, sendable]),
    )

    assert [url for _, url, _ in wechat.files] == ["https://public.example/v1/media/med_y.pdf"]
    assert slept == []


def test_dispatch_routes_images_to_post_image(monkeypatch) -> None:
    wechat = _RecordingWechat()
    engine = _build_engine(interval=0.0, wechat=wechat)
    monkeypatch.setattr(engine_module.time, "sleep", lambda _seconds: None)

    engine.dispatch_outbound(
        "room@chatroom",
        ReplyDeliverable(text="photo", deliverables=[_deliverable("med_img", suffix=".jpg")]),
    )

    assert [url for _, url in wechat.images] == ["https://public.example/v1/media/med_img.jpg"]
    assert wechat.files == []


def test_dispatch_is_a_noop_when_outbound_suppressed() -> None:
    wechat = _RecordingWechat()
    engine = _build_engine(interval=5.0, wechat=wechat)

    engine.dispatch_outbound(
        "room@chatroom",
        ReplyDeliverable(text="", deliverables=[_deliverable("med_a")], suppress_outbound=True),
    )

    assert wechat.texts == []
    assert wechat.files == []


def test_media_endpoint_streams_with_content_length_and_range_support(
    tmp_path,
    monkeypatch,
) -> None:
    data_root = tmp_path / "data"
    attachments = data_root / "resources" / "attachments"
    attachments.mkdir(parents=True)
    payload = b"%PDF-1.4\n" + b"report-body" * 400
    (attachments / "report.pdf").write_bytes(payload)

    db_path = data_root / "read-model.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE attachments (media_id TEXT, archive_file TEXT, status TEXT)")
    connection.execute(
        "INSERT INTO attachments VALUES (?, ?, ?)",
        (
            "med_stream",
            "data/resources/attachments/report.pdf",
            "available",
        ),
    )
    connection.commit()
    connection.close()

    fake_engine = SimpleNamespace(
        read_model=ReadModel(db_path),
        settings=SimpleNamespace(db_path=db_path),
    )
    monkeypatch.setattr(app_module, "get_engine", lambda: fake_engine)
    client = TestClient(app_module.app)

    full = client.get("/v1/media/med_stream.pdf")
    assert full.status_code == 200
    assert full.content == payload
    assert full.headers["content-length"] == str(len(payload))
    assert full.headers["accept-ranges"] == "bytes"

    partial = client.get("/v1/media/med_stream.pdf", headers={"Range": "bytes=0-99"})
    assert partial.status_code == 206
    assert partial.headers["content-range"] == f"bytes 0-99/{len(payload)}"
    assert partial.headers["content-length"] == "100"
    assert partial.content == payload[:100]


def test_media_endpoint_logs_the_exact_byte_size(tmp_path, monkeypatch, caplog) -> None:
    """Every fetch must leave its real size in the log.

    GeWe's download window is the last unknown in the attachment pipeline, so a
    timeout has to be explainable from the log alone.
    """
    data_root = tmp_path / "data"
    attachments = data_root / "resources" / "attachments"
    attachments.mkdir(parents=True)
    payload = b"%PDF-1.4\n" + b"x" * 4096
    (attachments / "report.pdf").write_bytes(payload)

    db_path = data_root / "read-model.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE attachments (media_id TEXT, archive_file TEXT, status TEXT)")
    connection.execute(
        "INSERT INTO attachments VALUES (?, ?, ?)",
        ("med_size", "data/resources/attachments/report.pdf", "available"),
    )
    connection.commit()
    connection.close()

    fake_engine = SimpleNamespace(
        read_model=ReadModel(db_path),
        settings=SimpleNamespace(db_path=db_path),
    )
    monkeypatch.setattr(app_module, "get_engine", lambda: fake_engine)

    with caplog.at_level(logging.INFO, logger="fastgpt_assistant"):
        client = TestClient(app_module.app)
        assert client.get("/v1/media/med_size.pdf").status_code == 200

    assert f"size={len(payload)} bytes" in caplog.text
    assert "med_size" in caplog.text


def test_media_endpoint_rejects_unavailable_attachment(tmp_path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True)
    db_path = data_root / "read-model.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE attachments (media_id TEXT, archive_file TEXT, status TEXT)")
    connection.execute(
        "INSERT INTO attachments VALUES (?, ?, ?)",
        ("med_gone", "data/resources/attachments/missing.pdf", "missing"),
    )
    connection.commit()
    connection.close()

    fake_engine = SimpleNamespace(
        read_model=ReadModel(db_path),
        settings=SimpleNamespace(db_path=db_path),
    )
    monkeypatch.setattr(app_module, "get_engine", lambda: fake_engine)
    client = TestClient(app_module.app)

    assert client.get("/v1/media/med_gone.pdf").status_code == 404
