"""Orchestration engine for enterprise customer service."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from fastgpt_assistant.adapters.fastgpt.client import FastGptClient
from fastgpt_assistant.adapters.llm.chat_model import (
    ChatModelClient,
    MultimodalImage,
    MultimodalTaskInput,
    QueryPlanUnavailable,
    clean_wechat_text,
)
from fastgpt_assistant.adapters.wechat.client import GeweError, WechatGeweClient
from fastgpt_assistant.core.chat_log import ChatLogEntry, ChatLogStore, message_kind
from fastgpt_assistant.core.config import Settings
from fastgpt_assistant.core.media_store import MediaStore
from fastgpt_assistant.core.models import Deliverable, ExecutionResult
from fastgpt_assistant.core.query_engine import (
    PLAN_REJECTION_REASONS,
    ReadModel,
    SqliteQueryExecutor,
)
from fastgpt_assistant.core.schema import QueryClarification, QueryEntity, QueryPlan
from fastgpt_assistant.core.wechat_groups import WechatGroupsConfig, WechatGroupsProvider
from fastgpt_assistant.core.wechat_quote import TextQuote
from fastgpt_assistant.orchestration.context_buffer import ContextBuffer
from fastgpt_assistant.orchestration.conversation_state import (
    ConversationStateStore,
    ConversationTask,
    EventReceipt,
    iso_timestamp,
)
from fastgpt_assistant.orchestration.demand_association import (
    ASSOCIATION_WINDOW_SECONDS,
    association_text,
    order_ids,
    validated_links,
)
from fastgpt_assistant.orchestration.demand_splitter import (
    split_and_merge_demands,
    split_message_demands,
    validate_model_demands,
)
from fastgpt_assistant.orchestration.group_context import GroupContextWindow
from fastgpt_assistant.orchestration.inbound import (
    extract_quote_payload,
    is_bot_mentioned,
    mention_names,
    normalize_media_xml,
    revoked_message_id,
    scalar_text,
    sender_display_name,
    split_group_sender,
    strip_group_mention,
)
from fastgpt_assistant.orchestration.inbound_media_store import InboundMediaStore
from fastgpt_assistant.orchestration.payload_vault import (
    PayloadVault,
    PayloadVaultCorruption,
    PayloadVaultKeyError,
)
from fastgpt_assistant.orchestration.query_routing import (
    NON_STANDARD_SOOTHE_REPLY,
    decide_query_route,
)
from fastgpt_assistant.orchestration.visual_candidates import (
    VisualSkuResolution,
    is_image_dependent_query,
    is_multi_visual_query,
    is_report_query,
    planner_safe_analysis,
    resolve_visual_skus,
)

logger = logging.getLogger(__name__)

FIELD_LABELS: dict[str, str] = {
    "factory_id": "工厂ID",
    "factory_name": "工厂名称",
    "sku_code": "SKU编码",
    "product_name": "商品名称",
    "trademark_code": "商标编码",
    "trademark_name": "商标名称",
    "category_id": "品类ID",
    "category_name": "品类名称",
    "product_size": "产品规格/尺寸",
    "batch_no": "批次号",
    "report_name": "报告名称",
    "factory_business_licenses": "营业执照",
    "factory_medical_certificates": "医护级认证",
    "factory_medical_certificate_standards": "医护级认证技术标准",
    "factory_special_care_certificates": "特护级认证",
    "factory_special_care_certificate_standards": "特护级认证技术标准",
    "factory_antibacterial_certificates": "抗菌产品及中卫安认证",
    "third_party_reports": "第三方检测报告",
    "product_documents": "产品资料",
    "product_standards": "产品技术标准",
    "production_licenses": "生产许可证",
    "trademark_certificates": "商标注册证",
    "sku_main_image_details": "主图详情",
    "sku_product_front": "产品正面图",
    "sku_product_back_label": "产品背面标签",
    "batch_report_files": "批次检测报告",
}

SCOPE_LABELS: dict[str, str] = {
    "factory": "工厂信息",
    "sku": "商品信息",
    "category": "品类信息",
    "trademark": "商标信息",
    "batch_report": "批次报告",
}

_MAX_BUFFERED_IMAGE_BYTES = 8 * 1024 * 1024

# Attachment suffixes GeWe must receive through ``sendImage`` rather than
# ``sendFile``. Shared by the dispatcher and the chat archive so a logged image
# row and an actually-sent image cannot disagree.
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})


def _detect_image_mime_type(image_data: bytes) -> str:
    """Detect supported image types from file signatures, not user-controlled names."""
    if image_data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(image_data) >= 12 and image_data[:4] == b"RIFF" and image_data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _public_base_url(callback_url: str) -> str:
    """Return the public origin used to build media proxy URLs."""
    parsed = urlsplit(callback_url.strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    if parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


@dataclass(slots=True)
class InboundEvent:
    """Normalized inbound message from WeChat."""

    message_id: str
    from_user: str
    to_user: str
    chat_id: str
    user_id: str
    msg_type: int
    text: str = ""
    is_image: bool = False
    image_url: str = ""
    image_data: bytes | None = None
    file_name: str = ""
    sender_id: str = ""
    sender_role: Literal["customer", "staff"] = "customer"
    sender_name: str = ""
    is_group: bool = False
    mentioned_bot: bool = False
    # Names the message @-mentioned, captured before ``strip_group_mention``
    # deletes the leading token: the chat archive needs them to replay a bubble
    # the way it was actually written.
    mentions: tuple[str, ...] = ()
    visual_sku_code: str = ""
    quoted_text: str = ""
    quoted_type: str = ""
    quoted_media_xml: str = ""
    quoted_msg_id: str = ""
    conversation_task_id: str = ""
    reply_source: TextQuote | None = None
    related_task_ids: tuple[str, ...] = ()
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GroupEventRoute:
    """Authorization decision for a configured group event."""

    allowed: bool
    shadow: bool = False
    reason: str = ""


@dataclass(slots=True)
class ReplyDeliverable:
    """Outbound reply message containing text and optional media attachments."""

    text: str
    deliverables: list[Deliverable] = field(default_factory=list)
    channel_used: str = "direct"
    plan: QueryPlan | None = None
    suppress_outbound: bool = False
    owner_notify_text: str = ""
    reply_source: TextQuote | None = None


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """What one :meth:`AssistantEngine.dispatch_outbound` call actually delivered.

    ``dispatch_outbound`` never raises — each send is individually best-effort —
    so "the call returned" says nothing about whether the customer saw anything.
    The group chat archive must not record bubbles the group never received,
    hence this honest receipt: ``sent_text`` is True only when the text send did
    not raise, and ``sent_media_ids`` holds exactly the attachments whose send
    did not raise. ``suppressed`` marks a reply that was intentionally not sent.
    """

    sent_text: bool = False
    sent_media_ids: tuple[str, ...] = ()
    suppressed: bool = False
    text_content: str = ""
    message_id: str = ""
    text_quote: TextQuote | None = None
    text_status: str = "not_attempted"


def dedupe_deliverables(deliverables: list[Deliverable]) -> list[Deliverable]:
    """Drop repeated attachments, keeping first-seen order.

    Two SKUs of the same category resolve to the same report file; without this
    the identical PDF is posted once per SKU and the copies contend for upload
    bandwidth. Entries carrying neither a media_id nor a download_url are kept
    as-is so the caller can still report them as undeliverable.
    """
    seen: set[str] = set()
    unique: list[Deliverable] = []
    for deliverable in deliverables:
        key = deliverable.media_id or deliverable.download_url
        if key:
            if key in seen:
                continue
            seen.add(key)
        unique.append(deliverable)
    return unique


def dispatch_interval_seconds(engine: Any) -> float:
    """Read the inter-attachment pause, tolerating partially built engines."""
    settings = getattr(engine, "settings", None)
    raw = getattr(settings, "attachment_dispatch_interval_seconds", 0.0)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def event_occurred_at(event: InboundEvent) -> str | datetime:
    """Return the moment an inbound event happened.

    GeWe does not populate ``received_at`` on its callbacks, so this normally
    resolves to arrival time. It stays a helper so the chat archive and the
    conversation-state store always agree on the same instant for one event.
    """
    received_at = event.raw_payload.get("received_at")
    if isinstance(received_at, str) and received_at.strip():
        return received_at
    return datetime.now(UTC)


_DEFAULT_CLARIFY_QUESTION = "请问您想查询哪个品牌或产品的具体信息？"
_CLARIFY_QUERY_MAX_CHARS = 240

# 资料不足文案的兜底。与安抚文案同理：正常走模型生成，常量只在模型不可用时兜底。
# 只陈述"没查到 + 已转负责人"，绝不暗示"该记录不存在"。
INSUFFICIENT_DATA_FALLBACK_REPLY = (
    "抱歉，这份资料我这边暂时没有查到，已经同步给负责人帮您确认，稍后给您答复。"
)


def clarify_question(plan: QueryPlan, default: str = _DEFAULT_CLARIFY_QUESTION) -> str:
    """Read a plan's clarification question, tolerating an unvalidated payload.

    ``QueryPlan.model_copy(update=...)`` skips validation, so a plan built inside
    the engine can still carry ``clarification`` as a plain mapping rather than a
    ``QueryClarification``. Both shapes must be readable here.
    """
    clarification = plan.clarification
    question = (
        clarification.get("question")
        if isinstance(clarification, dict)
        else getattr(clarification, "question", None)
    )
    return str(question).strip() if question else default


def clarification_reply(user_query: str, question: str) -> str:
    """Keep the customer's original question visible in a clarification reply."""
    query = user_query.strip()
    prompt = question.strip() or _DEFAULT_CLARIFY_QUESTION
    if not query or query in prompt:
        return prompt
    if len(query) > _CLARIFY_QUERY_MAX_CHARS:
        query = f"{query[:_CLARIFY_QUERY_MAX_CHARS]}…"
    return f'关于您刚才提到的“{query}”，{prompt}'


_OWNER_NOTIFY_QUERY_MAX_CHARS = 200

# 非标准需求不走群内 @（GeWe 客户端没有 at-list 能力），而是私聊群负责人。
_OWNER_NOTIFY_TEMPLATES: dict[str, str] = {
    "non_standard": (
        "【AI 客服｜非标准需求转人工】\n"
        "群：{chat_label}\n"
        "客户：{sender_id}\n"
        "需求原文：{query}\n"
        "处理：未命中标准意图，已先安抚客户、未向客户反问，请负责人跟进确认。"
    ),
    "no_evidence": (
        "【AI 客服｜资料不足需人工跟进】\n"
        "群：{chat_label}\n"
        "客户：{sender_id}\n"
        "需求原文：{query}\n"
        "处理：企业档案中未检索到对应记录，已告知客户稍后答复，请负责人跟进确认。"
    ),
    "unanswerable": (
        "【AI 客服｜检索未完成需人工跟进】\n"
        "群：{chat_label}\n"
        "客户：{sender_id}\n"
        "需求原文：{query}\n"
        "处理：系统未能按现有信息完成本次检索（属程序侧问题，非客户问题），"
        "已告知客户稍后答复，请负责人跟进并留意是否需要在群里补充说明。"
    ),
}

