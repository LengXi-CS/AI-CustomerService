"""Strict JSON contract for the FastGpt Assistant query executor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

QueryChannel = Literal["structured", "narrative", "clarify", "direct"]
QueryOperation = Literal[
    "record_value",
    "count",
    "list",
    "exists",
    "validity",
    "file",
    "related",
    "clarify",
]
QueryScope = Literal["factory", "sku", "category", "trademark", "batch_report"]
ResponseMode = Literal["answer", "deliver_files", "answer_and_files", "clarify"]
FilterOperator = Literal["eq", "in", "contains"]
# 会议决议（2026-09-18）：澄清属于标准需求 —— 命中标准需求但信息不足才反问澄清；
# 非标准需求只做安抚并提醒负责人。二者必须由规划器显式区分，不能靠推断。
DemandKind = Literal["standard", "non_standard"]
MessageAct = Literal["request", "statement", "unclear"]
ATTACHMENT_NAME_FILTER = "attachment_name"


@dataclass(frozen=True, slots=True)
class AttachmentFieldSpec:
    """Map one safe logical attachment field to its source column."""

    scope: str
    source_field: str


# Logical names are the only attachment selectors exposed to the LLM.
ATTACHMENT_FIELDS: dict[str, AttachmentFieldSpec] = {
    "product_documents": AttachmentFieldSpec("category", "产品资料整体压缩"),
    "third_party_reports": AttachmentFieldSpec("category", "第三方检测报告（附件）"),
    "product_standards": AttachmentFieldSpec("category", "产品技术标准"),
    "registration_files": AttachmentFieldSpec("category", "注册/备案信息"),
    "production_licenses": AttachmentFieldSpec("category", "生产许可证"),
    "trademark_certificates": AttachmentFieldSpec("trademark", "商标注册证"),
    "trademark_transfer_agreements": AttachmentFieldSpec("trademark", "商标转让协议"),
    "trademark_authorizations": AttachmentFieldSpec("trademark", "商标授权"),
    "trademark_summaries": AttachmentFieldSpec("trademark", "商标汇总"),
    "factory_business_licenses": AttachmentFieldSpec("factory", "营业执照（附件）"),
    "factory_processing_agreements": AttachmentFieldSpec("factory", "委托加工协议"),
    "factory_barcode_certificates": AttachmentFieldSpec("factory", "工厂条码证"),
    "factory_medical_certificates": AttachmentFieldSpec("factory", "医护级认证"),
    "factory_medical_certificate_standards": AttachmentFieldSpec(
        "factory", "医护级认证技术标准"
    ),
    "factory_special_care_certificates": AttachmentFieldSpec("factory", "特护级认证"),
    "factory_special_care_certificate_standards": AttachmentFieldSpec(
        "factory", "特护级认证技术标准"
    ),
    "factory_antibacterial_certificates": AttachmentFieldSpec(
        "factory", "抗菌产品及中卫安认证"
    ),
    "factory_antibacterial_requirements": AttachmentFieldSpec(
        "factory", "抗菌产品及中卫安认证技术要求"
    ),
    "sku_main_image_details": AttachmentFieldSpec("sku", "主图详情（附件）含广审图"),
    "sku_product_front": AttachmentFieldSpec("sku", "产品正面"),
    "sku_product_back_label": AttachmentFieldSpec("sku", "产品背面标签"),
    "sku_packaging_images": AttachmentFieldSpec("sku", "新包装&白底图&背标"),
    "sku_advertising_images": AttachmentFieldSpec("sku", "广审图"),
    "sku_advertising_files": AttachmentFieldSpec("sku", "广审文件"),
    "batch_report_files": AttachmentFieldSpec("batch_report", "批次检测报告"),
    # Backward-compatible aliases
    "factory_certificates": AttachmentFieldSpec("factory", "医护级认证"),
    "main_image_details": AttachmentFieldSpec("sku", "主图详情（附件）含广审图"),
    "product_front": AttachmentFieldSpec("sku", "产品正面"),
    "product_back_label": AttachmentFieldSpec("sku", "产品背面标签"),
    "packaging_images": AttachmentFieldSpec("sku", "新包装&白底图&背标"),
    "advertising_images": AttachmentFieldSpec("sku", "广审图"),
    "advertising_files": AttachmentFieldSpec("sku", "广审文件"),
}


def attachment_fields_for_scope(scope: str) -> set[str]:
    """Return logical attachment fields valid for one planner scope."""
    return {
        name
        for name, spec in ATTACHMENT_FIELDS.items()
        if spec.scope == scope
    }


class QueryEntity(BaseModel):
    """An entity candidate supplied by the planner."""

    model_config = ConfigDict(extra="forbid")

    type: str | None = None
    id: str | None = Field(default=None, min_length=1, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=500)


class QueryFilter(BaseModel):
    """One allowlisted, parameterized filter."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=100)
    op: FilterOperator
    value: Annotated[str | list[str], Field(min_length=1)]


