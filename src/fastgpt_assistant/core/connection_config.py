"""Scoped connection settings with write-only credential updates and safe probes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from fastgpt_assistant.core.config import Settings
from fastgpt_assistant.core.dotenv_store import (
    EnvironmentDocument,
    InvalidEnvFile,
    quote_value,
    setting_aliases,
)
from fastgpt_assistant.core.group_config_store import ConfigConflict, atomic_write, revision_of

SECRET_NAMES = ("llm_api_key", "fastgpt_api_key", "gewe_token", "lark_app_secret")
SecretName = Literal["llm_api_key", "fastgpt_api_key", "gewe_token", "lark_app_secret"]
ServiceName = Literal["llm", "fastgpt", "gewe", "lark"]


class ConnectionValues(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    llm_base_url: str = Field(default="https://api.deepseek.com", max_length=2048)
    llm_model: str = Field(default="deepseek-flash", min_length=1, max_length=256)
    llm_vision_model: str = Field(default="", max_length=256)
    fastgpt_api_url: str = Field(default="http://localhost:3001", max_length=2048)
    fastgpt_dataset_id: str = Field(default="", max_length=256)
    gewe_api_url: str = Field(default="http://api.geweapi.com", max_length=2048)
    gewe_app_id: str = Field(default="", max_length=256)
    lark_app_id: str = Field(default="", max_length=256)
    lark_bitable_app_token: str = Field(default="", max_length=256)

    @field_validator("*")
    @classmethod
    def plain_values(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value) or "${" in value:
            raise ValueError("字段不能包含控制字符或环境变量表达式")
        return value

    @field_validator("llm_base_url", "fastgpt_api_url", "gewe_api_url")
    @classmethod
    def service_url(cls, value: str) -> str:
        try:
            url = urlsplit(value)
            valid = (
                url.scheme in {"http", "https"}
                and url.hostname
                and not (url.username or url.password or url.query or url.fragment)
            )
            if url.port is not None and not 1 <= url.port <= 65535:
                valid = False
        except ValueError as exc:
            raise ValueError("请输入有效的 HTTP/HTTPS 服务地址") from exc
        if not valid:
            raise ValueError("服务地址须为 HTTP/HTTPS，不能包含用户名、密码、查询参数或片段")
        return value


class SecretEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["keep", "set", "clear"] = "keep"
    value: SecretStr | None = Field(default=None, max_length=8192)

    @model_validator(mode="after")
    def valid_action(self) -> SecretEdit:
        value = self.value.get_secret_value().strip() if self.value else ""
        if self.action == "set":
            if not value or any(ord(char) < 32 for char in value) or "${" in value:
                raise ValueError("替换密钥须填写非空值，不能包含控制字符或环境变量表达式")
        elif self.value is not None:
            raise ValueError("保留或清除密钥时不能提交密钥值")
        return self


class SaveConnectionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    values: ConnectionValues
    secrets: dict[SecretName, SecretEdit] = Field(default_factory=dict)


class ConnectionConfigStore:
    def __init__(
        self,
        document: EnvironmentDocument,
        *,
        runtime_settings: Settings | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.document = document
        self.environ = os.environ if environ is None else environ
        names = [*ConnectionValues.model_fields, *SECRET_NAMES, "fastgpt_root_key"]
        self.active = {
            name: getattr(runtime_settings, name)
            if runtime_settings is not None
            else Settings.model_fields[name].default
            for name in names
        }

    def _state(self, content: bytes):
        file_values = self.document.values(content)
        environment = {key.lower(): (value, key) for key, value in self.environ.items()}
        values, sources = {}, {}
        for name in self.active:
            aliases = [key.lower() for key in setting_aliases(name)]
            saved = next((file_values[key] for key in aliases if key in file_values), None)
            override = next((environment[key] for key in aliases if key in environment), None)
            values[name] = (
                override[0]
                if override
                else saved[0]
                if saved
                else Settings.model_fields[name].default
            )
            sources[name] = {
                "source": "environment" if override else "dotenv" if saved else "default",
                "source_key": override[1] if override else saved[1] if saved else None,
                "editable": override is None,
                "pending_restart": values[name] != self.active[name],
            }
        try:
            ConnectionValues.model_validate(
                {name: values[name] for name in ConnectionValues.model_fields}
            )
        except ValueError as exc:
            raise InvalidEnvFile("连接配置格式无效，请在本机检查服务地址和字段格式。") from exc
        return values, sources

    def _snapshot(self, content: bytes) -> dict:
        values, sources = self._state(content)
        public_values = {name: values[name] for name in ConnectionValues.model_fields}
        active_public = {name: self.active[name] for name in ConnectionValues.model_fields}
        for name in ("llm_base_url", "fastgpt_api_url", "gewe_api_url"):
            try:
                ConnectionValues.service_url(ConnectionValues.plain_values(active_public[name]))
            except ValueError:
                # A legacy process may still have a basic-auth/query credential
                # in its URL after the saved configuration has been corrected.
                active_public[name] = "旧地址格式不受支持，已隐藏"
        secret_status = {
            name: {
                **sources[name],
                "configured": bool(values[name]),
                "active_configured": bool(self.active[name]),
            }
            for name in SECRET_NAMES
        }
        return {
            "revision": revision_of(content),
            "values": public_values,
            "active_values": active_public,
            "fields": {name: sources[name] for name in ConnectionValues.model_fields},
            "secrets": secret_status,
            "apply_mode": "restart",
            "fastgpt_compatibility_key_configured": bool(values["fastgpt_root_key"]),
            "restart_required": any(
                sources[name]["pending_restart"] for name in [*public_values, *SECRET_NAMES]
            ),
        }

    def snapshot(self) -> dict:
        with self.document.lock:
            return self._snapshot(self.document.read())

    def _prepare(self, original: bytes, request: SaveConnectionsRequest):
        if request.revision != revision_of(original):
            raise ConfigConflict("环境配置文件已被修改，请重新加载模型与连接配置。")
        before, sources = self._state(original)
        changes = {
            name: value
            for name, value in request.values.model_dump().items()
            if value != before[name]
        }
        for name, edit in request.secrets.items():
            if edit.action != "keep":
                value = edit.value.get_secret_value().strip() if edit.action == "set" else ""
                if value != before[name]:
                    changes[name] = value
        if any(not sources[name]["editable"] for name in changes):
            raise ConfigConflict("参数由服务环境变量覆盖，不能通过页面修改。")
        candidate = self.document.render(
            original, {name: quote_value(value) for name, value in changes.items()}
        )
        after, _ = self._state(candidate)
        if any(after[name] != value for name, value in changes.items()):
            raise InvalidEnvFile("无法安全更新连接参数，原文件未被修改。")
        return candidate, after

    def save(self, request: SaveConnectionsRequest) -> dict:
        with self.document.lock:
            original = self.document.read()
            candidate, after = self._prepare(original, request)
            before, _ = self._state(original)
            fields = [
                name
                for name in [*ConnectionValues.model_fields, *SECRET_NAMES]
                if before[name] != after[name]
            ]
            result = self.document.commit(
                original, candidate, write=atomic_write, scope="connections", fields=fields
            )
            return {**self._snapshot(candidate), **result}

    def probe_values(self, request: SaveConnectionsRequest) -> dict:
        with self.document.lock:
            _, values = self._prepare(self.document.read(), request)
            return values


def probe_connection(service: ServiceName, values: dict, *, transport=None) -> dict:
    """Only authentication/list/search checks; never echo upstream bodies or send messages."""

    def result(level: str, message: str) -> dict:
        return {"service": service, "level": level, "message": message}

    def required(*names: str) -> bool:
        return all(values.get(name) for name in names)

    try:
        with httpx.Client(timeout=10.0, follow_redirects=False, transport=transport) as client:
            if service == "llm":
                if not required("llm_api_key", "llm_model"):
                    return result("failed", "请先填写主模型名称和模型 API Key。")
                response = client.get(
                    values["llm_base_url"].rstrip("/") + "/models",
                    headers={"Authorization": f"Bearer {values['llm_api_key']}"},
                )
                if response.status_code == 404:
                    return result(
                        "partial", "服务地址可达，但不支持模型列表接口，尚未验证密钥与模型。"
                    )
                response.raise_for_status()
                data = response.json().get("data")
                if not isinstance(data, list):
                    return result("failed", "模型列表返回格式异常，尚未完成认证验证。")
                names = {
                    item.get("id")
                    for item in data
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
                selected = {values["llm_model"], values["llm_vision_model"] or values["llm_model"]}
                if not selected.issubset(names):
                    return result(
                        "partial", "API 认证成功，部分所填模型未出现在模型列表中，请核对名称。"
                    )
                return result(
                    "passed", "API 认证和模型名称核对通过；本测试不验证对话生成或图像识别效果。"
                )
            if service == "fastgpt":
                token = values["fastgpt_api_key"] or values["fastgpt_root_key"]
                if not token or not values["fastgpt_dataset_id"]:
                    return result("failed", "请先填写知识库 ID 和 FastGPT API Key。")
                base = values["fastgpt_api_url"].rstrip("/")
                if base.endswith("/api"):
                    base = base[:-4]
                response = client.post(
                    base + "/api/core/dataset/searchTest",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "datasetId": values["fastgpt_dataset_id"],
                        "text": "连接测试",
                        "limit": 1,
                        "similarity": 0.5,
                        "searchMode": "embedding",
                    },
                )
                response.raise_for_status()
                data = response.json()
                if data.get("code") != 200 or not isinstance(
                    data.get("data", {}).get("list"), list
                ):
                    return result(
                        "failed", "知识库搜索接口验证失败，请检查密钥、知识库 ID 和权限。"
                    )
                return result(
                    "passed", "知识库搜索接口验证通过；没有检索结果也可以表示接口连接正常。"
                )
            if service == "gewe":
                if not required("gewe_token", "gewe_app_id"):
                    return result("failed", "请先填写 GeWe Token 和实例 App ID。")
                response = client.post(
                    values["gewe_api_url"].rstrip("/") + "/gewe/v2/api/login/checkOnline",
                    headers={"X-GEWE-TOKEN": values["gewe_token"]},
                    json={"appId": values["gewe_app_id"]},
                )
                response.raise_for_status()
                data = response.json()
                if data.get("ret") != 200:
                    return result("failed", "网关状态查询失败，请检查 Token 和实例 ID。")
                if data.get("data") is True:
                    return result("passed", "微信网关可连接，当前机器人实例在线。")
                return result("partial", "微信网关可连接，但当前实例未在线。")
            if not required("lark_app_id", "lark_app_secret"):
                return result("failed", "请先填写飞书应用 ID 和 App Secret。")
            response = client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": values["lark_app_id"], "app_secret": values["lark_app_secret"]},
            )
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 0 or not isinstance(data.get("tenant_access_token"), str):
                return result("failed", "飞书应用凭证验证失败，请检查 ID、Secret 和应用状态。")
            if not values["lark_bitable_app_token"]:
                return result("partial", "应用凭证验证通过；未填写多维表格标识，尚未验证表格访问。")
            token = quote(values["lark_bitable_app_token"], safe="")
            response = client.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{token}/tables",
                headers={"Authorization": f"Bearer {data['tenant_access_token']}"},
                params={"page_size": 1},
            )
            response.raise_for_status()
            if response.json().get("code") != 0:
                return result("failed", "应用凭证有效，但多维表格访问失败，请检查资源标识和授权。")
            return result("passed", "飞书应用凭证与多维表格访问验证通过。")
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        message = (
            "认证或权限失败，请检查凭证。"
            if code in {401, 403}
            else "服务返回异常状态，请核对地址或稍后重试。"
        )
        return result("failed", f"HTTP {code}：{message}")
    except httpx.TimeoutException:
        return result("failed", "连接超时，请检查服务地址和网络。")
    except httpx.HTTPError:
        return result("failed", "连接失败，请检查网络、服务地址或 HTTPS 证书。")
    except (ValueError, TypeError, KeyError, AttributeError):
        return result("failed", "服务返回格式异常，未完成连接验证。")
