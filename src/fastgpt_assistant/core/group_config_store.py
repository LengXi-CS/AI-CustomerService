"""Validated, revision-aware edits of the existing group policy TOML."""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from fastgpt_assistant.core.config_journal import ConfigJournal
from fastgpt_assistant.core.wechat_groups import (
    DEFAULT_FIXED_SOOTHE_REPLY,
    WechatGroupPolicy,
    WechatGroupsConfig,
)


class GroupEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    group_id: str = Field(min_length=1, max_length=128, pattern=r"^[^\s@]+@chatroom$")
    group_name: str = Field(default="", max_length=120)
    mode: Literal["disabled", "shadow", "active"] = "disabled"
    require_customer_mention: StrictBool = False
    allow_images: StrictBool = True
    allow_files: StrictBool = True
    owner_id: str = Field(default="", max_length=128)
    customer_service_ids: list[str] = Field(default_factory=list, max_length=200)
    bot_aliases: list[str] = Field(default_factory=list, max_length=50)
    reply_to_staff_when_mentioned: StrictBool = True
    native_quote_replies: StrictBool = False
    associate_customer_demands: StrictBool = False
    conversation_quiet_window_seconds: int | None = Field(
        default=None, ge=0, le=86400, strict=True
    )

    @field_validator("customer_service_ids", "bot_aliases")
    @classmethod
    def validate_list(cls, values: list[str]) -> list[str]:
        values = [value.strip() for value in values]
        if any(not value or len(value) > 128 for value in values):
            raise ValueError("列表项不能为空，且不能超过 128 个字符")
        if len(values) != len(set(values)):
            raise ValueError("列表中不能有重复项")
        return values


class GroupDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    global_enabled: StrictBool
    groups: list[GroupEdit] = Field(max_length=500)

    @model_validator(mode="after")
    def unique_groups(self) -> GroupDocument:
        ids = [group.group_id for group in self.groups]
        if len(ids) != len(set(ids)):
            raise ValueError("群 ID 不能重复")
        return self


class SaveGroupsRequest(GroupDocument):
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class SoothePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    soothe_mode: Literal["model", "fixed"] = "model"
    soothe_fixed_reply: str = Field(default=DEFAULT_FIXED_SOOTHE_REPLY, max_length=1000)

    @model_validator(mode="after")
    def fixed_text_required(self) -> SoothePolicy:
        if self.soothe_mode == "fixed" and not self.soothe_fixed_reply:
            raise ValueError("固定话术模式必须填写安抚文案")
        return self


class SaveSootheRequest(SoothePolicy):
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class ConfigConflict(Exception):
    """Another editor changed the source since it was read."""


class InvalidGroupFile(Exception):
    """The on-disk policy needs to be repaired before it can be edited."""


