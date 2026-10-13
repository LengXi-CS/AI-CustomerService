"""Preview and restore only settings managed by the administrator UI."""

from __future__ import annotations

import heapq
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from fastgpt_assistant.core.config import Settings
from fastgpt_assistant.core.config_journal import BACKUP_PATTERN, FIELD_LABELS
from fastgpt_assistant.core.connection_config import SECRET_NAMES, ConnectionValues, SecretEdit
from fastgpt_assistant.core.dotenv_store import InvalidEnvFile, quote_value, setting_aliases
from fastgpt_assistant.core.group_config_store import (
    ConfigConflict,
    GroupConfigStore,
    GroupDocument,
    GroupEdit,
    SoothePolicy,
    atomic_write,
    revision_of,
)
from fastgpt_assistant.core.reply_policy_store import ReplyPolicyStore, RuntimeReplyPolicy
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig

BackupTarget = Literal["groups", "environment"]
MANAGED_ENV = [*RuntimeReplyPolicy.model_fields, *ConnectionValues.model_fields, *SECRET_NAMES]


class BackupUnavailable(Exception):
    """A named snapshot is missing or is not a regular, trusted backup file."""


class BackupSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backup_id: str = Field(pattern="^" + BACKUP_PATTERN + "$", max_length=64)


class RestoreBackupRequest(BackupSelection):
    current_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    backup_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


def backup_time(name: str) -> str:
    return datetime.strptime(name.split("-")[0], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC).isoformat()


def display(value) -> str:
    if isinstance(value, bool):
        return "开启" if value else "关闭"
    if isinstance(value, list):
        prefix = "、".join(str(item) for item in value[:5])
        return (prefix + (f"…（共 {len(value)} 项）" if len(value) > 5 else "")) or "空"
    return str(value) or "空"