_OWNER_NOTIFY_REASONS = frozenset(_OWNER_NOTIFY_TEMPLATES)


def owner_notice_reason_for_failure(exec_result: ExecutionResult) -> str:
    """Classify an unanswerable structured lookup; "" means "do not hand off".

    Three failure shapes reach the engine, and they need different owner wording:

    * ``no_evidence`` — the archive genuinely has no matching record;
    * ``error`` — the executor failed, i.e. a program-side problem, not a data gap;
    * ``clarify`` — **the trap**: this status is shared by two very different
      payloads. The planner's own clarification question is written for the
      customer and may be forwarded, but a ``QueryPlanRejected`` message is
      internal validation text ("file 和 validity 操作必须选择一个已注册的附件
      字段。") and must never reach a customer's bubble. Only the rejection
      reasons are diverted; anything else keeps the previous forwarding behavior.
    """
    if exec_result.status == "no_evidence":
        return "no_evidence"
    if exec_result.status == "error":
        return "unanswerable"
    if exec_result.status == "clarify":
        clarify_meta = exec_result.metadata.get("clarify")
        reason = (
            str(clarify_meta.get("reason") or "")
            if isinstance(clarify_meta, dict)
            else ""
        )
        if reason in PLAN_REJECTION_REASONS:
            return "unanswerable"
    return ""


def build_owner_notice(
    groups_config: WechatGroupsConfig,
    *,
    chat_id: str,
    sender_id: str,
    user_query: str,
    reason: str = "non_standard",
) -> str:
    """Compose the private reminder text sent to a group's owner.

    Returns an empty string when the chat is not a configured group or has no
    ``owner_id``, so callers can treat "no reminder needed" and "no owner
    configured" identically. The group is addressed by its configured name
    (falling back to the room id) and the customer by wxid; no display name is
    ever recorded.

    ``reason`` selects the template: a non-standard demand (soothed, never
    questioned) versus a standard demand the archive could not answer.
    """
    if not chat_id.endswith("@chatroom"):
        return ""
    policy = groups_config.get(chat_id)
    if policy is None or not policy.owner_id:
        return ""
    template = _OWNER_NOTIFY_TEMPLATES.get(reason)
    if template is None:
        raise ValueError(
            f"unknown owner notice reason {reason!r}; expected one of "
            f"{sorted(_OWNER_NOTIFY_REASONS)}"
        )
    query = user_query.strip()
    if len(query) > _OWNER_NOTIFY_QUERY_MAX_CHARS:
        query = query[:_OWNER_NOTIFY_QUERY_MAX_CHARS] + "…"
    return template.format(
        chat_label=policy.group_name or chat_id,
        sender_id=sender_id,
        query=query,
    )