def revision_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def atomic_write(path: Path, content: bytes, *, expected_content: bytes | None = None) -> None:
    """Replace within the same filesystem; never expose partially written TOML."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(name, path.stat().st_mode)
        if expected_content is not None:
            try:
                current = path.read_bytes()
            except FileNotFoundError:
                current = b""
            if current != expected_content:
                raise ConfigConflict("配置已被其他操作修改，请重新加载。")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class GroupConfigStore:
    def __init__(
        self, path: Path, backup_dir: Path, *, journal: ConfigJournal | None = None
    ) -> None:
        self.path = path
        self.backup_dir = backup_dir
        self._lock = threading.Lock()
        self.journal = journal

    def _read(self) -> bytes:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return b""

    @staticmethod
    def _parse(content: bytes):
        try:
            document = tomlkit.parse(content.decode("utf-8-sig"))
            WechatGroupsConfig.from_mapping(document.unwrap())
            return document
        except (ValueError, TypeError, KeyError) as exc:
            raise InvalidGroupFile("群策略文件格式无效，请先在本机修复；原文件未被修改。") from exc

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot(self._read())

    def _snapshot(self, content: bytes) -> dict:
        raw = self._parse(content).unwrap()
        groups = []
        for item in raw.get("groups", []):
            policy = WechatGroupPolicy.from_mapping(item)
            values = {name: getattr(policy, name) for name in GroupEdit.model_fields}
            values["customer_service_ids"] = item.get("customer_service_ids", [])
            groups.append(GroupEdit.model_validate(values).model_dump())
        result = GroupDocument(
            global_enabled=raw.get("global", {}).get("enabled", False), groups=groups
        ).model_dump()
        return {"revision": revision_of(content), **result, "apply_mode": "next_policy_read"}

    def soothe_snapshot(self) -> dict:
        with self._lock:
            content = self._read()
            document = self._parse(content)
            config = WechatGroupsConfig.from_mapping(document.unwrap())
            return {
                "revision": revision_of(content),
                "values": SoothePolicy(
                    soothe_mode=config.soothe_mode,
                    soothe_fixed_reply=config.soothe_fixed_reply,
                ).model_dump(),
                "apply_mode": "next_policy_read",
            }

    def save_soothe(self, request: SaveSootheRequest) -> dict:
        with self._lock:
            original = self._read()
            if request.revision != revision_of(original):
                raise ConfigConflict("群策略文件已被修改，请重新加载安抚话术配置。")
            candidate = self.render_soothe(original, request)
            result = self._commit(original, candidate, scope="soothe")
            document = self._parse(candidate)
            config = WechatGroupsConfig.from_mapping(document.unwrap())
            result["values"] = {
                "soothe_mode": config.soothe_mode,
                "soothe_fixed_reply": config.soothe_fixed_reply,
            }
            return result

    def render_soothe(self, original: bytes, request: SoothePolicy) -> bytes:
        document = self._parse(original)
        config = WechatGroupsConfig.from_mapping(document.unwrap())
        previous = {
            "soothe_mode": config.soothe_mode,
            "soothe_fixed_reply": config.soothe_fixed_reply,
        }
        for key, value in request.model_dump(exclude={"revision"}).items():
            if value != previous[key]:
                if "global" not in document:
                    document["global"] = tomlkit.table()
                document["global"][key] = value
        return tomlkit.dumps(document).encode("utf-8")

    def _commit(
        self,
        original: bytes,
        candidate: bytes,
        *,
        scope: str = "groups",
        operation: str = "save",
        source_backup_id: str | None = None,
    ) -> dict:
        """Called with the shared lock held by either group or soothe editing."""
        self._parse(candidate)
        if self._read() != original:
            raise ConfigConflict("配置已被其他操作修改，请重新加载。")
        changed = candidate != original
        backup_id = None
        metadata = {}
        if self.journal:
            metadata = self.journal.commit(
                path=self.path,
                original=original,
                candidate=candidate,
                backup_dir=self.backup_dir,
                target="groups",
                scope=scope,
                fields=self.changed_fields(original, candidate),
                operation=operation,
                source_backup_id=source_backup_id,
                write=atomic_write,
            )
            backup_id = metadata["backup_id"]
        elif changed:
            if original:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                backup_id = f"{stamp}-{revision_of(original)[:12]}.toml"
                atomic_write(self.backup_dir / backup_id, original)
            atomic_write(self.path, candidate, expected_content=original)
        return {
            "revision": revision_of(candidate),
            "changed": changed,
            "backup_id": backup_id,
            "apply_mode": "next_policy_read",
            **metadata,
        }

    def changed_fields(self, original: bytes, candidate: bytes) -> list[str]:
        before, after = self._snapshot(original), self._snapshot(candidate)
        fields = []
        if before["global_enabled"] != after["global_enabled"]:
            fields.append("global.enabled")
        left = WechatGroupsConfig.from_mapping(self._parse(original).unwrap())
        right = WechatGroupsConfig.from_mapping(self._parse(candidate).unwrap())
        for name in SoothePolicy.model_fields:
            if getattr(left, name) != getattr(right, name):
                fields.append("global." + name)
        old = {group["group_id"]: group for group in before["groups"]}
        new = {group["group_id"]: group for group in after["groups"]}
        if new.keys() - old.keys():
            fields.append("groups.added")
        if old.keys() - new.keys():
            fields.append("groups.removed")
        if list(old) != list(new) and old.keys() == new.keys():
            fields.append("groups.order")
        for name in GroupEdit.model_fields:
            if name != "group_id" and any(
                old[key][name] != new[key][name] for key in old.keys() & new.keys()
            ):
                fields.append("groups." + name)
        return fields

    def save(self, request: SaveGroupsRequest) -> dict:
        with self._lock:
            original = self._read()
            if request.revision != revision_of(original):
                raise ConfigConflict("配置已被其他操作修改。请重新加载后合并你的修改。")
            candidate = self.render_groups(original, request)
            return self._commit(original, candidate)

    def render_groups(self, original: bytes, request: GroupDocument) -> bytes:
        document = self._parse(original)
        if "global" not in document:
            document["global"] = tomlkit.table()
        document["global"]["enabled"] = request.global_enabled
        previous = {str(item["group_id"]): item for item in document.get("groups", [])}
        rows = tomlkit.aot()
        for group in request.groups:
            row = previous.get(group.group_id, tomlkit.table())
            defaults = GroupEdit(group_id=group.group_id).model_dump()
            for key, value in group.model_dump().items():
                if (key == "conversation_quiet_window_seconds" and key not in group.model_fields_set
                        and group.group_id in previous):
                    continue
                if value is None:
                    row.pop(key, None)
                    continue
                if key in row and row[key] == value:
                    continue
                if group.group_id in previous and key not in row and value == defaults[key]:
                    continue
                row[key] = value
            rows.append(row)
        document["groups"] = rows
        return tomlkit.dumps(document).encode("utf-8")
