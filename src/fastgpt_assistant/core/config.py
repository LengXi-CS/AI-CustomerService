"""Global application settings and configuration loader."""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server settings
    host: str = Field(
        default="0.0.0.0",
        validation_alias=AliasChoices("APP_HOST", "EA_APP_HOST"),
    )
    port: int = Field(
        default=5330,
        validation_alias=AliasChoices("APP_PORT", "EA_APP_PORT"),
    )
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("LOG_LEVEL", "EA_LOG_LEVEL"),
    )

    # GeWe bridge & WeChat settings
    gewe_api_url: str = Field(
        default="http://api.geweapi.com",
        validation_alias=AliasChoices("GEWE_API_URL", "GEWE_BASE_URL", "EA_GEWE_BASE_URL"),
    )
    gewe_token: str = Field(
        default="",
        validation_alias=AliasChoices("GEWE_TOKEN", "EA_GEWE_TOKEN"),
    )
    gewe_app_id: str = Field(
        default="",
        validation_alias=AliasChoices("GEWE_APP_ID", "EA_GEWE_APP_ID"),
    )
    gewe_callback_url: str = Field(
        default="http://127.0.0.1:5330/api/wechat/callback",
        validation_alias=AliasChoices("GEWE_CALLBACK_URL", "EA_GEWE_CALLBACK_URL"),
    )
    gewe_public_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "GEWE_PUBLIC_BASE_URL",
            "PUBLIC_BASE_URL",
            "EA_GEWE_PUBLIC_BASE_URL",
        ),
    )
    wechat_groups_config_path: Path = Field(
        default=Path("config/wechat_groups.toml"),
        validation_alias=AliasChoices(
            "WECHAT_GROUPS_CONFIG_PATH", "EA_WECHAT_GROUPS_CONFIG_PATH"
        ),
    )

    # Provider-neutral chat model settings. The historical DEEPSEEK_* names remain
    # accepted as environment aliases so the running deployment does not break.
    llm_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "LLM_API_KEY",
            "MODEL_API_KEY",
            "DEEPSEEK_API_KEY",
            "EA_DEEPSEEK_API_KEY",
        ),
    )
    llm_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices(
            "LLM_BASE_URL",
            "MODEL_BASE_URL",
            "DEEPSEEK_BASE_URL",
            "EA_DEEPSEEK_BASE_URL",
        ),
    )
    llm_model: str = Field(
        default="deepseek-flash",
        validation_alias=AliasChoices(
            "LLM_MODEL",
            "MODEL_NAME",
            "DEEPSEEK_MODEL",
            "EA_DEEPSEEK_CHAT_MODEL",
        ),
    )
    llm_vision_model: str = Field(
        default="",
        validation_alias=AliasChoices(
            "LLM_VISION_MODEL",
            "MODEL_VISION_NAME",
            "DEEPSEEK_VISION_MODEL",
            "EA_DEEPSEEK_VISION_MODEL",
        ),
    )

    # FastGPT settings
    fastgpt_api_url: str = Field(
        default="http://localhost:3001",
        validation_alias=AliasChoices("FASTGPT_API_URL", "EA_FASTGPT_API_URL"),
    )
    fastgpt_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("FASTGPT_API_KEY", "EA_FASTGPT_API_KEY"),
    )
    fastgpt_root_key: str = Field(
        default="fastgpt-enterprise-root-key",
        validation_alias=AliasChoices("FASTGPT_ROOT_KEY", "EA_FASTGPT_ROOT_KEY"),
    )
    fastgpt_dataset_id: str = Field(
        default="",
        validation_alias=AliasChoices("FASTGPT_DATASET_ID", "EA_FASTGPT_DATASET_ID"),
    )

    # Lark OpenAPI settings (for bitable sync)
    lark_app_id: str = Field(
        default="",
        validation_alias=AliasChoices("LARK_APP_ID", "EA_LARK_APP_ID"),
    )
    lark_app_secret: str = Field(
        default="",
        validation_alias=AliasChoices("LARK_APP_SECRET", "EA_LARK_APP_SECRET"),
    )
    lark_bitable_app_token: str = Field(
        default="",
        validation_alias=AliasChoices("LARK_BITABLE_APP_TOKEN", "EA_LARK_BITABLE_APP_TOKEN"),
    )

    # Storage paths
    db_path: Path = Field(
        default=Path("data/read-model.db"),
        validation_alias=AliasChoices("SQLITE_DB_PATH", "EA_READ_MODEL_DB_PATH"),
    )
    media_dir: Path = Field(
        default=Path("data/resources/attachments"),
        validation_alias=AliasChoices("MEDIA_STORE_DIR", "EA_MEDIA_STORE_DIR"),
    )
    vault_storage_dir: Path = Field(
        default=Path("data/vault"),
        validation_alias=AliasChoices("VAULT_STORAGE_DIR", "EA_VAULT_STORAGE_DIR"),
    )
    vault_secret_key: str = Field(
        default="fastgpt-enterprise-assistant-vault-master-key-32b",
        validation_alias=AliasChoices("VAULT_SECRET_KEY", "EA_WECHAT_PAYLOAD_VAULT_KEY"),
    )
    conversation_state_db_path: Path = Field(
        default=Path("data/conversation-state.db"),
        validation_alias=AliasChoices(
            "CONVERSATION_STATE_DB_PATH", "EA_CONVERSATION_STATE_DB_PATH"
        ),
    )
    chat_log_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHAT_LOG_ENABLED", "EA_CHAT_LOG_ENABLED"),
    )
    chat_log_db_path: Path = Field(
        default=Path("data/chat-log.db"),
        validation_alias=AliasChoices("CHAT_LOG_DB_PATH", "EA_CHAT_LOG_DB_PATH"),
    )
    conversation_quiet_window_seconds: int = Field(
        default=120,
        validation_alias=AliasChoices(
            "CONVERSATION_QUIET_WINDOW_SECONDS", "EA_CONVERSATION_QUIET_WINDOW_SECONDS"
        ),
    )
    conversation_poll_interval_seconds: int = Field(
        default=30,
        validation_alias=AliasChoices(
            "CONVERSATION_POLL_INTERVAL_SECONDS", "EA_CONVERSATION_POLL_INTERVAL_SECONDS"
        ),
    )
    attachment_dispatch_interval_seconds: float = Field(
        default=5.0,
        ge=0,
        validation_alias=AliasChoices(
            "ATTACHMENT_DISPATCH_INTERVAL_SECONDS",
            "EA_ATTACHMENT_DISPATCH_INTERVAL_SECONDS",
        ),
    )
    routing_normal_confidence_threshold: float = Field(
        default=0.80,
        ge=0,
        le=1,
        validation_alias=AliasChoices(
            "ROUTING_NORMAL_CONFIDENCE_THRESHOLD",
            "EA_ROUTING_NORMAL_CONFIDENCE_THRESHOLD",
        ),
    )
    routing_human_confidence_threshold: float = Field(
        default=0.50,
        ge=0,
        le=1,
        validation_alias=AliasChoices(
            "ROUTING_HUMAN_CONFIDENCE_THRESHOLD",
            "EA_ROUTING_HUMAN_CONFIDENCE_THRESHOLD",
        ),
    )
    owner_notify_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "OWNER_NOTIFY_ENABLED", "EA_OWNER_NOTIFY_ENABLED"
        ),
    )


settings = Settings()
