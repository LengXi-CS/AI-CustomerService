"""Tests for reverse-proxy-aware public media URL selection."""

from __future__ import annotations

from types import SimpleNamespace

from fastgpt_assistant.orchestration.engine import AssistantEngine


def test_forwarded_public_origin_updates_media_executor() -> None:
    engine = object.__new__(AssistantEngine)
    engine.query_executor = SimpleNamespace(media_base_url="")

    engine.observe_public_origin(
        host="public.example.test, 127.0.0.1:5330",
        proto="https, http",
    )

    assert engine.query_executor.media_base_url == "https://public.example.test"


def test_local_origin_is_not_used_for_public_media() -> None:
    engine = object.__new__(AssistantEngine)
    engine.query_executor = SimpleNamespace(media_base_url="https://configured.example")

    engine.observe_public_origin(host="127.0.0.1:5330", proto="http")

    assert engine.query_executor.media_base_url == "https://configured.example"
