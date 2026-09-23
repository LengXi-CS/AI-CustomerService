"""Non-secret WeChat group allow-list and sender policy configuration."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

GroupMode = Literal["disabled", "shadow", "active"]
SenderRole = Literal["customer", "staff"]


def _text(value: Any) -> str:
    """Normalize a scalar configuration value without exposing raw input."""
    if isinstance(value, str):
        return value.strip()
    return ""


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError("group configuration list fields must be arrays")
    result = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if len(set(result)) != len(result):
        raise ValueError("group configuration list fields cannot contain duplicates")
    return result


@dataclass(frozen=True, slots=True)
class WechatGroupPolicy:
    """Policy for one explicitly configured WeChat group."""

    group_id: str
    group_name: str = ""
    mode: GroupMode = "disabled"
    require_customer_mention: bool = False
    allow_images: bool = True
    allow_files: bool = True
    owner_id: str = ""
    customer_service_ids: tuple[str, ...] = ()
    bot_aliases: tuple[str, ...] = ()
    reply_to_staff_when_mentioned: bool = True

    @classmethod
    def from_mapping(cls, value: Any) -> WechatGroupPolicy:
        if not isinstance(value, dict):
            raise ValueError("each group configuration must be an object")
        group_id = _text(value.get("group_id"))
        if not group_id:
            raise ValueError("group_id must be non-empty")
        mode = _text(value.get("mode")) or "disabled"
        if mode not in {"disabled", "shadow", "active"}:
            raise ValueError("group mode must be disabled, shadow, or active")
        owner_id = _text(value.get("owner_id"))
        service_ids = _string_tuple(value.get("customer_service_ids"))
        if owner_id and owner_id not in service_ids:
            service_ids = (owner_id, *service_ids)
        return cls(
            group_id=group_id,
            group_name=_text(value.get("group_name")),
            mode=mode,
            require_customer_mention=bool(value.get("require_customer_mention", False)),
            allow_images=bool(value.get("allow_images", True)),
            allow_files=bool(value.get("allow_files", True)),
            owner_id=owner_id,
            customer_service_ids=service_ids,
            bot_aliases=_string_tuple(value.get("bot_aliases")),
            reply_to_staff_when_mentioned=bool(
                value.get("reply_to_staff_when_mentioned", True)
            ),
        )

    def sender_role(self, sender_id: str) -> SenderRole:
        return "staff" if sender_id.strip() in self.customer_service_ids else "customer"


@dataclass(frozen=True, slots=True)
class WechatGroupsConfig:
    """Global gate plus an explicit group policy map."""

    enabled: bool = False
    default_reply_to_staff_when_mentioned: bool = True
    groups: dict[str, WechatGroupPolicy] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "groups", dict(self.groups or {}))

    @classmethod
    def from_mapping(cls, value: Any) -> WechatGroupsConfig:
        if not isinstance(value, dict):
            raise ValueError("WeChat groups configuration must be an object")
        global_config = value.get("global", {})
        if not isinstance(global_config, dict):
            raise ValueError("WeChat groups global configuration must be an object")
        raw_groups = value.get("groups", [])
        if not isinstance(raw_groups, list):
            raise ValueError("groups must be an array")
        policies = [WechatGroupPolicy.from_mapping(item) for item in raw_groups]
        group_map = {policy.group_id: policy for policy in policies}
        if len(group_map) != len(policies):
            raise ValueError("groups cannot contain duplicate group_id values")
        return cls(
            enabled=bool(global_config.get("enabled", False)),
            default_reply_to_staff_when_mentioned=bool(
                global_config.get("reply_to_staff_when_mentioned", True)
            ),
            groups=group_map,
        )

    @classmethod
    def load(cls, path: Path) -> WechatGroupsConfig:
        """Load a non-secret TOML file; a missing file fails closed for groups."""
        if not path.exists():
            return cls()
        with path.open("rb") as handle:
            return cls.from_mapping(tomllib.load(handle))

    def get(self, group_id: str) -> WechatGroupPolicy | None:
        return self.groups.get(group_id.strip())

    def role_for(self, group_id: str, sender_id: str) -> SenderRole:
        policy = self.get(group_id)
        return policy.sender_role(sender_id) if policy else "customer"
