"""Shared lock, comment-preserving rendering, and atomic commits for .env editors."""

from __future__ import annotations

import io
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from fastgpt_assistant.core.config import Settings
from fastgpt_assistant.core.config_journal import ConfigJournal
from fastgpt_assistant.core.group_config_store import atomic_write, revision_of


class InvalidEnvFile(Exception):
    """Use generic messages only: the source may contain credentials."""


def setting_aliases(name: str) -> list[str]:
    alias = Settings.model_fields[name].validation_alias
    return list(alias.choices) if hasattr(alias, "choices") else [str(alias)]


def replace_assignment(source: str, key: str, value: str) -> str:
    key_pattern = "(?:" + re.escape(key) + "|'" + re.escape(key) + "')"
    pattern = (
        r"(?m)(^[ \t]*(?:export[ \t]+)?" + key_pattern + r"[ \t]*=[ \t]*)"
        r"(?:'(?:\\.|[^'])*'|\"(?:\\.|[^\"\\])*\"|[^\r\n]*?)"
        r"([ \t]*(?:#[^\r\n]*)?)(\r\n|\n|\r|$)"
    )
    result, count = re.subn(
        pattern, lambda match: match[1] + value + match[2] + match[3], source, count=1
    )
    if count != 1:
        raise InvalidEnvFile("无法安全更新参数，请先检查配置文件格式。")
    return result


def quote_value(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class EnvironmentDocument:
    def __init__(
        self, path: Path, backup_dir: Path, *, journal: ConfigJournal | None = None
    ) -> None:
        self.path = path
        self.backup_dir = backup_dir
        self.lock = threading.Lock()
        self.journal = journal

    def read(self) -> bytes:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return b""

    @staticmethod
    def bindings(content: bytes):
        try:
            bindings = list(parse_stream(io.StringIO(content.decode("utf-8-sig"))))
        except UnicodeError as exc:
            raise InvalidEnvFile("配置文件编码无效，请先在本机修复。") from exc
        if any(binding.error for binding in bindings):
            raise InvalidEnvFile("配置文件语法无效，请先在本机修复。")
        return bindings

    def values(self, content: bytes) -> dict[str, tuple[str, str]]:
        self.bindings(content)
        # Match pydantic-settings' dotenv interpolation and duplicate-key behavior.
        values = dotenv_values(stream=io.StringIO(content.decode("utf-8-sig")))
        return {key.lower(): (value or "", key) for key, value in values.items()}

    def render(self, original: bytes, changes: dict[str, str]) -> bytes:
        replacements = {
            key.lower(): (name, value)
            for name, value in changes.items()
            for key in setting_aliases(name)
        }
        found = set()
        fragments = []
        for binding in self.bindings(original):
            source = binding.original.string
            if binding.key and binding.key.lower() in replacements:
                name, value = replacements[binding.key.lower()]
                source = replace_assignment(source, binding.key, value)
                found.add(name)
            fragments.append(source)
        text = "".join(fragments)
        newline = "\r\n" if b"\r\n" in original else "\n"
        for name, value in changes.items():
            if name not in found:
                if text and not text.endswith(("\n", "\r")):
                    text += newline
                text += f"{setting_aliases(name)[0]}={value}{newline}"
        candidate = (
            b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b""
        ) + text.encode("utf-8")
        self.bindings(candidate)
        return candidate

    def commit(
        self,
        original: bytes,
        candidate: bytes,
        *,
        write=atomic_write,
        scope: str = "environment",
        fields: list[str] | None = None,
        operation: str = "save",
        source_backup_id: str | None = None,
    ) -> dict:
        if self.journal:
            return self.journal.commit(
                path=self.path,
                original=original,
                candidate=candidate,
                backup_dir=self.backup_dir,
                target="environment",
                scope=scope,
                fields=fields or [],
                write=write,
                operation=operation,
                source_backup_id=source_backup_id,
            )
        if candidate != original:
            if original:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                write(self.backup_dir / f"{stamp}-{revision_of(original)[:12]}.env", original)
            write(self.path, candidate, expected_content=original)
        return {"history_pending": False}