class QueryRelation(BaseModel):
    """A named relation registered by the read model."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1, max_length=100)
    from_path: str | None = Field(default=None, alias="from")
    to_path: str | None = Field(default=None, alias="to")


class QueryClarification(BaseModel):
    """Structured clarification payload when entity is ambiguous or missing."""

    model_config = ConfigDict(extra="ignore")

    reason: str | None = None
    question: str | None = None


def _confidence_scalar(value: Any) -> float:
    """Map one raw planner confidence onto the closed interval [0, 1]."""
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        val_lower = value.strip().lower()
        if val_lower in {"high", "高"}:
            return 0.9
        if val_lower in {"medium", "med", "中"}:
            return 0.6
        if val_lower in {"low", "低"}:
            return 0.3
        try:
            num = float(value)
            return max(0.0, min(1.0, num))
        except ValueError:
            return 0.8
    return 0.8


class QueryPlan(BaseModel):
    """The standard plan shape accepted by the executor.

    Planner certainty is deliberately split into two independent signals:

    * ``intent_confidence`` — how sure the planner is about *what the customer
      wants* (intent, operation, scope);
    * ``completeness`` — how sure it is that the *information needed to execute*
      (entity plus fields) is present.

    They are read separately by the router. Collapsing them into one number is
    what made an ambiguous image drag the whole score down and silence a
    perfectly well-understood demand — see
    ``架构诊断-越改越不如意的根因.md`` 根因 3.

    ``demand_kind`` says whether a request maps onto a standard demand. The
    independent ``message_act`` says whether the customer made a request at all.
    Older plans omit ``message_act`` and retain the confidence-based route.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["query-plan-v1"] = "query-plan-v1"
    channel: QueryChannel
    operation: QueryOperation
    scope: QueryScope | None = None
    entities: list[QueryEntity] = Field(default_factory=list, max_length=10)
    select: list[str] = Field(default_factory=list, max_length=30)
    relations: list[QueryRelation] = Field(default_factory=list, max_length=4)
    filters: list[QueryFilter] = Field(default_factory=list, max_length=10)
    response_mode: ResponseMode = "answer"
    intent_confidence: float = Field(ge=0, le=1)
    completeness: float = Field(ge=0, le=1)
    demand_kind: DemandKind = "standard"
    message_act: MessageAct | None = None
    clarification: QueryClarification | None = None

    @model_validator(mode="before")
    @classmethod
    def _expand_planner_confidence(cls, data: Any) -> Any:
        """Accept the pre-split single ``confidence`` field and single-signal output.

        Two compatibility paths, both fail-soft rather than rejecting a live
        customer message:

        1. A caller still sending ``confidence`` (stored fixtures, the FastGPT
           workflow, older prompt output) gets it copied into both signals, which
           reproduces the previous behavior exactly.
        2. A planner that answered only one of the two signals falls back to the
           value it did provide. That is safe: the executor remains the
           fail-closed backstop for a genuinely incomplete plan.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        legacy = data.pop("confidence", None)
        if legacy is not None:
            data.setdefault("intent_confidence", legacy)
            data.setdefault("completeness", legacy)
            return data

        intent = data.get("intent_confidence")
        completeness = data.get("completeness")
        if intent is not None and completeness is None:
            data["completeness"] = intent
        elif completeness is not None and intent is None:
            data["intent_confidence"] = completeness
        return data

    @field_validator("intent_confidence", "completeness", mode="before")
    @classmethod
    def _normalize_confidence(cls, value: Any) -> float:
        return _confidence_scalar(value)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def confidence(self) -> float:
        """Derived fusion of the two signals — observability and JSON compat only.

        Never route on this. Reading one fused number instead of the two
        independent signals is exactly the defect this field pair replaces.
        """
        return min(self.intent_confidence, self.completeness)

    @field_validator("entities", mode="before")
    @classmethod
    def _normalize_entities(cls, value: Any) -> Any:
        if isinstance(value, dict):
            norm: list[dict[str, Any]] = []
            for entity_type, entity_val in value.items():
                if isinstance(entity_val, dict):
                    eid = (
                        entity_val.get("id")
                        or entity_val.get("sku_code")
                        or entity_val.get("factory_id")
                        or entity_val.get("category_id")
                        or entity_val.get("trademark_code")
                    )
                    ename = (
                        entity_val.get("name")
                        or entity_val.get("product_name")
                        or entity_val.get("factory_name")
                    )
                    norm.append(
                        {
                            "type": entity_type,
                            "id": str(eid) if eid else None,
                            "name": str(ename) if ename else None,
                        }
                    )
                elif isinstance(entity_val, str):
                    norm.append({"type": entity_type, "id": entity_val})
                elif isinstance(entity_val, list):
                    for sub in entity_val:
                        if isinstance(sub, str):
                            norm.append({"type": entity_type, "id": sub})
                        elif isinstance(sub, dict):
                            norm.append(sub)
            return norm
        return value

    @field_validator("relations", mode="before")
    @classmethod
    def _normalize_relations(cls, value: Any) -> Any:
        if isinstance(value, list):
            norm: list[Any] = []
            for item in value:
                if isinstance(item, str):
                    norm.append({"name": item})
                else:
                    norm.append(item)
            return norm
        return value


class QueryExecuteRequest(BaseModel):
    """HTTP envelope for a planner output and its audit context."""

    model_config = ConfigDict(extra="forbid")

    plan: QueryPlan
    request_id: str = Field(default="", max_length=200)
    session: dict[str, str] = Field(default_factory=dict)
