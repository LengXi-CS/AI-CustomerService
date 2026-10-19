"""Non-secret WeChat group allow-list and sender policy configuration."""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

GroupMode = Literal["disabled", "shadow", "active"]
SenderRole = Literal["customer", "staff"]

# 安抚话术的两种来源：
#   model —— 每个非标准需求由模型现写一句（默认，措辞不重复、更像真人）；
#   fixed —— 所有非标准需求统一使用一句固定文案，可控、可审计、不耗模型调用。
SootheMode = Literal["model", "fixed"]
DEFAULT_FIXED_SOOTHE_REPLY = "收到，我们加急处理"


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
    native_quote_replies: bool = False
    associate_customer_demands: bool = False
    conversation_quiet_window_seconds: int | None = None

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
        wait_seconds = value.get("conversation_quiet_window_seconds")
        if wait_seconds is not None and (
            isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int)
            or not 0 <= wait_seconds <= 86400
        ):
            raise ValueError("group wait time must be an integer between 0 and 86400")
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
            native_quote_replies=bool(value.get("native_quote_replies", False)),
            associate_customer_demands=bool(value.get("associate_customer_demands", False)),
            conversation_quiet_window_seconds=wait_seconds,
        )

    def sender_role(self, sender_id: str) -> SenderRole:
        return "staff" if sender_id.strip() in self.customer_service_ids else "customer"


@dataclass(frozen=True, slots=True)
class WechatGroupsConfig:
    """Global gate plus an explicit group policy map."""

    enabled: bool = False
    default_reply_to_staff_when_mentioned: bool = True
    soothe_mode: SootheMode = "model"
    soothe_fixed_reply: str = DEFAULT_FIXED_SOOTHE_REPLY
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
        soothe_mode = _text(global_config.get("soothe_mode")) or "model"
        if soothe_mode not in {"model", "fixed"}:
            raise ValueError("soothe_mode must be model or fixed")
        policies = [WechatGroupPolicy.from_mapping(item) for item in raw_groups]
        group_map = {policy.group_id: policy for policy in policies}
        if len(group_map) != len(policies):
            raise ValueError("groups cannot contain duplicate group_id values")
        return cls(
            enabled=bool(global_config.get("enabled", False)),
            default_reply_to_staff_when_mentioned=bool(
                global_config.get("reply_to_staff_when_mentioned", True)
            ),
            soothe_mode=soothe_mode,
            soothe_fixed_reply=(
                _text(global_config.get("soothe_fixed_reply")) or DEFAULT_FIXED_SOOTHE_REPLY
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


class WechatGroupsProvider:
    """File-backed group policy source that reloads when the TOML file changes.

    Operators add customer groups regularly; requiring a service restart for a
    pure data edit is both slow and risky (restarts re-read the public callback
    origin). This provider re-reads the file whenever its ``(mtime_ns, size)``
    stamp moves, so an edit takes effect on the next message.

    Failures never take the running service down:

    * a malformed or unreadable file keeps the **last known good** policy set
      in effect and logs the problem once per distinct file state;
    * a missing file degrades to the same fail-closed default as
      :meth:`WechatGroupsConfig.load` (group handling disabled).

    The stamp is always recorded, including on failure, so a broken file does
    not re-parse (and re-log) on every single message.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._stamp: tuple[int, int] | None = None
        self._loaded = False
        self._config = WechatGroupsConfig()

    @property
    def path(self) -> Path:
        return self._path

    def _stamp_of_path(self) -> tuple[int, int] | None:
        """Return the file's ``(mtime_ns, size)``, or ``None`` when absent."""
        try:
            status = self._path.stat()
        except OSError:
            return None
        return (status.st_mtime_ns, status.st_size)

    def current(self) -> WechatGroupsConfig:
        """Return the active configuration, reloading only when the file moved."""
        stamp = self._stamp_of_path()
        if self._loaded and stamp == self._stamp:
            return self._config
        try:
            config = WechatGroupsConfig.load(self._path)
        except Exception:
            logger.exception(
                "WeChat group configuration at %s is invalid; keeping the previous "
                "%d configured group(s) until the file changes again",
                self._path,
                len(self._config.groups or {}),
            )
            self._stamp = stamp
            self._loaded = True
            return self._config
        logger.info(
            "Loaded WeChat group configuration from %s: enabled=%s, %d group(s)%s",
            self._path,
            config.enabled,
            len(config.groups or {}),
            "" if self._loaded else " (initial load)",
        )
        self._config = config
        self._stamp = stamp
        self._loaded = True
        return self._config