class AssistantEngine:
    """Central orchestration engine handling inbound WeChat messages and routing."""

    def __init__(
        self,
        settings: Settings,
        *,
        read_model: ReadModel | None = None,
        media_store: MediaStore | None = None,
        context_buffer: ContextBuffer | None = None,
        groups_config: WechatGroupsConfig | None = None,
        chat_log: ChatLogStore | None = None,
    ) -> None:
        self.settings = settings
        self.media_store = media_store or MediaStore(resources_root=settings.media_dir)
        self.read_model = read_model or ReadModel(database_path=settings.db_path)
        self.query_executor = SqliteQueryExecutor(
            self.read_model,
            media_store=self.media_store,
            media_base_url=(
                _public_base_url(settings.gewe_public_base_url)
                or _public_base_url(settings.gewe_callback_url)
            ),
        )
        self.context_buffer = context_buffer or ContextBuffer(
            max_media_items=60,
        )
        self.inbound_media_store = InboundMediaStore(
            settings.vault_storage_dir / "inbound-media",
            settings.vault_storage_dir / "inbound-media-index.db",
        )
        self.conversation_state = ConversationStateStore(settings.conversation_state_db_path)
        self.payload_vault: PayloadVault | None = None
        try:
            self.payload_vault = PayloadVault(
                key=settings.vault_secret_key,
                db_path=settings.vault_storage_dir / "payload-vault.db",
            )
        except PayloadVaultKeyError:
            logger.error("Payload vault key is unavailable; delayed group replies are disabled")
        self._groups_provider = WechatGroupsProvider(settings.wechat_groups_config_path)
        self._groups_override: WechatGroupsConfig | None = groups_config
        self._group_context_windows: dict[str, GroupContextWindow] = {}
        # Archiving group history must never be able to stop the service from
        # booting, so an unusable database degrades to "logging off" instead of
        # raising out of __init__.
        self.chat_log: ChatLogStore | None = None
        try:
            self.chat_log = chat_log or ChatLogStore(
                db_path=settings.chat_log_db_path,
                enabled=settings.chat_log_enabled,
            )
        except Exception:
            logger.exception(
                "Chat log store is unavailable; configured-group history will not be archived"
            )

        self.model_client = ChatModelClient(
            api_key=settings.llm_api_key or "placeholder",
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            vision_model=settings.llm_vision_model or None,
        )
        self.fastgpt = FastGptClient(
            base_url=settings.fastgpt_api_url,
            api_key=settings.fastgpt_api_key or settings.fastgpt_root_key,
        )
        self.wechat = WechatGeweClient(
            base_url=settings.gewe_api_url,
            token=settings.gewe_token,
            app_id=settings.gewe_app_id,
        )

    @property
    def groups_config(self) -> WechatGroupsConfig:
        """Active group policy, re-read from disk when the TOML file changes.

        Adding or editing a customer group in ``config/wechat_groups.toml``
        takes effect on the next message — no service restart needed. Assigning
        to this attribute pins a fixed configuration instead, which is what
        tests and callers that supply their own policy set rely on.
        """
        override = self._groups_override
        if override is not None:
            return override
        return self._groups_provider.current()

    @groups_config.setter
    def groups_config(self, value: WechatGroupsConfig) -> None:
        self._groups_override = value

    def observe_public_origin(self, *, host: str, proto: str) -> None:
        """Use a trusted reverse-proxy origin for immediate attachment delivery."""
        forwarded_host = next((item.strip() for item in host.split(",") if item.strip()), "")
        forwarded_proto = next((item.strip() for item in proto.split(",") if item.strip()), "")
        if not forwarded_host or forwarded_proto not in {"http", "https"}:
            return
        if forwarded_host.startswith(("127.", "localhost", "0.0.0.0", "[::1]")):
            return
        origin = _public_base_url(f"{forwarded_proto}://{forwarded_host}")
        if origin:
            self.query_executor.media_base_url = origin

    def _group_context_for(self, chat_id: str) -> GroupContextWindow | None:
        """Return the encrypted rolling context for one configured group."""
        vault = getattr(self, "payload_vault", None)
        if vault is None or not chat_id.endswith("@chatroom"):
            return None
        windows = getattr(self, "_group_context_windows", None)
        if windows is None:
            windows = {}
            self._group_context_windows = windows
        context = windows.get(chat_id)
        if context is None:
            context = GroupContextWindow(
                vault,
                bot_id=self.settings.gewe_app_id or "fastgpt-bot",
                group_id=chat_id,
            )
            windows[chat_id] = context
        return context

    def record_group_context_event(self, event: InboundEvent) -> None:
        """Persist one safe group message in the rolling 60-message window."""
        context = self._group_context_for(event.chat_id)
        if context is None:
            return
        received_at = event.raw_payload.get("received_at")
        if not isinstance(received_at, str) or not received_at.strip():
            received_at = datetime.now(UTC).isoformat()
        text = event.text.strip()
        if not text:
            text = "[图片消息]" if event.is_image else f"[message-type:{event.msg_type}]"
        context.append(
            message_id=event.message_id,
            text=text,
            received_at=received_at,
            actor_id=event.sender_id or "unknown",
            reply_to_message_id=event.quoted_msg_id,
            quoted_text=event.quoted_text,
        )
        media_store = getattr(self, "inbound_media_store", None)
        if media_store is not None:
            media_store.retain_message_ids(
                chat_id=event.chat_id,
                message_ids={entry["message_id"] for entry in context.read()},
            )

    def log_group_inbound(self, event: InboundEvent) -> None:
        """Archive one inbound message of a configured group. Never raises.

        Called from the webhook **before** routing on purpose: a message the
        policy then drops (客服没 @机器人、客户没按要求的 @) is still part of the
        group's history, and "为什么这条没回" is answered by exactly those rows.
        Only configured groups are written — a private chat or an unknown group
        is left alone, which is what keeps the archive bounded to the groups the
        owner explicitly chose to run.
        """
        store = getattr(self, "chat_log", None)
        if store is None or not store.enabled or not event.is_group:
            return
        policy = self.groups_config.get(event.chat_id)
        if policy is None:
            return
        try:
            # Keep native quote envelopes for protocol comparisons. Parsed
            # summaries discard fields needed by WeChat/WeCom interoperability.
            # These bodies belong in the existing plaintext chat archive, never
            # in the metadata-only conversation state database.
            message_data = event.raw_payload.get("Data", {})
            quote_source = {}
            if event.quoted_msg_id and isinstance(message_data, dict):
                quote_source = {
                    "raw_quote_xml": scalar_text(message_data.get("Content")),
                    "raw_quote_msg_source": scalar_text(message_data.get("MsgSource")),
                }
            store.record(
                ChatLogEntry(
                    chat_id=event.chat_id,
                    chat_name=policy.group_name,
                    direction="inbound",
                    sender_id=event.sender_id or event.from_user,
                    sender_name=event.sender_name,
                    sender_role="staff" if event.sender_role == "staff" else "customer",
                    kind=message_kind(
                        event.msg_type,
                        is_image=event.is_image,
                        has_text=bool(event.text.strip()),
                    ),
                    msg_type=event.msg_type,
                    text=event.text,
                    message_id=event.message_id,
                    quoted_msg_id=event.quoted_msg_id,
                    quoted_type=event.quoted_type,
                    quoted_text=event.quoted_text,
                    # The same deterministic handle ``process_image`` stages the
                    # bytes under, so a row can be traced back to the file.
                    media_ref=f"inm_{event.message_id}" if event.is_image else "",
                    metadata={
                        "is_group": True,
                        "mentioned_bot": event.mentioned_bot,
                        "mentions": list(event.mentions),
                        # Needed to re-download the *quoted* image/file later; it
                        # is bulkier than the rest and rarely rendered, so it
                        # lives here instead of in a dedicated column.
                        "quoted_media_xml": event.quoted_media_xml,
                        "reply_source": self.reply_source_for_event(event).as_dict(),
                        **quote_source,
                    },
                    occurred_at=event_occurred_at(event),
                )
            )
        except Exception:
            logger.exception(
                "Unable to archive inbound group message chat=%s message=%s",
                event.chat_id,
                event.message_id,
            )

    def log_group_outbound(
        self,
        *,
        to_wxid: str,
        reply: ReplyDeliverable,
        task_id: str = "",
        outcome: DispatchOutcome | None = None,
    ) -> None:
        """Archive what the bot actually sent into a configured group.

        Only called for a configured group after a dispatch, and — when the
        caller passes the :class:`DispatchOutcome` — only the pieces that
        dispatch actually handed to GeWe are written: a text send that raised,
        or an attachment with no public URL, leaves **no** row. A shadow-mode
        reply was never sent and a private owner reminder belongs to a
        different chat, so neither appears here either. The archive therefore
        stays a record of the group's real conversation rather than of what
        the bot intended.

        ``outcome=None`` means the caller did not track the dispatch (a test
        stub, for instance); in that case every part of the reply is recorded,
        matching the behaviour this method had before outcomes existed.
        """
        state_store = getattr(self, "conversation_state", None)
        if (
            state_store is not None and outcome is not None and not outcome.suppressed
            and to_wxid.endswith("@chatroom") and self.groups_config.get(to_wxid)
        ):
            try:
                state_store.record_delivery_attempt(
                    task_id=task_id, chat_id=to_wxid,
                    text_status="sent" if outcome.sent_text else outcome.text_status,
                    text_format="native_quote" if outcome.text_quote else "plain",
                    message_id=outcome.message_id,
                    reply_to_message_id=reply.reply_source.message_id
                    if reply.reply_source else "",
                )
            except Exception:
                logger.exception("Unable to persist delivery receipt chat=%s", to_wxid)
        store = getattr(self, "chat_log", None)
        if store is None or not store.enabled or not to_wxid.endswith("@chatroom"):
            return
        policy = self.groups_config.get(to_wxid)
        if policy is None:
            return

        try:
            occurred_at = datetime.now(UTC)
            # getattr, not attribute access: several suites build a partial engine
            # with a minimal settings namespace, and archiving must never be the
            # thing that breaks a working reply.
            sender_id = getattr(self.settings, "gewe_app_id", "") or "assistant"
            entries: list[ChatLogEntry] = []
            if reply.text.strip() and (outcome is None or outcome.sent_text):
                entries.append(
                    ChatLogEntry(
                        chat_id=to_wxid,
                        chat_name=policy.group_name,
                        direction="outbound",
                        sender_id=sender_id,
                        sender_role="assistant",
                        kind="text",
                        text=outcome.text_content
                        if outcome and outcome.text_content else reply.text,
                        message_id=outcome.message_id if outcome else "",
                        msg_type=49 if outcome and outcome.text_quote else 0,
                        quoted_msg_id=outcome.text_quote.message_id
                        if outcome and outcome.text_quote else "",
                        quoted_type="1" if outcome and outcome.text_quote else "",
                        quoted_text=outcome.text_quote.content
                        if outcome and outcome.text_quote else "",
                        channel_used=reply.channel_used,
                        task_id=task_id,
                        metadata={
                            "is_group": True, "mentions": [], "mentioned_bot": False,
                            "text_format": "native_quote" if outcome and outcome.text_quote
                            else "plain",
                            "text_status": outcome.text_status if outcome else "sent",
                        },
                        occurred_at=occurred_at,
                    )
                )
            for deliverable in dedupe_deliverables(reply.deliverables):
                if outcome is not None and deliverable.media_id not in outcome.sent_media_ids:
                    continue
                entries.append(
                    ChatLogEntry(
                        chat_id=to_wxid,
                        chat_name=policy.group_name,
                        direction="outbound",
                        sender_id=sender_id,
                        sender_role="assistant",
                        kind=(
                            "image"
                            if Path(deliverable.filename).suffix.lower() in _IMAGE_SUFFIXES
                            else "file"
                        ),
                        media_ref=deliverable.media_id,
                        media_name=deliverable.filename,
                        media_mime=deliverable.mime_type,
                        media_size=deliverable.size_bytes or 0,
                        channel_used=reply.channel_used,
                        task_id=task_id,
                        metadata={"is_group": True, "mentions": [], "mentioned_bot": False},
                        occurred_at=occurred_at,
                    )
                )
            for entry in entries:
                store.record(entry)
        except Exception:
            logger.exception("Unable to archive outbound group message chat=%s", to_wxid)

    def parse_gewe_callback(self, data: dict[str, Any]) -> InboundEvent | None:
        """Parse raw GeWe webhook callback into a normalized InboundEvent."""
        type_name = data.get("TypeName", "")
        if type_name != "AddMsg":
            return None

        msg_data = data.get("Data", {})
        if not isinstance(msg_data, dict):
            return None

        msg_id = str(msg_data.get("NewMsgId") or msg_data.get("MsgId") or "")
        msg_type_raw = msg_data.get("MsgType", 0)
        msg_type = int(msg_type_raw) if isinstance(msg_type_raw, (int, str)) and str(
            msg_type_raw
        ).isdigit() else 0

        from_user_obj = msg_data.get("FromUserName", {})
        from_user = from_user_obj.get("string", "") if isinstance(from_user_obj, dict) else str(
            from_user_obj
        )

        to_user_obj = msg_data.get("ToUserName", {})
        to_user = to_user_obj.get("string", "") if isinstance(to_user_obj, dict) else str(
            to_user_obj
        )

        content_text = scalar_text(msg_data.get("Content"))
        is_group = from_user.endswith("@chatroom")
        sender_id = scalar_text(
            msg_data.get("ActualUserName")
            or msg_data.get("ActualUser")
            or msg_data.get("FromUserName")
        )
        if is_group:
            prefixed_sender, group_text = split_group_sender(content_text)
            sender_id = scalar_text(
                msg_data.get("ActualUserName")
                or msg_data.get("ActualUser")
                or prefixed_sender
            ) or "unknown_member"
            content_text = group_text

        clean_text, quoted_text, quoted_type, quoted_media_xml, quoted_msg_id = (
            extract_quote_payload(content_text)
        )
        content_text = clean_text

        group_policy = self.groups_config.get(from_user) if is_group else None
        sender_role: Literal["customer", "staff"] = (
            group_policy.sender_role(sender_id) if group_policy else "customer"
        )
        mentioned_bot = is_bot_mentioned(
            msg_data,
            content_text,
            bot_id=self.settings.gewe_app_id,
            aliases=group_policy.bot_aliases if group_policy else (),
        )
        if is_group:
            mentions = mention_names(msg_data, content_text)
            content_text = strip_group_mention(
                content_text,
                aliases=group_policy.bot_aliases if group_policy else (),
            )
        else:
            mentions = ()

        # Ignore self-sent messages, including a bot sender embedded in a group payload.
        if self.settings.gewe_app_id and sender_id == self.settings.gewe_app_id:
            return None

        is_image = msg_type == 3
        reply_source = TextQuote.from_callback(
            data=msg_data, message_id=msg_id, chat_id=from_user, sender_id=sender_id,
            content=clean_text if msg_type == 1 else (content_text if not is_image else ""),
            msg_type=msg_type, sender_name=sender_display_name(msg_data),
        )
        return InboundEvent(
            message_id=msg_id,
            from_user=from_user,
            to_user=to_user,
            chat_id=from_user,
            user_id=sender_id,
            msg_type=msg_type,
            text=content_text if not is_image else "",
            is_image=is_image,
            sender_id=sender_id,
            sender_role=sender_role,
            sender_name=sender_display_name(msg_data),
            is_group=is_group,
            mentioned_bot=mentioned_bot,
            mentions=mentions,
            quoted_text=quoted_text,
            quoted_type=quoted_type,
            quoted_media_xml=quoted_media_xml,
            quoted_msg_id=quoted_msg_id,
            reply_source=reply_source,
            raw_payload=data,
        )

    def revoke_group_message(self, event: InboundEvent) -> tuple[str, ...]:
        """Cancel queued work for a message revoked in a group."""
        original_id = revoked_message_id(event.text)
        if not original_id:
            return ()
        task_ids = self.conversation_state.close_revoked_message(
            chat_id=event.chat_id, external_event_id=original_id
        )
        logger.info(
            "Group message revoked chat=%s original_event=%s cancelled_tasks=%s",
            event.chat_id,
            original_id,
            task_ids,
        )
        return task_ids

    def route_group_event(self, event: InboundEvent) -> GroupEventRoute:
        """Apply the explicit group allow-list and sender policy."""
        if not event.is_group:
            return GroupEventRoute(True, reason="private_chat")
        if not self.groups_config.enabled:
            return GroupEventRoute(False, reason="group_gate_disabled")
        policy = self.groups_config.get(event.chat_id)
        if policy is None:
            return GroupEventRoute(False, reason="group_not_configured")
        if policy.mode == "disabled":
            return GroupEventRoute(False, reason="group_disabled")
        if event.sender_role == "staff":
            if (
                not self.groups_config.default_reply_to_staff_when_mentioned
                or not policy.reply_to_staff_when_mentioned
            ):
                return GroupEventRoute(False, reason="staff_reply_disabled")
            if not event.mentioned_bot:
                return GroupEventRoute(False, reason="staff_not_mentioned")
        elif policy.require_customer_mention and not event.mentioned_bot:
            return GroupEventRoute(False, reason="customer_not_mentioned")
        return GroupEventRoute(True, shadow=policy.mode == "shadow", reason="group_allowed")

    def split_customer_demands(self, text: str) -> list[str]:
        """Use the whole message to decide task boundaries, preserving one on failure."""
        whole_message = text.strip()
        splitter = getattr(self.model_client, "split_demands", None)
        if not callable(splitter):
            return [whole_message]
        try:
            demands = validate_model_demands(whole_message, splitter(whole_message))
        except Exception as exc:
            logger.warning("Demand segmentation unavailable: %s", exc)
            demands = None
        if demands is None:
            logger.warning("Demand segmentation unverified; keeping the whole message")
            return [whole_message]
        logger.info("Semantic demand segmentation produced %d task(s)", len(demands))
        return demands

    def record_conversation_event(
        self, event: InboundEvent, *, demands: list[str] | None = None
    ) -> EventReceipt:
        """Persist event metadata for quiet-window and human-takeover decisions."""
        self.record_group_context_event(event)
        text = event.text or f"[message-type:{event.msg_type}]"
        received_at = event_occurred_at(event)
        if event.sender_role == "staff":
            receipt = self.conversation_state.record_staff_message(
                chat_id=event.chat_id,
                staff_id=event.sender_id,
                external_event_id=event.message_id,
                received_at=received_at,
                text=text,
                mentioned_bot=event.mentioned_bot,
                quoted_msg_id=event.quoted_msg_id,
                precise_issue_target=bool(
                    self.groups_config.get(event.chat_id)
                    and self.groups_config.get(event.chat_id).associate_customer_demands
                ),
            )
            logger.info(
                "Recorded staff event chat=%s mentioned_bot=%s target_tasks=%s",
                event.chat_id,
                event.mentioned_bot,
                receipt.task_ids or "ambiguous_or_none",
            )
            return receipt
        validated_demands = validate_model_demands(text, demands) if demands is not None else None
        demand_count = len(validated_demands) if validated_demands is not None else 1
        receipt = self.conversation_state.record_customer_message(
            chat_id=event.chat_id,
            customer_id=event.sender_id,
            external_event_id=event.message_id,
            received_at=received_at,
            text=text,
            demand_count=demand_count,
        )
        # Media is received in separate callbacks before the describing text.
        # Bind that batch now, while the event still has a deterministic task
        # identity; never let later tasks read the whole group media window.
        context_sender = event.chat_id if event.is_group else event.sender_id
        if hasattr(self, "context_buffer"):
            self.context_buffer.bind_pending_media(
                chat_id=event.chat_id,
                sender_id=context_sender,
                task_ids=receipt.task_ids or ((receipt.task_id,) if receipt.task_id else ()),
            )
        logger.info(
            "Created demand task(s) chat=%s event=%s task_count=%d",
            event.chat_id,
            event.message_id,
            len(receipt.task_ids) or int(receipt.task_id is not None),
        )
        return receipt

    def persist_pending_group_event(
        self,
        event: InboundEvent,
        *,
        event_id: str | None = None,
        demands: list[str] | None = None,
    ) -> bool:
        """Encrypt the delayed event payload; return false when fail-closed."""
        if self.payload_vault is None:
            return False
        payload = json.dumps(
            {
                "message_id": event.message_id,
                "sender_id": event.sender_id,
                "text": event.text,
                "demands": validate_model_demands(event.text, demands) or [event.text],
                "quoted_text": event.quoted_text,
                "quoted_type": event.quoted_type,
                "quoted_media_xml": event.quoted_media_xml,
                "quoted_msg_id": event.quoted_msg_id,
                "msg_type": event.msg_type,
                "reply_source": self.reply_source_for_event(event).as_dict(),
            },
            ensure_ascii=False,
        )
        try:
            self.payload_vault.store_text(event_id or event.message_id, payload)
        except Exception:
            logger.exception("Unable to persist delayed group event metadata")
            return False
        return True

    def _association_item(self, task: ConversationTask) -> dict:
        refs = self.conversation_state.list_customer_event_refs(task)
        texts = []
        payload = {}
        for event_id, index in refs:
            raw = self.payload_vault.retrieve_text(event_id)
            if not raw:
                continue
            payload = json.loads(raw)
            parts = validate_model_demands(payload.get("text", ""), payload.get("demands"))
            if "demands" not in payload:
                parts = split_message_demands(payload.get("text", ""))
            if parts and index < len(parts):
                texts.append(association_text(parts[index]))
        return {
            "task_id": task.task_id,
            "event_id": refs[-1][0] if refs else "",
            "text": "\n".join(texts),
            "message_id": payload.get("message_id", ""),
            "quoted_msg_id": payload.get("quoted_msg_id", ""),
            "quoted_text": association_text(payload.get("quoted_text", "")),
            "received_at": task.latest_customer_at,
        }

    def reconcile_customer_issues(self, *, now=None) -> list[str]:
        """Associate collected demands before polling; model failure keeps them independent."""
        if self.payload_vault is None:
            return []
        chats = [
            key
            for key, policy in self.groups_config.groups.items()
            if policy.associate_customer_demands and policy.mode != "disabled"
        ]
        window = min(
            ASSOCIATION_WINDOW_SECONDS,
            getattr(self.settings, "conversation_quiet_window_seconds", ASSOCIATION_WINDOW_SECONDS),
        )
        cutoff = iso_timestamp(
            datetime.fromisoformat(iso_timestamp(now)) - timedelta(seconds=window)
        )
        issues = self.conversation_state.issues
        tasks = issues.unassigned(chats, cutoff)
        batches = {}
        for row in tasks:
            batches.setdefault((row["chat_id"], row["customer_id"]), []).append(row)
        for (chat_id, customer_id), rows in batches.items():
            items = []
            for row in rows:
                task = self.conversation_state.get_task(row["task_id"])
                try:
                    item = self._association_item(task)
                except (PayloadVaultCorruption, ValueError, TypeError, KeyError):
                    item = {
                        "task_id": task.task_id,
                        "text": "",
                        "event_id": "",
                        "quoted_msg_id": "",
                        "received_at": task.latest_customer_at,
                    }
                items.append(item)
            candidates = []
            for issue in issues.candidates(chat_id, customer_id):
                context = []
                event_ids = set()
                for member in issues.members(issue["issue_id"])[-4:]:
                    try:
                        item = self._association_item(self.conversation_state.get_task(member))
                        context.append(item["text"])
                        event_ids.add(item["event_id"])
                    except (PayloadVaultCorruption, ValueError, TypeError, KeyError):
                        continue
                candidates.append(
                    {**issue, "text": "\n".join(context)[-6000:], "event_ids": list(event_ids)}
                )
            links = {}
            associator = getattr(getattr(self, "model_client", None), "associate_demands", None)
            if callable(associator) and (len(items) > 1 or candidates):
                try:
                    links = validated_links(
                        associator(items, candidates), {i["task_id"] for i in items}
                    )
                except Exception as exc:
                    logger.warning(
                        "Demand association unavailable; keeping independent tasks: %s", exc
                    )
            known = {candidate["issue_id"]: candidate for candidate in candidates}
            for item in items:
                proposal = links.get(item["task_id"], {})
                relation = proposal.get("relation", "new")
                target = known.get(proposal.get("target_id", "")) if relation != "new" else None
                if target and relation != "new":
                    explicit = bool(
                        issues.referenced_tasks(chat_id, item.get("quoted_msg_id", ""))
                        & set(issues.members(target["issue_id"]))
                    )
                    left, right = order_ids(item["text"]), order_ids(target["text"])
                    conflict = bool(left and right and not left & right)
                    same_event = item["event_id"] in target.get("event_ids", [])
                    anchored = (
                        explicit
                        or bool(left & right)
                        or issues.within_window(target, item["received_at"])
                    )
                    if conflict or same_event or not anchored:
                        target = None
                assigned = issues.assign(
                    item["task_id"],
                    target_id=target["issue_id"] if target else "",
                    relation=relation if target else "new",
                )
                if assigned:
                    existing = known.get(assigned["issue_id"], {})
                    descriptor = {
                        **assigned,
                        "text": "\n".join(filter(None, [existing.get("text", ""), item["text"]])),
                        "event_ids": list(set(existing.get("event_ids", [])) | {item["event_id"]}),
                    }
                    known[item["task_id"]] = descriptor
                    known[assigned["issue_id"]] = descriptor
                    for alias, previous in list(known.items()):
                        if previous["issue_id"] == assigned["issue_id"]:
                            known[alias] = descriptor
        return chats

    def process_conversation_task(self, task: ConversationTask) -> None:
        """Restore one quiet task, query it, and dispatch according to group mode."""
        if self.payload_vault is None:
            self.conversation_state.set_state(task.task_id, "blocked")
            return

        texts: list[str] = []
        latest_message_id = ""
        latest_message_type = 1
        latest_reply_source = None
        latest_quoted_text = ""
        latest_quoted_type = ""
        latest_quoted_media_xml = ""
        latest_quoted_msg_id = ""
        issue = self.conversation_state.issues.get(task.task_id)
        member_ids = (
            self.conversation_state.issues.members(issue["issue_id"], pending_only=True)
            if issue
            else [task.task_id]
        )
        reply_sources = []
        try:
            event_refs = [
                ref
                for member_id in member_ids
                for ref in self.conversation_state.list_customer_event_refs(
                    self.conversation_state.get_task(member_id)
                )
            ]
            logger.info(
                "Restoring conversation task task_id=%s event_slots=%d",
                task.task_id,
                len(event_refs),
            )
            for event_id, demand_index in event_refs:
                raw_payload = self.payload_vault.retrieve_text(event_id)
                if not raw_payload:
                    raise PayloadVaultCorruption(f"Missing delayed payload {event_id}")
                payload = json.loads(raw_payload)
                original_message_id = payload.get("message_id")
                latest_message_id = (
                    original_message_id if isinstance(original_message_id, str) else ""
                )
                original_message_type = payload.get("msg_type")
                latest_message_type = (
                    original_message_type if isinstance(original_message_type, int) else 1
                )
                latest_reply_source = TextQuote.from_mapping(payload.get("reply_source"))
                if latest_reply_source and (
                    latest_reply_source.message_id != latest_message_id
                    or latest_reply_source.chat_id != task.chat_id
                    or latest_reply_source.sender_id != task.customer_id
                ):
                    latest_reply_source = None
                if latest_reply_source:
                    reply_sources.append(latest_reply_source)
                if issue and latest_message_id:
                    self._restore_issue_media(task, latest_message_id)
                quoted_text = payload.get("quoted_text")
                quoted_type = payload.get("quoted_type")
                quoted_media_xml = payload.get("quoted_media_xml")
                quoted_msg_id = payload.get("quoted_msg_id")
                latest_quoted_text = quoted_text if isinstance(quoted_text, str) else ""
                latest_quoted_type = quoted_type if isinstance(quoted_type, str) else ""
                latest_quoted_media_xml = (
                    quoted_media_xml if isinstance(quoted_media_xml, str) else ""
                )
                latest_quoted_msg_id = quoted_msg_id if isinstance(quoted_msg_id, str) else ""
                text = payload.get("text")
                if isinstance(text, str) and text.strip():
                    if "demands" in payload:
                        demands = validate_model_demands(text, payload["demands"])
                        if demands is None:
                            raise PayloadVaultCorruption(
                                f"Invalid persisted demand segmentation for event {event_id}"
                            )
                    else:
                        # Payloads queued before the semantic splitter was added.
                        demands = split_message_demands(text.strip())
                    if demand_index >= len(demands):
                        raise PayloadVaultCorruption(
                            f"Demand slot {demand_index} missing for event {event_id}"
                        )
                    texts.append(
                        association_text(demands[demand_index], limit=None)
                        if issue
                        else demands[demand_index]
                    )
        except (PayloadVaultCorruption, json.JSONDecodeError, TypeError, ValueError):
            logger.exception("Unable to restore delayed conversation task=%s", task.task_id)
            self.conversation_state.set_state(task.task_id, "blocked")
            if issue:
                self.conversation_state.issues.finish(issue["issue_id"], state="blocked")
            return

        if not texts:
            self.conversation_state.set_state(task.task_id, "closed")
            if issue:
                self.conversation_state.issues.finish(issue["issue_id"], state="closed")
            return

        if issue and reply_sources:
            latest_reply_source = next(
                (source for source in reply_sources if source.native_ready), reply_sources[0]
            )
            latest_message_id = latest_reply_source.message_id
            latest_message_type = latest_reply_source.msg_type

        event = InboundEvent(
            message_id=latest_message_id,
            from_user=task.chat_id,
            to_user=self.settings.gewe_app_id,
            chat_id=task.chat_id,
            user_id=task.customer_id,
            msg_type=latest_message_type,
            text="\n".join(texts),
            sender_id=task.customer_id,
            sender_role="customer",
            is_group=task.chat_id.endswith("@chatroom"),
            conversation_task_id=task.task_id,
            quoted_text=latest_quoted_text,
            quoted_type=latest_quoted_type,
            quoted_media_xml=latest_quoted_media_xml,
            quoted_msg_id=latest_quoted_msg_id,
            reply_source=latest_reply_source,
            related_task_ids=tuple(member_ids) if issue else (),
        )
        soothe_claimed = False
        try:
            reply = (
                self.process_text_query(event)
                if len(texts) == 1 or issue
                else self.process_demand_bundle(event, texts)
            )
            current_task = self.conversation_state.get_task(task.task_id)
            if current_task is None or current_task.state != "processing":
                logger.info(
                    "Suppressing stale delayed reply task_id=%s state=%s",
                    task.task_id,
                    current_task.state if current_task is not None else "missing",
                )
                if issue:
                    self.conversation_state.issues.finish(issue["issue_id"], state="waiting_quiet")
                return
            policy = self.groups_config.get(task.chat_id)
            soothe_claimed = bool(
                issue
                and reply.channel_used == "soothe"
                and not reply.suppress_outbound
                and policy
                and policy.mode not in {"shadow", "disabled"}
            )
            if issue and not self.conversation_state.issues.can_send(
                issue["issue_id"],
                issue["version"],
                soothe=soothe_claimed,
            ):
                soothe_claimed = False
                self.conversation_state.issues.finish(issue["issue_id"], state="waiting_quiet")
                return
            outcome = None
            if policy is not None and policy.mode not in {"shadow", "disabled"}:
                outcome = self.dispatch_outbound(to_wxid=task.chat_id, reply=reply)
                self.log_group_outbound(
                    to_wxid=task.chat_id, reply=reply, task_id=task.task_id, outcome=outcome
                )
            if issue:
                status = None
                if soothe_claimed:
                    status = outcome.text_status if outcome else "unknown"
                self.conversation_state.issues.finish(issue["issue_id"], soothe_status=status)
            self.conversation_state.set_state(task.task_id, "responded")
            if hasattr(self, "context_buffer"):
                for member_id in member_ids:
                    self.context_buffer.clear_task_media(member_id)
        except Exception:
            logger.exception("Delayed conversation task failed: task_id=%s", task.task_id)
            next_state = "responded" if soothe_claimed else "waiting_quiet"
            self.conversation_state.set_state(task.task_id, next_state)
            if issue:
                self.conversation_state.issues.finish(
                    issue["issue_id"],
                    state=next_state,
                    soothe_status="unknown" if soothe_claimed else None,
                )

    def _restore_issue_media(self, task: ConversationTask, message_id: str) -> None:
        """Restore only media owned by the issue's customer and original event."""
        media_store = getattr(self, "inbound_media_store", None)
        context = getattr(self, "context_buffer", None)
        if media_store is None or context is None:
            return
        if context.get_media_by_message_id(
            chat_id=task.chat_id, sender_id=task.customer_id, message_id=message_id
        ):
            context.bind_media_by_message_id(
                chat_id=task.chat_id,
                sender_id=task.customer_id,
                message_id=message_id,
                task_id=task.task_id,
            )
            return
        try:
            stored = media_store.find_by_message(
                chat_id=task.chat_id, sender_id=task.customer_id, message_id=message_id
            )
            if stored:
                context.push_media(
                    msg_id=message_id,
                    chat_id=task.chat_id,
                    sender_id=task.customer_id,
                    kind=stored.kind,
                    local_path="",
                    media_id=stored.media_id,
                    image_data=media_store.read(stored),
                    mime_type=stored.mime_type,
                    bound_task_ids=(task.task_id,),
                )
        except Exception:
            logger.exception("Unable to restore issue media message_id=%s", message_id)

    def process_demand_bundle(
        self,
        event: InboundEvent,
        messages: list[str],
    ) -> ReplyDeliverable:
        """Process split demands separately and merge outbound text/files once."""
        demands = split_and_merge_demands(messages)
        replies = [self.process_text_query(replace(event, text=demand)) for demand in demands]
        if not replies:
            return ReplyDeliverable(text="", channel_used="silent", suppress_outbound=True)

        texts = [reply.text.strip() for reply in replies if reply.text.strip()]
        deliverables = [item for reply in replies for item in reply.deliverables]
        owner_notices = [reply.owner_notify_text for reply in replies if reply.owner_notify_text]
        return ReplyDeliverable(
            text="\n\n".join(texts),
            deliverables=deliverables,
            channel_used=replies[0].channel_used if len(replies) == 1 else "multi",
            plan=replies[-1].plan,
            suppress_outbound=all(reply.suppress_outbound for reply in replies),
            owner_notify_text="\n\n".join(owner_notices),
        )

    def process_visual_query_bundle(
        self,
        event: InboundEvent,
        sku_codes: tuple[str, ...],
    ) -> ReplyDeliverable:
        """Fan out an explicit multi-product image query and merge it once."""
        replies = [
            self.process_text_query(
                replace(event, visual_sku_code=sku_code),
                record_history=False,
            )
            for sku_code in sku_codes
        ]
        sections = []
        for sku_code, reply in zip(sku_codes, replies, strict=False):
            if reply.text.strip():
                sections.append(f"【SKU编码：{sku_code}】\n{reply.text.strip()}")
        merged_text = "\n\n".join(sections)
        history_sender = event.chat_id if event.is_group else event.sender_id
        self.context_buffer.push_message(
            event.chat_id,
            history_sender,
            "user",
            event.text,
            event.conversation_task_id or None,
        )
        if merged_text:
            self.context_buffer.push_message(
                event.chat_id,
                history_sender,
                "assistant",
                merged_text,
                event.conversation_task_id or None,
            )
        return ReplyDeliverable(
            text=merged_text,
            deliverables=[item for reply in replies for item in reply.deliverables],
            channel_used="multi_visual",
            plan=replies[-1].plan if replies else None,
            suppress_outbound=all(reply.suppress_outbound for reply in replies),
            owner_notify_text="\n\n".join(
                reply.owner_notify_text for reply in replies if reply.owner_notify_text
            ),
        )

    def process_image(self, event: InboundEvent) -> bool:
        """Download and buffer one image for deferred model understanding."""
        image_data = event.image_data
        if image_data is None:
            msg_data = event.raw_payload.get("Data", {})
            if isinstance(msg_data, dict):
                image_xml = normalize_media_xml(
                    msg_data.get("Content")
                    or msg_data.get("content")
                    or msg_data.get("Xml")
                    or msg_data.get("xml"),
                    is_group=event.is_group,
                )
            else:
                image_xml = ""
            if image_xml:
                try:
                    image_data = self.wechat.download_inbound_media(
                        xml=image_xml,
                        message_id=event.message_id,
                        kind="image",
                    )
                except Exception:
                    logger.exception(
                        "Unable to download inbound image message=%s xml_len=%d",
                        event.message_id,
                        len(image_xml),
                    )

        mime_type = _detect_image_mime_type(image_data or b"")
        if image_data and len(image_data) > _MAX_BUFFERED_IMAGE_BYTES:
            logger.warning("Inbound image exceeds local size limit message=%s", event.message_id)
            image_data = None
            mime_type = ""
        if image_data and not mime_type:
            logger.warning("Inbound image type is unsupported message=%s", event.message_id)
            image_data = None

        media_id = f"inm_{event.message_id}"
        context_sender = event.chat_id if event.is_group else event.sender_id
        self.context_buffer.push_media(
            msg_id=event.message_id,
            chat_id=event.chat_id,
            sender_id=context_sender,
            kind="image",
            local_path=event.image_url or "",
            media_id=media_id,
            image_data=image_data,
            mime_type=mime_type,
        )
        if image_data and mime_type:
            try:
                self.inbound_media_store.stage(
                    media_id=media_id,
                    message_id=event.message_id,
                    chat_id=event.chat_id,
                    sender_id=context_sender,
                    kind="image",
                    mime_type=mime_type,
                    content=image_data,
                )
            except Exception:
                logger.exception(
                    "Unable to persist inbound image handle message=%s", event.message_id
                )
        logger.info(
            "Buffered image %s from user %s into sliding window (downloaded=%s)",
            media_id,
            event.from_user,
            bool(image_data),
        )
        return bool(image_data and mime_type)

    def compose_soothe_text(
        self,
        *,
        user_query: str,
        history: list[dict[str, str]] | None = None,
        task_input: MultimodalTaskInput | None = None,
    ) -> str:
        """Compose the acknowledgement for a non-standard demand.

        Two modes, selected by ``soothe_mode`` in ``config/wechat_groups.toml``
        and hot-reloaded with the rest of that file:

        * ``model`` (default) — the wording must vary per message and read like a
          person, so it is generated. ``NON_STANDARD_SOOTHE_REPLY`` is only the
          backstop for when the model is unreachable: a non-standard demand must
          still get a human reply instead of silence.
        * ``fixed`` — one operator-authored sentence for every such demand. The
          owner asked for this option so the wording is predictable and reviewable
          ("收到，我们加急处理"), and so the path costs no model call.
        """
        config = self.groups_config
        fixed_reply = clean_wechat_text(config.soothe_fixed_reply).strip()
        if config.soothe_mode == "fixed" and fixed_reply:
            logger.info("Using the fixed soothing wording (soothe_mode=fixed)")
            return fixed_reply
        try:
            try:
                composed = self.model_client.compose_soothe_reply(
                    user_query=user_query,
                    history=history,
                    task_input=task_input,
                )
            except TypeError as exc:
                if "task_input" not in str(exc):
                    raise
                composed = self.model_client.compose_soothe_reply(
                    user_query=user_query,
                    history=history,
                )
        except Exception:
            logger.exception("Soothe reply composition failed; using fallback wording")
            return NON_STANDARD_SOOTHE_REPLY
        cleaned = clean_wechat_text(composed or "").strip()
        return cleaned or NON_STANDARD_SOOTHE_REPLY

    def compose_insufficient_data_text(
        self,
        *,
        user_query: str,
        history: list[dict[str, str]] | None = None,
        task_input: MultimodalTaskInput | None = None,
    ) -> str:
        """Compose the "not found in our records, handing over" reply.

        负责人决策（2026-09-18）：标准需求但档案里查不到 → 答"资料不足"并转人工，
        不要静默。Wording is model-generated so it reads like a person; the
        constant is only the fallback when the model is unreachable.
        """
        try:
            try:
                composed = self.model_client.compose_insufficient_data_reply(
                    user_query=user_query,
                    history=history,
                    task_input=task_input,
                )
            except TypeError as exc:
                if "task_input" not in str(exc):
                    raise
                composed = self.model_client.compose_insufficient_data_reply(
                    user_query=user_query,
                    history=history,
                )
        except Exception:
            logger.exception(
                "Insufficient-data reply composition failed; using fallback wording"
            )
            return INSUFFICIENT_DATA_FALLBACK_REPLY
        cleaned = clean_wechat_text(composed or "").strip()
        return cleaned or INSUFFICIENT_DATA_FALLBACK_REPLY

    def compose_reply_text(
        self,
        *,
        user_query: str,
        context: str,
        history: list[dict[str, str]] | None,
        task_input: MultimodalTaskInput | None = None,
    ) -> str:
        """Compose a reply with the task protocol and legacy-double fallback."""
        try:
            return self.model_client.compose_reply(
                user_query=user_query,
                context=context,
                history=history,
                task_input=task_input,
            )
        except TypeError as exc:
            if "task_input" not in str(exc):
                raise
            return self.model_client.compose_reply(
                user_query=user_query,
                context=context,
                history=history,
            )

    def insufficient_data_reply(
        self,
        *,
        plan: QueryPlan | None,
        chat_id: str,
        sender_id: str,
        user_query: str,
        history: list[dict[str, str]] | None,
        reason: str,
        task_input: MultimodalTaskInput | None = None,
    ) -> ReplyDeliverable:
        """Build the "资料不足 + 私聊转人工" reply.

        负责人决策（2026-09-18）：可以答资料不足，然后转人工。Used whenever a
        standard demand cannot be answered from the archive — never silence, and
        never a fabricated "查询成功" context for the model to pad out.
        """
        text = self.compose_insufficient_data_text(
            user_query=user_query,
            history=history,
            task_input=task_input,
        )
        notice = build_owner_notice(
            self.groups_config,
            chat_id=chat_id,
            sender_id=sender_id,
            user_query=user_query,
            reason=reason,
        )
        logger.info(
            "Unanswerable structured lookup: chat=%s reason=%s owner_reminder=%s text=%s",
            chat_id,
            reason,
            bool(notice),
            text,
        )
        return ReplyDeliverable(
            text=text,
            channel_used="structured_no_evidence",
            plan=plan,
            owner_notify_text=notice,
        )

    def process_text_query(
        self,
        event: InboundEvent,
        *,
        record_history: bool = True,
    ) -> ReplyDeliverable:
        reply = self._process_text_query(event, record_history=record_history)
        if event.is_group and reply.channel_used in {"soothe", "clarify"}:
            reply.reply_source = self.reply_source_for_event(event)
        return reply

    @staticmethod
    def reply_source_for_event(event: InboundEvent) -> TextQuote:
        if event.reply_source is not None:
            return event.reply_source
        data = event.raw_payload.get("Data", {})
        return TextQuote.from_callback(
            data=data if isinstance(data, dict) else {}, message_id=event.message_id,
            chat_id=event.chat_id, sender_id=event.sender_id, content=event.text,
            msg_type=event.msg_type, sender_name=event.sender_name,
        )

    def _process_text_query(
        self,
        event: InboundEvent,
        *,
        record_history: bool = True,
    ) -> ReplyDeliverable:
        """Process one text query through the configured model and read model."""
        user_query = event.text.strip()
        sender_id = event.chat_id if event.is_group else event.sender_id
        chat_id = event.chat_id

        def remember(role: str, content: str) -> None:
            if record_history:
                self.context_buffer.push_message(
                    chat_id,
                    sender_id,
                    role,
                    content,
                    event.conversation_task_id or None,
                )

        # 1. Retrieve media and chat history from count-bounded windows.
        staff_direct_question = (
            event.is_group
            and event.sender_role == "staff"
            and event.mentioned_bot
            and not event.conversation_task_id
        )
        if event.conversation_task_id:
            active_media = self.context_buffer.get_task_media(
                chat_id=chat_id,
                sender_id=sender_id,
                task_id=event.conversation_task_id,
            )
            for related_id in event.related_task_ids:
                for media in self.context_buffer.get_task_media(
                    chat_id=chat_id, sender_id=sender_id, task_id=related_id
                ):
                    if all(item.media_id != media.media_id for item in active_media):
                        active_media.append(media)
        elif staff_direct_question:
            # A staff question without a linked task must not silently inherit
            # a customer's recent unquoted image from the shared group window.
            active_media = []
        else:
            active_media = self.context_buffer.get_recent_media(
                chat_id=chat_id,
                sender_id=sender_id,
            )
        if not active_media and event.quoted_msg_id:
            referenced_media = self.context_buffer.get_media_by_message_id(
                chat_id=chat_id,
                sender_id=sender_id,
                message_id=event.quoted_msg_id,
            )
            if referenced_media and event.conversation_task_id:
                self.context_buffer.bind_media_by_message_id(
                    chat_id=chat_id,
                    sender_id=sender_id,
                    message_id=event.quoted_msg_id,
                    task_id=event.conversation_task_id,
                )
                active_media = self.context_buffer.get_task_media(
                    chat_id=chat_id,
                    sender_id=sender_id,
                    task_id=event.conversation_task_id,
                )
            elif referenced_media:
                active_media = referenced_media

        inbound_media_store = getattr(self, "inbound_media_store", None)
        if not active_media and event.quoted_msg_id and inbound_media_store is not None:
            try:
                stored_media = inbound_media_store.find_by_message(
                    chat_id=chat_id,
                    sender_id=sender_id,
                    message_id=event.quoted_msg_id,
                )
                if stored_media is not None:
                    quoted_bytes = inbound_media_store.read(stored_media)
                    self.context_buffer.push_media(
                        msg_id=stored_media.message_id,
                        chat_id=chat_id,
                        sender_id=sender_id,
                        kind=stored_media.kind,
                        local_path="",
                        media_id=stored_media.media_id,
                        image_data=quoted_bytes,
                        mime_type=stored_media.mime_type,
                        bound_task_ids=(event.conversation_task_id,)
                        if event.conversation_task_id
                        else (),
                    )
                    active_media = (
                        self.context_buffer.get_task_media(
                            chat_id=chat_id,
                            sender_id=sender_id,
                            task_id=event.conversation_task_id,
                        )
                        if event.conversation_task_id
                        else self.context_buffer.get_recent_media(
                            chat_id=chat_id,
                            sender_id=sender_id,
                        )
                    )
                    logger.info(
                        "Rehydrated quoted media message=%s media_id=%s",
                        event.quoted_msg_id,
                        stored_media.media_id,
                    )
            except Exception:
                logger.exception(
                    "Failed to rehydrate quoted media message=%s", event.quoted_msg_id
                )

        if not active_media and event.quoted_media_xml:
            try:
                quoted_bytes = self.wechat.download_inbound_media(
                    xml=event.quoted_media_xml,
                    message_id=event.quoted_msg_id or event.message_id,
                    kind="image",
                )
                mime = _detect_image_mime_type(quoted_bytes or b"")
                if quoted_bytes and mime:
                    media_id = f"inm_quoted_{event.message_id}"
                    self.context_buffer.push_media(
                        msg_id=f"quoted_{event.message_id}",
                        chat_id=chat_id,
                        sender_id=sender_id,
                        kind="image",
                        local_path="",
                        media_id=media_id,
                        image_data=quoted_bytes,
                        mime_type=mime,
                        bound_task_ids=(event.conversation_task_id,)
                        if event.conversation_task_id
                        else (),
                    )
                    if event.conversation_task_id:
                        active_media = self.context_buffer.get_task_media(
                            chat_id=chat_id,
                            sender_id=sender_id,
                            task_id=event.conversation_task_id,
                        )
                    else:
                        active_media = self.context_buffer.get_recent_media(
                            chat_id=chat_id,
                            sender_id=sender_id,
                        )
            except Exception:
                logger.exception("Failed to download quoted media message=%s", event.message_id)

        if not active_media and event.quoted_msg_id and not event.quoted_media_xml:
            logger.warning(
                "Quoted media handle unavailable message=%s; no downloadable XML fallback",
                event.quoted_msg_id,
            )

        logger.info(
            "Media window chat=%s sender=%s retention=message_window count=%d [%s]",
            chat_id,
            sender_id,
            len(active_media),
            ", ".join(
                f"{media.media_id}:{media.kind}:{'bytes' if media.image_data else 'no-bytes'}"
                f":{'analyzed' if media.visual_analysis else 'raw'}"
                for media in active_media
            )
            or "-",
        )

        history = self.context_buffer.get_history(
            chat_id=chat_id,
            sender_id=sender_id,
            limit=4,
            task_id=event.conversation_task_id or None,
        )

        # Select the persisted 60-message window (plus quote ±5 neighborhood)
        # before any LLM node runs.  The same snapshot is passed to visual and
        # planning nodes so neither node has to infer task scope from a session.
        group_context = ""
        context_window = self._group_context_for(event.chat_id)
        if context_window is not None:
            group_context = context_window.format_for_message(
                message_id=event.message_id,
                current_text=user_query,
            )
        task_input = MultimodalTaskInput(
            task_id=event.conversation_task_id,
            user_query=user_query,
            quoted_text=event.quoted_text,
            quoted_message_id=event.quoted_msg_id,
            quoted_type=event.quoted_type,
            quoted_context=group_context if event.quoted_msg_id else "",
            group_context=group_context,
            history=tuple(dict(item) for item in history),
            images=tuple(
                MultimodalImage(
                    media_id=media.media_id,
                    message_id=media.msg_id,
                    mime_type=media.mime_type,
                    image_data=media.image_data,
                )
                for media in active_media
                if media.kind == "image" and media.image_data and media.mime_type
            ),
        )

        image_context_parts: list[str] = []
        image_analyses: list[dict[str, object]] = []
        if active_media:
            image_context_parts.append(
                f"当前需求关联了 {len(active_media)} 张图片/条码。"
            )
            for media in active_media:
                if media.kind != "image":
                    continue
                analysis = media.visual_analysis
                if analysis is None and media.image_data and media.mime_type:
                    try:
                        try:
                            analysis = self.model_client.analyze_image(
                                media.image_data,
                                mime_type=media.mime_type,
                                task_input=task_input,
                                media_id=media.media_id,
                            )
                        except TypeError as exc:
                            # Compatibility for legacy test doubles/plugins. The
                            # production ChatModelClient always receives the full
                            # task snapshot above.
                            if "task_input" not in str(exc) and "media_id" not in str(exc):
                                raise
                            analysis = self.model_client.analyze_image(
                                media.image_data,
                                mime_type=media.mime_type,
                            )
                    except Exception:
                        logger.exception("Image understanding failed media_id=%s", media.media_id)
                        analysis = {
                            "intent": "unknown",
                            "visible_text": "",
                            "candidates": [],
                            "target_count": {"min": 0, "max": 0, "exact": False},
                            "missing_information": ["图片视觉结构化输出不可用"],
                            "image_confidence": 0.0,
                        }
                    self.context_buffer.set_visual_analysis(
                        chat_id=chat_id,
                        sender_id=sender_id,
                        msg_id=media.msg_id,
                        analysis=analysis,
                    )
                if analysis:
                    # The resolver keeps the raw analysis and its OCR confidence;
                    # the planner only sees what was recognised and what is missing.
                    image_analyses.append(analysis)
                    logger.info(
                        "Image analysis media_id=%s bytes=%d mime=%s intent=%s "
                        "candidates=%d image_confidence=%s visible_text_chars=%d",
                        media.media_id,
                        len(media.image_data or b""),
                        media.mime_type,
                        analysis.get("intent"),
                        len(analysis.get("candidates") or []),
                        analysis.get("image_confidence"),
                        len(str(analysis.get("visible_text") or "")),
                    )
                    # Full OCR payload only at DEBUG: 默认不把图片识别内容写进日志。
                    logger.debug(
                        "Image analysis detail media_id=%s payload=%s",
                        media.media_id,
                        json.dumps(analysis, ensure_ascii=False),
                    )
                    image_context_parts.append(
                        "图片视觉候选（仅供本地查询核验，不是已确认事实；"
                        "此处不含识别把握，不得据此压低 intent_confidence）："
                        + json.dumps(
                            planner_safe_analysis(analysis),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                else:
                    image_context_parts.append("图片视觉理解不可用，请不要依据图片猜测编码或商品事实。")

        image_ctx = "\n".join(image_context_parts)
        visual_resolution = resolve_visual_skus(
            self.read_model,
            image_analyses,
            minimum_confidence=(
                0.50
                if active_media and is_image_dependent_query(user_query)
                else 0.80
            ),
        )
        if event.visual_sku_code:
            visual_resolution = VisualSkuResolution(
                "unique",
                (event.visual_sku_code,),
                "explicit_bundle_candidate",
            )
            image_context_parts.append(
                "本次只处理已核验的图片 SKU：" + event.visual_sku_code
            )
        if visual_resolution.status == "unique":
            image_context_parts.append(
                "本地读模型已唯一核验图片候选 SKU："
                + ",".join(visual_resolution.sku_codes)
            )
        elif visual_resolution.status == "ambiguous":
            image_context_parts.append(
                "本地读模型核验到多个图片候选 SKU，禁止猜测，必须请求人工客服确认。"
            )
        elif active_media:
            image_context_parts.append(
                "图片候选尚未被本地读模型唯一核验，禁止依据图片猜测商品事实。"
            )
        image_ctx = "\n".join(image_context_parts)

        # A single locally verified visual candidate is an authoritative query
        # entity even when the inbound event was not pre-labelled by the bundle
        # path. This prevents the planner from replacing a unique SKU with a
        # textual guess after the media batch has been isolated.
        verified_visual_sku = (
            visual_resolution.sku_codes[0]
            if visual_resolution.status == "unique"
            else ""
        )

        logger.info(
            "Visual resolution status=%s reason=%s skus=%s image_dependent=%s multi_query=%s",
            visual_resolution.status,
            visual_resolution.reason,
            ",".join(visual_resolution.sku_codes) or "-",
            is_image_dependent_query(user_query),
            is_multi_visual_query(user_query),
        )

        if (
            not event.visual_sku_code
            and visual_resolution.status == "ambiguous"
            and is_multi_visual_query(user_query)
        ):
            return self.process_visual_query_bundle(event, visual_resolution.sku_codes)

        # 2. Plan query using the configured chat model
        planning_query = user_query
        if verified_visual_sku:
            planning_query = (
                f"本次只处理已由本地读模型核验的 SKU {verified_visual_sku}。"
                f"原始用户请求：{user_query}"
            )
        logger.debug("Planner image context: %s", image_ctx)
        planner_kwargs: dict[str, object] = {
            "user_query": planning_query,
            "history": history,
            "image_context": image_ctx,
            "task_input": task_input,
        }
        if group_context:
            planner_kwargs["group_context"] = group_context
        try:
            try:
                plan = self.model_client.plan_query(**planner_kwargs)
            except TypeError as exc:
                if "task_input" not in str(exc):
                    raise
                # Legacy test doubles/plugins may not expose the new keyword;
                # the real client path above is the required production contract.
                planner_kwargs.pop("task_input", None)
                plan = self.model_client.plan_query(**planner_kwargs)
        except QueryPlanUnavailable as exc:
            # 规划器给不出可用计划（模型返回非 JSON / 校验失败 / 接口异常）。
            # 以前这个异常会一路冒到 ASGI，把微信回调打成 500 —— 客户一条回复都
            # 收不到。现在降级为"资料不足 + 私聊转人工"：不说假话，也不静默。
            logger.error("Planner unavailable for chat=%s: %s", chat_id, exc)
            remember("user", user_query)
            reply = self.insufficient_data_reply(
                plan=None,
                chat_id=chat_id,
                sender_id=event.sender_id,
                user_query=user_query,
                history=history,
                reason="unanswerable",
                task_input=task_input,
            )
            remember("assistant", reply.text)
            return reply
        if verified_visual_sku and visual_resolution.status == "unique":
            verified_entity = QueryEntity(type="sku", id=verified_visual_sku)
            if is_report_query(user_query):
                # A verified image SKU is authoritative.  For report requests,
                # the report files live on the SKU's category, so pin the
                # cross-scope attachment query instead of letting the planner
                # drift to product photos or omit the entity on one fan-out.
                plan = plan.model_copy(
                    update={
                        "channel": "structured",
                        "operation": "file",
                        "scope": "sku",
                        "entities": [verified_entity],
                        "select": ["third_party_reports"],
                        "relations": [],
                        "filters": [],
                        "response_mode": "deliver_files",
                    }
                )
                logger.info(
                    "Pinned visual report query sku=%s select=third_party_reports",
                    verified_visual_sku,
                )
            elif plan.channel == "structured":
                # Non-report structured queries still keep the planner's
                # operation and fields, but never its unverified entity.
                plan = plan.model_copy(update={"entities": [verified_entity]})
                logger.info(
                    "Pinned visual query entity sku=%s operation=%s",
                    verified_visual_sku,
                    plan.operation,
                )
        if (
            verified_visual_sku
            and visual_resolution.status == "unique"
            and plan.channel == "structured"
            and plan.completeness < self.settings.routing_normal_confidence_threshold
        ):
            # The read model has already verified the entity, so that doubt is
            # resolved. intent_confidence is deliberately left alone: a verified
            # SKU says nothing about what the customer actually wants.
            plan = plan.model_copy(
                update={
                    "completeness": self.settings.routing_normal_confidence_threshold,
                }
            )
        if (
            active_media
            and is_image_dependent_query(user_query)
            and visual_resolution.status != "unique"
        ):
            question = (
                "图片中识别到多个已登记商品，请提供具体 SKU/UPC 后再查询。"
                if visual_resolution.status == "ambiguous"
                else (
                    "我暂时无法从图片中唯一确认商品，请提供清晰的 SKU/UPC，"
                    "或直接发送商品名称。"
                )
            )
            plan = plan.model_copy(
                update={
                    "channel": "clarify",
                    "operation": "clarify",
                    "scope": None,
                    "entities": [],
                    "select": [],
                    "relations": [],
                    "filters": [],
                    "response_mode": "clarify",
                    # The intent can be perfectly clear (the customer does want the
                    # pictured item) while the entity stays unresolved. Expressing
                    # that as low completeness keeps the intent signal intact
                    # instead of dragging one fused score below the silence floor.
                    "completeness": 0.0,
                    "clarification": QueryClarification(
                        reason=visual_resolution.reason,
                        question=question,
                    ),
                }
            )
        logger.info(
            "Planned query channel=%s op=%s sender_role=%s mentioned_bot=%s",
            plan.channel,
            plan.operation,
            event.sender_role,
            event.mentioned_bot,
        )

        route = decide_query_route(
            plan,
            user_query,
            normal_threshold=self.settings.routing_normal_confidence_threshold,
            human_threshold=self.settings.routing_human_confidence_threshold,
        )
        logger.info(
            "Query route=%s reason=%s intent_confidence=%.3f completeness=%.3f "
            "demand_kind=%s message_act=%s",
            route.kind,
            route.reason,
            plan.intent_confidence,
            plan.completeness,
            plan.demand_kind,
            plan.message_act,
        )
        if route.kind == "silent":
            remember("user", user_query)
            return ReplyDeliverable(
                text="",
                channel_used="silent",
                plan=plan,
                suppress_outbound=True,
            )
        if route.kind == "human":
            remember("user", user_query)
            remember("assistant", route.reply_text)
            return ReplyDeliverable(
                text=route.reply_text,
                channel_used="human",
                plan=plan,
            )
        if route.kind == "clarify":
            # The route is authoritative here, not plan.channel: a structured plan
            # whose entity is not pinned down must ask the customer, never fall
            # through to the executor and answer about the wrong record.
            question = clarification_reply(user_query, clarify_question(plan))
            remember("user", user_query)
            remember("assistant", question)
            return ReplyDeliverable(text=question, channel_used="clarify", plan=plan)
        if route.kind == "soothe":
            # 非标准需求：只安抚客户，绝不反问；改由私聊提醒群负责人跟进。
            soothe_text = self.compose_soothe_text(
                user_query=user_query,
                history=history,
                task_input=task_input,
            )
            remember("user", user_query)
            remember("assistant", soothe_text)
            owner_notice = build_owner_notice(
                self.groups_config,
                chat_id=chat_id,
                sender_id=event.sender_id,
                user_query=user_query,
            )
            logger.info(
                "Non-standard demand routed to soothe: chat=%s owner_reminder=%s text=%s",
                chat_id,
                bool(owner_notice),
                soothe_text,
            )
            return ReplyDeliverable(
                text=soothe_text,
                channel_used="soothe",
                plan=plan,
                owner_notify_text=owner_notice,
            )

        # 3. Dispatch by channel
        if plan.channel == "structured":
            exec_res: ExecutionResult = self.query_executor.execute_to_result(plan)
            if not exec_res.is_success:
                failure_reason = owner_notice_reason_for_failure(exec_res)
                if failure_reason:
                    # 标准需求却答不出来 → "资料不足 + 私聊转人工"，不静默也不编造。
                    remember("user", user_query)
                    reply = self.insufficient_data_reply(
                        plan=plan,
                        chat_id=chat_id,
                        sender_id=event.sender_id,
                        user_query=user_query,
                        history=history,
                        reason=failure_reason,
                        task_input=task_input,
                    )
                    remember("assistant", reply.text)
                    return reply
                # 规划器自带的澄清问句（面向客户，可转发）。
                # clarify_question() 而不是 plan.clarification.question —— 后者在
                # model_copy(update=...) 产出的未校验 dict 上会 AttributeError。
                answer = (
                    exec_res.text
                    or clarify_question(plan, default="")
                    or _DEFAULT_CLARIFY_QUESTION
                )
                remember("user", user_query)
                remember("assistant", answer)
                return ReplyDeliverable(
                    text=answer,
                    channel_used="structured",
                    plan=plan,
                )

            # Compose natural language answer
            data_dict = exec_res.metadata.get("data", {})
            context_blocks: list[str] = []
            source_info = data_dict.get("source")
            if source_info and isinstance(source_info, dict):
                fields = source_info.get("fields", {})
                scope_name = source_info.get("scope", "")
                scope_title = SCOPE_LABELS.get(scope_name, f"主体信息 ({scope_name})")
                lines = [f"【{scope_title}】"]
                for k, v in fields.items():
                    if v is not None and str(v).strip():
                        label = FIELD_LABELS.get(k, k)
                        lines.append(f"· {label}：{v}")
                if len(lines) > 1:
                    context_blocks.append("\n".join(lines))

            records = data_dict.get("records", [])
            if records:
                for idx, r in enumerate(records, 1):
                    r_scope = r.get("scope", "")
                    r_fields = r.get("fields", {})
                    rel_name = r.get("relation") or ""
                    scope_title = SCOPE_LABELS.get(r_scope, f"记录 ({r_scope})")
                    if rel_name:
                        block_header = f"【关联{scope_title}】"
                    else:
                        block_header = f"【匹配记录 {idx} ({scope_title})】"
                    lines = [block_header]
                    for k, v in r_fields.items():
                        if v is not None and str(v).strip():
                            label = FIELD_LABELS.get(k, k)
                            lines.append(f"· {label}：{v}")

                    r_attachments = r.get("attachments") or {}
                    if isinstance(r_attachments, dict):
                        for att_field, items in r_attachments.items():
                            if isinstance(items, list) and items:
                                names = [
                                    item.get("display_name")
                                    for item in items
                                    if isinstance(item, dict) and item.get("display_name")
                                ]
                                if names:
                                    att_label = FIELD_LABELS.get(att_field, att_field)
                                    lines.append(f"· {att_label}：{'、'.join(names)}")

                    if len(lines) > 1:
                        context_blocks.append("\n".join(lines))

            count_value = data_dict.get("count")
            if not context_blocks and not exec_res.deliverables and isinstance(
                count_value, int
            ):
                # 计数类计划的信封只有 {count: N, records: [], items: []}。Engine
                # 只渲染 source/records，`count` 一直被丢掉 → context_blocks 为空 →
                # raw_answer 退回"查询成功。" → 模型拿着空事实只能答"暂未记录该信息"。
                # 实测：`周口美腾实业有限公司有几份医护级认证？` 与商标注册证份数
                # 都栽在这里。把这个数字显式带进上下文即可修复。
                context_blocks.append(f"【匹配记录数】\n· 共 {count_value} 条")

            if exec_res.deliverables:
                file_lines = [f"已找到 {len(exec_res.deliverables)} 个原始文件。"]
                file_lines.extend(f"· {item.filename}" for item in exec_res.deliverables)
                raw_answer = "\n".join(file_lines)
                # The file list is authoritative. Do not let the LLM negate an
                # attachment that the executor has already resolved.
                polite_answer = raw_answer
            elif context_blocks or exec_res.text:
                # exec_res.text is a real fact source the executor may populate
                # directly; the literal "查询成功。" placeholder it used to fall
                # back to was a fabricated fact, and is gone.
                raw_answer = "\n\n".join(context_blocks) if context_blocks else exec_res.text
                polite_answer = self.compose_reply_text(
                    user_query=user_query,
                    context=raw_answer,
                    history=history,
                    task_input=task_input,
                )
            else:
                # status=ok 却没有任何可陈述的事实：以前会拿假文案"查询成功。"
                # 去喂模型，客户收到的却是"暂未记录该信息"。现在按"资料不足 +
                # 转人工"处理（负责人决策 2026-09-18）。
                remember("user", user_query)
                reply = self.insufficient_data_reply(
                    plan=plan,
                    chat_id=chat_id,
                    sender_id=event.sender_id,
                    user_query=user_query,
                    history=history,
                    reason="no_evidence",
                    task_input=task_input,
                )
                remember("assistant", reply.text)
                return reply
            polite_answer = clean_wechat_text(polite_answer)
            remember("user", user_query)
            remember("assistant", polite_answer)
            return ReplyDeliverable(
                text=polite_answer,
                deliverables=exec_res.deliverables,
                channel_used="structured",
                plan=plan,
            )

        if plan.channel == "narrative":
            # Search FastGPT official knowledge base
            chunks = []
            search_failed = False
            if self.settings.fastgpt_dataset_id:
                try:
                    chunks = self.fastgpt.search_dataset(
                        dataset_id=self.settings.fastgpt_dataset_id,
                        text=user_query,
                        limit=4,
                    )
                except Exception as exc:
                    logger.warning("FastGPT search failed: %s", exc)
                    search_failed = True

            if search_failed and not chunks:
                # 知识库**没能查**（网络/重定向/服务异常）不等于"知识库里没有"。
                # 直接答"暂未记录该信息"会把基础设施故障说成事实缺失，客户会以为
                # 我们真没有这块知识。按"资料不足 + 转人工"处理。
                remember("user", user_query)
                reply = self.insufficient_data_reply(
                    plan=plan,
                    chat_id=chat_id,
                    sender_id=event.sender_id,
                    user_query=user_query,
                    history=history,
                    reason="unanswerable",
                    task_input=task_input,
                )
                remember("assistant", reply.text)
                return reply

            knowledge_snippets = []
            for c in chunks:
                header = f"【{c.q}】" if c.q else f"【来源: {c.source_name or '官网知识库'}】"
                body = c.a.strip() if c.a else c.q.strip()
                knowledge_snippets.append(f"{header}\n{body}")
            combined_knowledge = "\n\n".join(knowledge_snippets)
            answer = self.compose_reply_text(
                user_query=user_query,
                context=combined_knowledge,
                history=history,
                task_input=task_input,
            )
            remember("user", user_query)
            remember("assistant", answer)
            return ReplyDeliverable(
                text=answer,
                channel_used="narrative",
                plan=plan,
            )

        if plan.channel == "clarify":
            # Defensive fallback only: decide_query_route already resolves every
            # channel=="clarify" plan to the clarify or soothe route above, so
            # this is unreachable on the normal path. Kept consistent with the
            # routing rule so a future refactor cannot silently reintroduce a
            # clarifying question for a non-standard demand.
            remember("user", user_query)
            if plan.demand_kind == "non_standard":
                soothe = self.compose_soothe_text(
                    user_query=user_query,
                    history=history,
                    task_input=task_input,
                )
                remember("assistant", soothe)
                return ReplyDeliverable(
                    text=soothe,
                    channel_used="soothe",
                    plan=plan,
                    owner_notify_text=build_owner_notice(
                        self.groups_config,
                        chat_id=chat_id,
                        sender_id=event.sender_id,
                        user_query=user_query,
                    ),
                )
            question = clarification_reply(user_query, clarify_question(plan))
            remember("assistant", question)
            return ReplyDeliverable(text=question, channel_used="clarify", plan=plan)

        # Default direct channel
        direct_reply = self.compose_reply_text(
            user_query=user_query,
            context="",
            history=history,
            task_input=task_input,
        )
        remember("user", user_query)
        remember("assistant", direct_reply)
        return ReplyDeliverable(text=direct_reply, channel_used="direct", plan=plan)

    def dispatch_outbound(self, to_wxid: str, reply: ReplyDeliverable) -> DispatchOutcome:
        """Send response text and deliverables via WeChat GeWe client.

        Attachments are deduplicated by media_id and dispatched strictly one at a
        time with a pause between sends. GeWe pulls every ``fileUrl`` server-side
        under its own short download timeout, so parallel sends contend for the
        tunnel's upstream bandwidth and make that timeout far more likely.

        Never raises; the returned :class:`DispatchOutcome` says which pieces
        were actually handed to GeWe, and the chat archive records only those.
        """
        if reply.suppress_outbound:
            logger.info("Outbound suppressed for channel=%s", reply.channel_used)
            return DispatchOutcome(suppressed=True)
        # 1. Send text reply
        sent_text = False
        text_content = reply.text
        text_quote = None
        message_id = ""
        text_status = "not_attempted"
        if reply.reply_source is not None:
            policy = self.groups_config.get(to_wxid)
            source = reply.reply_source
            if policy and policy.native_quote_replies and source.chat_id == to_wxid:
                if source.native_ready:
                    text_quote = source
                else:
                    text_content = source.fallback_text(reply.text)
                    logger.info("Native quote unavailable; using contextual text chat=%s", to_wxid)
        if text_content.strip():
            text_status = "unknown"
            try:
                if text_quote:
                    result = self.wechat.send_quoted_text(
                        to_wxid=to_wxid, content=text_content,
                        quoted_msg_id=text_quote.message_id,
                        quoted_sender_id=text_quote.sender_id,
                        quoted_chat_id=text_quote.chat_id,
                        quoted_content=text_quote.content,
                        quoted_sender_name=text_quote.sender_name,
                        quoted_created_at=text_quote.created_at,
                        quoted_msg_source=text_quote.msg_source,
                        quoted_str_id=text_quote.str_id,
                        quoted_imunion_sender_id=text_quote.imunion_sender_id,
                    )
                else:
                    result = self.wechat.send_text(to_wxid=to_wxid, content=text_content)
                sent_text = True
                text_status = "sent"
                result_data = result.get("data") if isinstance(result, dict) else None
                if isinstance(result_data, dict):
                    message_id = str(result_data.get("newMsgId") or "")
            except Exception as exc:
                # Never resend immediately after an ambiguous transport outcome.
                if isinstance(exc, GeweError):
                    text_status = "failed"
                logger.error("Failed to send text to %s (status=%s): %s", to_wxid, text_status, exc)

        # 2. Deliver attachments if any
        attachments = dedupe_deliverables(reply.deliverables)
        if len(attachments) < len(reply.deliverables):
            logger.info(
                "Deduplicated attachments %d -> %d for %s",
                len(reply.deliverables),
                len(attachments),
                to_wxid,
            )

        sent_media_ids: list[str] = []
        interval = dispatch_interval_seconds(self)
        already_sent = False
        for deliv in attachments:
            media_url = deliv.download_url
            if not media_url:
                logger.error("No public media URL for attachment %s", deliv.media_id)
                continue

            if already_sent and interval > 0:
                time.sleep(interval)
            already_sent = True

            logger.info(
                "Dispatching attachment %s (media_id=%s, size=%s bytes) to %s "
                "with file_url=%s",
                deliv.filename,
                deliv.media_id,
                deliv.size_bytes,
                to_wxid,
                media_url,
            )
            try:
                if Path(deliv.filename).suffix.lower() in _IMAGE_SUFFIXES:
                    # Send image attachment
                    self.wechat.send_image(to_wxid=to_wxid, img_url=media_url)
                else:
                    # Send file attachment
                    self.wechat.send_file(
                        to_wxid=to_wxid,
                        file_url=media_url,
                        file_name=deliv.filename,
                    )
                sent_media_ids.append(deliv.media_id)
            except Exception as exc:
                logger.error("Failed to send attachment %s: %s", deliv.filename, exc)

        # 3. Private reminder to the group owner for a non-standard demand.
        if reply.owner_notify_text:
            self.notify_owner(to_wxid=to_wxid, text=reply.owner_notify_text)
        return DispatchOutcome(
            sent_text=sent_text, sent_media_ids=tuple(sent_media_ids), text_content=text_content,
            message_id=message_id, text_quote=text_quote, text_status=text_status,
        )

    def notify_owner(self, *, to_wxid: str, text: str) -> None:
        """Send a private reminder about a non-standard demand to a group's owner.

        The reminder goes to the group owner's wxid as a **private** message, not
        into the customer group: the group already received the soothing reply,
        and GeWe's text API has no at-list capability to mention the owner there.

        Never raises. A missing or misconfigured owner must not break the
        customer-facing reply that was already delivered.
        """
        if not self.settings.owner_notify_enabled:
            logger.info("Owner reminder disabled by settings chat=%s", to_wxid)
            return
        if not to_wxid.endswith("@chatroom"):
            logger.debug("Owner reminder skipped for non-group chat=%s", to_wxid)
            return
        policy = self.groups_config.get(to_wxid)
        owner_id = policy.owner_id if policy is not None else ""
        if not owner_id:
            logger.warning(
                "Owner reminder skipped: no owner_id configured chat=%s", to_wxid
            )
            return
        try:
            self.wechat.send_text(to_wxid=owner_id, content=text)
            logger.info(
                "Owner reminder delivered privately chat=%s owner=%s", to_wxid, owner_id
            )
        except Exception as exc:
            logger.error("Owner reminder failed chat=%s owner=%s: %s", to_wxid, owner_id, exc)