class ConfigRecovery:
    def __init__(self, groups: GroupConfigStore, reply: ReplyPolicyStore) -> None:
        self.groups = groups
        self.reply = reply
        self.document = reply.document

    def _directory(self, target: BackupTarget) -> Path:
        return self.groups.backup_dir if target == "groups" else self.document.backup_dir

    def backups(self, target: BackupTarget, *, before: str | None = None, limit: int = 20) -> dict:
        directory = self._directory(target)
        suffix = ".toml" if target == "groups" else ".env"
        if not directory.exists():
            return {"items": [], "next_cursor": None}
        with os.scandir(directory) as entries:
            names = heapq.nlargest(
                limit + 1,
                (
                    entry.name
                    for entry in entries
                    if re.fullmatch(BACKUP_PATTERN, entry.name)
                    and entry.name.endswith(suffix)
                    and (before is None or entry.name < before)
                    and entry.is_file(follow_symlinks=False)
                ),
            )
        items = []
        for name in names[:limit]:
            try:
                path = self._safe_path(target, name)
                items.append(
                    {
                        "backup_id": name,
                        "created_at": backup_time(name),
                        "size_bytes": path.stat().st_size,
                    }
                )
            except (BackupUnavailable, FileNotFoundError, ValueError):
                continue
        return {"items": items, "next_cursor": names[limit - 1] if len(names) > limit else None}

    def _safe_path(self, target: BackupTarget, name: str) -> Path:
        suffix = ".toml" if target == "groups" else ".env"
        directory = self._directory(target).resolve()
        path = directory / name
        if (
            not re.fullmatch(BACKUP_PATTERN, name)
            or not name.endswith(suffix)
            or path.is_symlink()
            or path.resolve().parent != directory
            or not path.is_file()
        ):
            raise BackupUnavailable("备份不存在或无法安全读取，请刷新备份列表。")
        return path

    def _read_backup(self, target: BackupTarget, name: str) -> bytes:
        path = self._safe_path(target, name)
        try:
            backup_time(name)
        except ValueError as exc:
            raise BackupUnavailable("备份标识格式无效，不能用于页面恢复。") from exc
        maximum = 16 * 1024 * 1024 if target == "groups" else 2 * 1024 * 1024
        with path.open("rb") as handle:
            content = handle.read(maximum + 1)
        if len(content) > maximum:
            raise BackupUnavailable("备份超过页面恢复的大小上限，请在本机检查。")
        if revision_of(content)[:12] != name.split("-")[1].split(".")[0]:
            raise BackupUnavailable("备份内容校验失败，不能用于页面恢复。")
        return content

    def _env_values(self, content: bytes, *, strict: bool) -> tuple[dict, set]:
        saved = self.document.values(content)
        values = {}
        invalid = set()
        for name in MANAGED_ENV:
            entry = next(
                (saved[alias.lower()] for alias in setting_aliases(name) if alias.lower() in saved),
                None,
            )
            value = entry[0] if entry else Settings.model_fields[name].default
            if name in RuntimeReplyPolicy.model_fields:
                try:
                    value = TypeAdapter(
                        RuntimeReplyPolicy.model_fields[name].annotation
                    ).validate_python(value)
                except ValidationError:
                    invalid.add(name)
            values[name] = value
        try:
            RuntimeReplyPolicy.model_validate(
                {name: values[name] for name in RuntimeReplyPolicy.model_fields}
            )
        except ValidationError as exc:
            for error in exc.errors():
                if error["loc"]:
                    invalid.add(str(error["loc"][0]))
                else:
                    invalid.update(
                        (
                            "routing_normal_confidence_threshold",
                            "routing_human_confidence_threshold",
                        )
                    )
        try:
            ConnectionValues.model_validate(
                {name: values[name] for name in ConnectionValues.model_fields}
            )
        except ValidationError as exc:
            invalid.update(str(error["loc"][0]) for error in exc.errors())
        for name in SECRET_NAMES:
            if values[name]:
                try:
                    SecretEdit(action="set", value=values[name])
                except ValidationError:
                    invalid.add(name)
        if strict and invalid:
            raise InvalidEnvFile("备份中的可管理参数格式无效，不能用于恢复。")
        return values, invalid

    def _prepare_environment(self, original: bytes, backup: bytes) -> tuple[bytes, list]:
        before, invalid = self._env_values(original, strict=False)
        desired, _ = self._env_values(backup, strict=True)
        hidden = set()
        for content in (original, backup):
            saved = self.document.values(content)
            for name in (*SECRET_NAMES, "fastgpt_root_key", "payload_vault_key"):
                if name not in Settings.model_fields:
                    continue
                for alias in setting_aliases(name):
                    if (entry := saved.get(alias.lower())) and entry[0]:
                        hidden.add(entry[0])
                    for key, value in self.reply.environ.items():
                        if key.lower() == alias.lower() and value:
                            hidden.add(value)

        def sensitive(value):
            return isinstance(value, str) and any(
                value == token or (len(token) >= 6 and token in value) for token in hidden
            )

        # A dotenv expression can place an API key inside an otherwise public
        # model name or URL. Never expose it in preview, or restore such a plan
        # where another admin endpoint could subsequently reveal the value.
        if any(sensitive(desired[name]) for name in ConnectionValues.model_fields):
            raise InvalidEnvFile("备份的公开参数包含敏感值，请在本机检查后再恢复。")
        changes = {name: desired[name] for name in MANAGED_ENV if before[name] != desired[name]}
        overrides = {key.lower() for key in self.reply.environ}
        differences = []
        for name, value in changes.items():
            secret = name in SECRET_NAMES
            differences.append(
                {
                    "field": name,
                    "label": FIELD_LABELS[name],
                    "secret": secret,
                    "before": ("已配置（内容隐藏）" if before[name] else "未配置")
                    if secret
                    else "敏感内容已隐藏"
                    if sensitive(before[name])
                    else "格式无效（原值已隐藏）"
                    if name in invalid
                    else display(before[name]),
                    "after": ("已配置（内容隐藏）" if value else "未配置")
                    if secret
                    else display(value),
                    "blocked": any(alias.lower() in overrides for alias in setting_aliases(name)),
                }
            )
        candidate = self.document.render(
            original,
            {
                name: quote_value(str(value).lower() if isinstance(value, bool) else str(value))
                for name, value in changes.items()
            },
        )
        actual, _ = self._env_values(candidate, strict=True)
        if actual != desired:
            raise InvalidEnvFile("无法安全还原备份中的可管理参数，原文件未修改。")
        return candidate, differences

    def _prepare_groups(self, original: bytes, backup: bytes) -> tuple[bytes, list]:
        before, desired = self.groups._snapshot(original), self.groups._snapshot(backup)
        old_config = WechatGroupsConfig.from_mapping(self.groups._parse(original).unwrap())
        new_config = WechatGroupsConfig.from_mapping(self.groups._parse(backup).unwrap())
        groups = GroupDocument(global_enabled=desired["global_enabled"], groups=desired["groups"])
        soothe = SoothePolicy(
            soothe_mode=new_config.soothe_mode, soothe_fixed_reply=new_config.soothe_fixed_reply
        )
        candidate = self.groups.render_soothe(self.groups.render_groups(original, groups), soothe)
        differences = []

        def add(field, label, left, right):
            if left != right:
                differences.append(
                    {
                        "field": field,
                        "label": label,
                        "before": display(left),
                        "after": display(right),
                        "secret": False,
                        "blocked": False,
                    }
                )

        add(
            "global.enabled",
            FIELD_LABELS["global.enabled"],
            before["global_enabled"],
            desired["global_enabled"],
        )
        for name in SoothePolicy.model_fields:
            add(
                "global." + name,
                FIELD_LABELS["global." + name],
                getattr(old_config, name),
                getattr(new_config, name),
            )
        old = {group["group_id"]: group for group in before["groups"]}
        new = {group["group_id"]: group for group in desired["groups"]}
        for key in old:
            if key not in new:
                add("groups.removed", old[key]["group_name"] or key, "当前已授权", "移除授权")
        for key, group in new.items():
            title = group["group_name"] or key
            if key not in old:
                add("groups.added", title, "当前未授权", "恢复群规则 · " + group["mode"])
            else:
                for name in GroupEdit.model_fields:
                    if name != "group_id":
                        field = "groups." + name
                        add(field, title + " · " + FIELD_LABELS[field], old[key][name], group[name])
        if list(old) != list(new) and old.keys() == new.keys():
            add("groups.order", FIELD_LABELS["groups.order"], "当前顺序", "备份中的顺序")
        return candidate, differences

    def _prepare(self, target: BackupTarget, backup_id: str) -> tuple[dict, bytes, bytes]:
        backup = self._read_backup(target, backup_id)
        original = self.groups._read() if target == "groups" else self.document.read()
        candidate, differences = (
            self._prepare_groups(original, backup)
            if target == "groups"
            else self._prepare_environment(original, backup)
        )
        descriptor = {
            "target": target,
            "backup_id": backup_id,
            "created_at": backup_time(backup_id),
            "current_revision": revision_of(original),
            "backup_revision": revision_of(backup),
            "changes": differences,
            "can_restore": bool(differences)
            and not any(change["blocked"] for change in differences),
            "apply_mode": "next_policy_read" if target == "groups" else "restart",
        }
        descriptor["plan_revision"] = revision_of(
            json.dumps(descriptor, sort_keys=True, ensure_ascii=True).encode() + candidate
        )
        return descriptor, original, candidate

    def preview(self, target: BackupTarget, request: BackupSelection) -> dict:
        lock = self.groups._lock if target == "groups" else self.document.lock
        with lock:
            result, _, _ = self._prepare(target, request.backup_id)
            return result

    def restore(self, target: BackupTarget, request: RestoreBackupRequest) -> dict:
        lock = self.groups._lock if target == "groups" else self.document.lock
        with lock:
            preview, original, candidate = self._prepare(target, request.backup_id)
            if any(
                getattr(request, name) != preview[name]
                for name in ("current_revision", "backup_revision", "plan_revision")
            ):
                raise ConfigConflict("当前配置、备份或恢复条件已改变，请重新预览后确认。")
            if not preview["can_restore"]:
                raise ConfigConflict("没有可恢复的差异，或部分参数由服务环境变量覆盖。")
            if target == "groups":
                result = self.groups._commit(
                    original, candidate, operation="restore", source_backup_id=request.backup_id
                )
            else:
                result = self.document.commit(
                    original,
                    candidate,
                    write=atomic_write,
                    scope="environment",
                    fields=[change["field"] for change in preview["changes"]],
                    operation="restore",
                    source_backup_id=request.backup_id,
                )
            return {
                **result,
                "revision": revision_of(candidate),
                "changed": True,
                "apply_mode": preview["apply_mode"],
            }
