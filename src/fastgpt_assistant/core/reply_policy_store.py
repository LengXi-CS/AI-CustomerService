"""Allow-listed edits to reply policy fields in .env; never expose credentials."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from fastgpt_assistant.core.config import Settings
from fastgpt_assistant.core.dotenv_store import EnvironmentDocument
from fastgpt_assistant.core.dotenv_store import InvalidEnvFile as InvalidReplyFile
from fastgpt_assistant.core.dotenv_store import setting_aliases as policy_aliases
from fastgpt_assistant.core.group_config_store import ConfigConflict, atomic_write, revision_of


class RuntimeReplyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    conversation_quiet_window_seconds: int = Field(default=120, ge=0, le=86400)
    conversation_poll_interval_seconds: int = Field(default=30, ge=1, le=86400)
    attachment_dispatch_interval_seconds: float = Field(default=5.0, ge=0, le=86400)
    routing_normal_confidence_threshold: float = Field(default=0.80, ge=0, le=1)
    routing_human_confidence_threshold: float = Field(default=0.50, ge=0, le=1)
    owner_notify_enabled: bool = True

    @model_validator(mode="after")
    def threshold_order(self) -> RuntimeReplyPolicy:
        if self.routing_human_confidence_threshold > self.routing_normal_confidence_threshold:
            raise ValueError("人工阈值不能高于正常阈值")
        return self


class SaveRuntimeReplyRequest(RuntimeReplyPolicy):
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


def decode_value(name: str, value: str):
    try:
        return TypeAdapter(RuntimeReplyPolicy.model_fields[name].annotation).validate_python(value)
    except ValidationError as exc:
        raise InvalidReplyFile("回复策略参数格式无效，请先在本机修复配置。") from exc


class ReplyPolicyStore:
    def __init__(
        self,
        path: Path,
        backup_dir: Path,
        *,
        runtime_settings: Settings | None = None,
        environ: Mapping[str, str] | None = None,
        document: EnvironmentDocument | None = None,
    ) -> None:
        self.path = path
        self.backup_dir = backup_dir
        self.document = document or EnvironmentDocument(path, backup_dir)
        self._lock = self.document.lock
        self.environ = os.environ if environ is None else environ
        self.active_values = {
            name: getattr(runtime_settings, name) if runtime_settings is not None else field.default
            for name, field in RuntimeReplyPolicy.model_fields.items()
        }

    def _read(self) -> bytes:
        return self.document.read()

    def _snapshot(self, content: bytes) -> dict:
        file_values = self.document.values(content)
        environment = {key.lower(): (value, key) for key, value in self.environ.items()}
        values = {}
        fields = {}
        for name, field in RuntimeReplyPolicy.model_fields.items():
            aliases = [key.lower() for key in policy_aliases(name)]
            saved = next((file_values[key] for key in aliases if key in file_values), None)
            override = next((environment[key] for key in aliases if key in environment), None)
            value = (
                decode_value(name, override[0])
                if override
                else decode_value(name, saved[0])
                if saved
                else field.default
            )
            pending = value != self.active_values[name]
            values[name] = value
            fields[name] = {
                "source": "environment" if override else "dotenv" if saved else "default",
                "source_key": override[1] if override else saved[1] if saved else None,
                "editable": override is None,
                "status": "environment_override"
                if override
                else "pending_restart"
                if pending
                else "effective",
                "pending_restart": pending,
            }
        try:
            RuntimeReplyPolicy.model_validate(values)
        except ValidationError as exc:
            raise InvalidReplyFile("回复策略参数超出范围或阈值顺序无效，请先修复配置。") from exc
        return {
            "revision": revision_of(content),
            "values": values,
            "active_values": dict(self.active_values),
            "fields": fields,
            "apply_mode": "restart",
            "restart_required": any(field["pending_restart"] for field in fields.values()),
        }

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot(self._read())

    def save(self, request: SaveRuntimeReplyRequest) -> dict:
        with self._lock:
            original = self._read()
            if request.revision != revision_of(original):
                raise ConfigConflict("环境配置文件已被修改，请重新加载回复节奏与路由配置。")
            before = self._snapshot(original)
            proposed = request.model_dump(exclude={"revision"})
            changed_fields = {
                name: value for name, value in proposed.items() if value != before["values"][name]
            }
            for name in changed_fields:
                if not before["fields"][name]["editable"]:
                    raise ConfigConflict("该参数由服务环境变量覆盖，不能通过页面修改。")
            candidate = self.document.render(
                original,
                {
                    name: str(value).lower() if isinstance(value, bool) else str(value)
                    for name, value in changed_fields.items()
                },
            )
            # Verify our renderer using the same reader before creating a backup.
            after = self._snapshot(candidate)
            if after["values"] != proposed:
                raise InvalidReplyFile("无法安全更新回复策略参数，原文件未被修改。")
            result = self.document.commit(
                original, candidate, write=atomic_write, scope="reply", fields=list(changed_fields)
            )
            after.update(result)
            return after
