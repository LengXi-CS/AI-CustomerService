"""Deterministic SQLite query execution engine for FastGPT Assistant.

Executes structured query plans (query-plan-v1) directly against the local
SQLite read model, supporting count, list, record_value, exists, validity,
file, and related operations without circular Docker network calls.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastgpt_assistant.core.media_store import MediaStore
from fastgpt_assistant.core.models import Deliverable, ExecutionResult, Provenance
from fastgpt_assistant.core.schema import (
    ATTACHMENT_FIELDS,
    ATTACHMENT_NAME_FILTER,
    QueryEntity,
    QueryPlan,
    QueryRelation,
)


@dataclass(frozen=True, slots=True)
class _Scope:
    """Allowlisted database mapping for one planner scope."""

    name: str
    table: str
    record_id_column: str
    business_id_column: str
    name_column: str | None
    fields: dict[str, str]


_SCOPES: dict[str, _Scope] = {
    "factory": _Scope(
        "factory",
        "factories",
        "record_id",
        "factory_id",
        "factory_name",
        {"record_id": "record_id", "factory_id": "factory_id", "factory_name": "factory_name"},
    ),
    "sku": _Scope(
        "sku",
        "skus",
        "sku_code",
        "sku_code",
        "product_name",
        {
            "record_id": "sku_code",
            "sku_code": "sku_code",
            "product_name": "product_name",
            "trademark_code": "trademark_code",
            "factory_id": "factory_id",
            "category_id": "category_id",
            "product_size": "product_size",
        },
    ),
    "category": _Scope(
        "category",
        "categories",
        "record_id",
        "category_id",
        "category_name",
        {
            "record_id": "record_id",
            "category_id": "category_id",
            "category_name": "category_name",
            "factory_id": "factory_id",
        },
    ),
    "trademark": _Scope(
        "trademark",
        "trademarks",
        "trademark_code",
        "trademark_code",
        "trademark_name",
        {
            "record_id": "trademark_code",
            "trademark_code": "trademark_code",
            "trademark_name": "trademark_name",
        },
    ),
    "batch_report": _Scope(
        "batch_report",
        "batch_reports",
        "sku_code",
        "sku_code",
        None,
        {
            "record_id": "sku_code",
            "sku_code": "sku_code",
            "batch_no": "batch_no",
            "report_name": "report_name",
        },
    ),
}

_TABLE_TO_SCOPE = {scope.table: scope for scope in _SCOPES.values()}
_ENTITY_TYPES = {
    "factory": {"factory", "record"},
    "sku": {"sku", "record"},
    "category": {"category", "record"},
    "trademark": {"trademark", "record"},
    "batch_report": {"batch_report", "sku", "record"},
}

_RELATIONS: dict[str, tuple[str, str, str]] = {
    "sku_to_factory": ("sku", "factory", "factories"),
    "sku_to_category": ("sku", "category", "categories"),
    "sku_to_trademark": ("sku", "trademark", "trademarks"),
    "category_to_factory": ("category", "factory", "factories"),
    "batch_to_sku": ("batch_report", "sku", "skus"),
    "factory_to_sku": ("factory", "sku", "skus"),
    "category_to_sku": ("category", "sku", "skus"),
    "trademark_to_sku": ("trademark", "sku", "skus"),
    "factory_to_category": ("factory", "category", "categories"),
    "sku_to_batch": ("sku", "batch_report", "batch_reports"),
}

_CROSS_SCOPE_SELECT_RELATIONS: dict[str, dict[str, str]] = {
    "sku": {
        "factory_name": "sku_to_factory",
        "category_name": "sku_to_category",
        "trademark_name": "sku_to_trademark",
    },
    "category": {"factory_name": "category_to_factory"},
    "factory": {"category_name": "factory_to_category"},
    "trademark": {"product_name": "trademark_to_sku"},
    "batch_report": {"product_name": "batch_to_sku"},
}

_DOMAIN_TABLES = ("factories", "skus", "categories", "trademarks", "batch_reports")


class QueryPlanRejected(ValueError):
    """A plan failed a deterministic safety or contract check."""

    def __init__(self, reason: str, message: str, *, status: str = "clarify") -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = status


# Transports of ``QueryPlanRejected`` whose messages are **internal** validation
# text ("file 和 validity 操作必须选择一个已注册的附件字段。"). They ride the
# same ``status="clarify"`` channel as the planner's own customer-facing
# questions, so anything consuming ``ExecutionResult.text`` must check this set
# before forwarding it — otherwise an English-condition-free internals message
# ends up in a customer's WeChat bubble.
PLAN_REJECTION_REASONS = frozenset(
    {
        "missing_entity",
        "too_many_entities",
        "missing_attachment_field",
        "invalid_filter",
        "missing_scope",
        "unknown_scope",
    }
)


def _plan_id(plan: QueryPlan) -> str:
    """Generate a stable, non-secret identifier for audit correlation."""
    payload = json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return "plan_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _repair_cross_scope_select(plan: QueryPlan) -> QueryPlan:
    """Repair a safe, common planner omission before strict validation."""
    if plan.channel != "structured" or plan.operation not in {
        "record_value",
        "list",
        "file",
        "related",
    }:
        return plan

    if (
        len(plan.entities) == 1
        and plan.entities[0].type == "sku"
        and plan.scope in {"category", "factory", "trademark"}
    ):
        target_scope = plan.scope
        needed_rel = f"sku_to_{target_scope}"
        relations = list(plan.relations)
        if needed_rel in _RELATIONS and not any(r.name == needed_rel for r in relations):
            relations.append(QueryRelation(name=needed_rel))
        new_response_mode = (
            "deliver_files"
            if (
                plan.operation == "file"
                and plan.response_mode not in {"deliver_files", "answer_and_files"}
            )
            else plan.response_mode
        )
        plan = plan.model_copy(
            update={
                "scope": "sku",
                "operation": "related",
                "relations": relations,
                "response_mode": new_response_mode,
            }
        )

    source_scope = plan.scope or ""
    relation_by_field = _CROSS_SCOPE_SELECT_RELATIONS.get(source_scope, {})
    needed_relations: list[str] = []
    for field in plan.select:
        relation_name = relation_by_field.get(field)
        if relation_name is None:
            attachment = ATTACHMENT_FIELDS.get(field)
            if attachment is not None and attachment.scope != source_scope:
                relation_name = next(
                    (
                        name
                        for name, (from_scope, to_scope, _) in _RELATIONS.items()
                        if from_scope == source_scope and to_scope == attachment.scope
                    ),
                    None,
                )
        if relation_name is not None:
            needed_relations.append(relation_name)
    if not needed_relations:
        return plan

    relations = list(plan.relations)
    existing = {relation.name for relation in relations}
    for relation_name in needed_relations:
        if relation_name not in existing:
            relations.append(QueryRelation(name=relation_name))
            existing.add(relation_name)
    return plan.model_copy(update={"operation": "related", "relations": relations})


class ReadModel:
    """Read-only access facade for the SQLite read model database."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).resolve()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Open the database in SQLite read-only mode."""
        if not self.database_path.is_file():
            raise FileNotFoundError(f"Database not found: {self.database_path}")
        conn = sqlite3.connect(f"{self.database_path.as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def table_counts(self) -> dict[str, int]:
        """Return row counts for domain tables."""
        with self.connection() as conn:
            return {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in _DOMAIN_TABLES
            }


class SqliteQueryExecutor:
    """Execute query plans safely without exposing raw SQL generation."""

    def __init__(
        self,
        read_model: ReadModel,
        media_store: MediaStore | None = None,
        media_base_url: str = "",
    ) -> None:
        self.read_model = read_model
        self.media_store = media_store
        self.media_base_url = media_base_url.rstrip("/")

    def execute(self, plan: QueryPlan, *, request_id: str = "") -> dict[str, Any]:
        """Validate and execute a plan, returning a structured response envelope."""
        plan = _repair_cross_scope_select(plan)
        plan_id = _plan_id(plan)
        response: dict[str, Any] = {
            "status": "ok",
            "plan_id": plan_id,
            "request_id": request_id,
            "snapshot_id": None,
            "data": {"count": 0, "records": [], "items": []},
            "provenance": [],
            "warnings": [],
            "clarify": None,
        }

        if plan.channel == "clarify" or plan.operation == "clarify":
            response["status"] = "clarify"
            response["clarify"] = plan.clarification or {
                "reason": "planner_clarification",
                "question": "请补充具体的查询对象。",
            }
            return response

        try:
            self._validate_plan(plan)
        except QueryPlanRejected as exc:
            response["status"] = exc.status
            response["clarify"] = {"reason": exc.reason, "question": exc.message}
            return response

        try:
            with self.read_model.connection() as connection:
                snapshot = connection.execute(
                    "SELECT snapshot_id FROM source_snapshots WHERE status = 'ready' "
                    "ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if snapshot is None:
                    response["status"] = "data_not_ready"
                    response["warnings"] = ["SQLite read model snapshot is not ready"]
                    return response
                response["snapshot_id"] = str(snapshot[0])

                if plan.operation == "related":
                    self._execute_related(connection, plan, response)
                else:
                    self._execute_rows(connection, plan, response)
        except FileNotFoundError:
            response["status"] = "data_not_ready"
            response["warnings"] = ["SQLite read model database not found"]
        except sqlite3.Error as exc:
            response["status"] = "error"
            response["warnings"] = [f"SQLite query failed: {exc}"]

        return response

    def execute_to_result(self, plan: QueryPlan, *, request_id: str = "") -> ExecutionResult:
        """Execute a query plan and convert outcome into an ExecutionResult."""
        envelope = self.execute(plan, request_id=request_id)
        status_str = envelope.get("status", "error")

        provenances: list[Provenance] = []
        for prov in envelope.get("provenance", []):
            if isinstance(prov, dict):
                provenances.append(
                    Provenance(
                        table=str(prov.get("table") or ""),
                        record_id=str(prov.get("record_id") or ""),
                        field_name=",".join(prov.get("fields") or []),
                    )
                )

        deliverables: list[Deliverable] = []
        data = envelope.get("data", {})
        for d in data.get("deliverables", []):
            if isinstance(d, dict) and d.get("media_id"):
                media_id = str(d["media_id"])
                filename = str(d.get("name") or "attachment")
                deliverables.append(
                    Deliverable(
                        media_id=media_id,
                        filename=filename,
                        mime_type=mimetypes.guess_type(filename)[0]
                        or "application/octet-stream",
                        download_url=(
                            f"{self.media_base_url}/v1/media/{media_id}{Path(filename).suffix}"
                            if self.media_base_url
                            else ""
                        ),
                    )
                )

        if status_str == "ok":
            return ExecutionResult(
                status="answered",
                text="",
                deliverables=deliverables,
                provenances=provenances,
                metadata=envelope,
            )
        if status_str in {"not_found", "data_not_ready"}:
            return ExecutionResult(
                status="no_evidence",
                text="未找到相关数据或附件记录。",
                deliverables=[],
                provenances=[],
                metadata=envelope,
            )
        if status_str in {"clarify", "ambiguous"}:
            clarify_data = envelope.get("clarify") or {}
            question = str(clarify_data.get("question") or "请补充查询信息。")
            return ExecutionResult(
                status="clarify",
                text=question,
                deliverables=[],
                provenances=[],
                metadata=envelope,
            )

        return ExecutionResult(
            status="error",
            text="; ".join(envelope.get("warnings") or ["查询执行异常"]),
            deliverables=[],
            provenances=[],
            metadata=envelope,
        )

    def _validate_plan(self, plan: QueryPlan) -> None:
        if plan.channel != "structured":
            raise QueryPlanRejected(
                "unsupported_channel",
                "只支持 structured 渠道的查询计划。",
                status="unsupported_relation",
            )
        if plan.scope is None:
            raise QueryPlanRejected("missing_scope", "请明确查询的表范围。")
        if plan.scope not in _SCOPES:
            raise QueryPlanRejected("unknown_scope", f"查询表范围未注册: {plan.scope}")

        scope = _SCOPES[plan.scope]
        for entity in plan.entities:
            if entity.type and entity.type not in _ENTITY_TYPES[plan.scope]:
                raise QueryPlanRejected("scope_entity_conflict", "实体类型与表范围不一致。")

        if (
            plan.operation in {"record_value", "exists", "validity"}
            and len(plan.entities) != 1
        ) or (
            plan.operation in {"file", "related"}
            and len(plan.entities) < 1
        ):
            raise QueryPlanRejected("missing_entity", "该查询需要一个明确的实体。")

        if len(plan.entities) > 1 and plan.operation not in {"count", "list", "file", "related"}:
            raise QueryPlanRejected("too_many_entities", "当前操作只支持一个实体。")

        if plan.operation != "related":
            selected = plan.select or self._default_select(scope)
            self._validate_fields(scope, selected)
            attachment_selected = any(field in ATTACHMENT_FIELDS for field in selected)
            batch_report_reference = (
                plan.operation == "file"
                and plan.scope == "batch_report"
                and "report_name" in selected
            )
            if (
                plan.operation in {"file", "validity"}
                and not attachment_selected
                and not batch_report_reference
            ):
                raise QueryPlanRejected(
                    "missing_attachment_field",
                    "file 和 validity 操作必须选择一个已注册的附件字段。",
                )

        filename_filters = [
            item for item in plan.filters if item.field == ATTACHMENT_NAME_FILTER
        ]
        if filename_filters:
            if plan.operation not in {"count", "list", "exists", "validity", "file"}:
                raise QueryPlanRejected(
                    "invalid_filter",
                    "attachment_name 过滤器只能用于附件查询。",
                )
            if len(filename_filters) > 1:
                raise QueryPlanRejected(
                    "invalid_filter",
                    "attachment_name 过滤器只能使用一次。",
                )
            selected = plan.select or self._default_select(scope)
            attachment_scopes = {scope.name}
            if plan.operation == "related":
                attachment_scopes.update(
                    registered[1]
                    for relation in plan.relations
                    if (registered := _RELATIONS.get(relation.name))
                    and registered[0] == scope.name
                )
            if not any(
                field in ATTACHMENT_FIELDS
                and ATTACHMENT_FIELDS[field].scope in attachment_scopes
                for field in selected
            ):
                raise QueryPlanRejected(
                    "invalid_filter",
                    "attachment_name 过滤器需要选择附件字段。",
                )
            filename_filter = filename_filters[0]
            if filename_filter.op != "eq" or not isinstance(filename_filter.value, str):
                raise QueryPlanRejected(
                    "invalid_filter",
                    "attachment_name 过滤器只支持完整文件名的 eq 匹配。",
                )

        for item in plan.filters:
            if item.field == ATTACHMENT_NAME_FILTER:
                continue
            if item.field not in scope.fields:
                raise QueryPlanRejected("unknown_filter", f"字段未注册：{item.field}")
            if item.op == "eq" and isinstance(item.value, list):
                raise QueryPlanRejected("invalid_filter", "eq 条件只能使用单个值。")
            if item.op == "contains" and isinstance(item.value, list):
                raise QueryPlanRejected("invalid_filter", "contains 条件只能使用单个值。")

        if plan.operation == "related":
            if not plan.relations:
                raise QueryPlanRejected(
                    "missing_relation",
                    "关联查询必须声明已注册的关系。",
                    status="unsupported_relation",
                )
            for relation in plan.relations:
                registered = _RELATIONS.get(relation.name)
                if registered is None:
                    raise QueryPlanRejected(
                        "unknown_relation",
                        f"关系未注册：{relation.name}",
                        status="unsupported_relation",
                    )
                if registered[0] != plan.scope:
                    raise QueryPlanRejected(
                        "relation_direction",
                        f"关系 {relation.name} 不能从 {plan.scope} 发起。",
                        status="unsupported_relation",
                    )
                target_scope = _SCOPES[registered[1]]
                selected = self._related_select(plan.select, target_scope)
                self._validate_fields(target_scope, selected)

    @staticmethod
    def _default_select(scope: _Scope) -> list[str]:
        fields = list(scope.fields)
        return [field for field in fields if field != "record_id"]

    @staticmethod
    def _validate_fields(scope: _Scope, fields: list[str]) -> None:
        for field in fields:
            attachment = ATTACHMENT_FIELDS.get(field)
            if field in scope.fields:
                continue
            if attachment is not None and attachment.scope == scope.name:
                continue
            if attachment is not None:
                raise QueryPlanRejected(
                    "scope_field_conflict",
                    f"字段 {field} 不属于 {scope.name} 表范围",
                )
            raise QueryPlanRejected("unknown_field", f"字段未注册：{field}")

    def _execute_rows(
        self,
        connection: sqlite3.Connection,
        plan: QueryPlan,
        response: dict[str, Any],
    ) -> None:
        scope = _SCOPES[plan.scope or ""]
        entity_rows: list[sqlite3.Row] = []
        if plan.entities:
            if plan.operation == "file" and len(plan.entities) > 1:
                for entity in plan.entities:
                    matches = self._resolve_entity(connection, scope, entity)
                    if matches:
                        entity_rows.extend(matches)
                if not entity_rows:
                    response["status"] = "not_found"
                    response["warnings"] = ["entity not found in read model snapshot"]
                    return
            else:
                matches = self._resolve_entity(connection, scope, plan.entities[0])
                if len(matches) > 1:
                    response["status"] = "ambiguous"
                    response["clarify"] = {
                        "reason": "ambiguous_entity",
                        "question": "请从候选记录中选择一个明确对象。",
                        "candidates": [self._candidate(scope, row) for row in matches],
                    }
                    return
                if not matches:
                    response["status"] = "not_found"
                    response["warnings"] = ["entity not found in read model snapshot"]
                    return
                entity_rows = [matches[0]]

        rows, _ = self._select_rows(connection, scope, plan, entity_rows)
        selected = plan.select or self._default_select(scope)
        selected_attachment_fields = [
            field
            for field in selected
            if (attachment := ATTACHMENT_FIELDS.get(field)) is not None
            and attachment.scope == scope.name
        ]
        attachment_name = self._attachment_name_filter(plan)
        if selected_attachment_fields and plan.operation in {"count", "list"}:
            rows = [
                row
                for row in rows
                if self._attachment_count(
                    connection, scope, row, selected_attachment_fields, attachment_name
                )
            ]

        records = []
        provenance = []
        for row in rows:
            record = self._record(scope, row, selected)
            if selected_attachment_fields:
                record["attachments"] = self._attachments(
                    connection, scope, row, selected_attachment_fields, attachment_name
                )
            records.append(record)
            provenance.append(self._provenance(connection, scope, row, selected))

        response["data"]["records"] = records[:100]
        response["data"]["count"] = len(records)
        response["provenance"] = provenance[:100]

        if plan.operation == "exists":
            if selected_attachment_fields:
                attachment_count = sum(
                    self._attachment_count(
                        connection, scope, row, selected_attachment_fields, attachment_name
                    )
                    for row in rows
                )
                response["data"]["count"] = attachment_count
                response["data"]["exists"] = bool(attachment_count)
            else:
                response["data"]["exists"] = bool(records)

        if plan.operation == "count":
            if selected_attachment_fields and entity_rows:
                response["data"]["count"] = sum(
                    self._attachment_count(
                        connection, scope, row, selected_attachment_fields, attachment_name
                    )
                    for row in rows
                )
            response["data"]["records"] = []

        if plan.operation == "list" and selected_attachment_fields:
            response["data"]["count"] = sum(
                self._attachment_count(
                    connection, scope, row, selected_attachment_fields, attachment_name
                )
                for row in rows
            )

        if plan.operation == "file":
            entries = self._attachment_items(
                connection, scope, rows, selected_attachment_fields, attachment_name
            )
            response["data"]["items"] = entries
            response["data"]["deliverables"] = [
                {
                    "media_id": str(entry["media_id"]),
                    "kind": self._delivery_kind(str(entry["display_name"])),
                    "name": str(entry["display_name"]),
                    "caption": f"{scope.name} / {entry['record_id']} / {entry['logical_field']}",
                }
                for entry in entries
                if entry.get("media_id")
            ]
            response["data"]["count"] = len(response["data"]["deliverables"])
            response["data"]["records"] = []
            if not response["data"]["deliverables"]:
                response["status"] = "not_found"
                response["warnings"] = ["no available attachment found in snapshot"]

        if plan.operation == "validity":
            entries = self._attachment_items(
                connection, scope, rows, selected_attachment_fields, attachment_name
            )
            response["data"]["items"] = entries
            response["data"]["count"] = len(entries)
            response["data"]["records"] = []
            if not entries:
                response["status"] = "not_found"
                response["warnings"] = ["no available attachment found in snapshot"]
            elif any(entry.get("valid_until") is None for entry in entries):
                response["warnings"] = ["validity date is unavailable for one or more attachments"]

        if len(records) > 100:
            response["warnings"] = ["result truncated at 100 records"]

    def _select_rows(
        self,
        connection: sqlite3.Connection,
        scope: _Scope,
        plan: QueryPlan,
        entity_rows: list[sqlite3.Row] | None,
    ) -> tuple[list[sqlite3.Row], list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if entity_rows:
            placeholders = ", ".join("?" for _ in entity_rows)
            where.append(f"{scope.record_id_column} IN ({placeholders})")
            params.extend(row[scope.record_id_column] for row in entity_rows)

        for item in plan.filters:
            if item.field == ATTACHMENT_NAME_FILTER:
                continue
            column = scope.fields[item.field]
            if item.op == "eq":
                where.append(f"{column} = ?")
                params.append(item.value)
            elif item.op == "contains":
                where.append(f"{column} LIKE ?")
                params.append(f"%{item.value}%")
            else:
                values = item.value if isinstance(item.value, list) else [item.value]
                placeholders = ", ".join("?" for _ in values)
                where.append(f"{column} IN ({placeholders})")
                params.extend(values[:50])

        statement = f"SELECT * FROM {scope.table}"
        if where:
            statement += " WHERE " + " AND ".join(where)
        statement += f" ORDER BY {scope.record_id_column} LIMIT 101"
        return connection.execute(statement, params).fetchall(), params

    def _execute_related(
        self,
        connection: sqlite3.Connection,
        plan: QueryPlan,
        response: dict[str, Any],
    ) -> None:
        scope = _SCOPES[plan.scope or ""]
        source_selected = [field for field in plan.select if field in scope.fields]
        self._validate_fields(scope, source_selected)
        source_rows: list[tuple[Any, dict[str, Any]]] = []
        source_provenance: list[dict[str, Any]] = []
        warnings: list[str] = []
        incomplete = False

        for entity in plan.entities:
            matches = self._resolve_entity(connection, scope, entity)
            if len(matches) > 1:
                response["status"] = "ambiguous"
                response["clarify"] = {
                    "reason": "ambiguous_entity",
                    "question": "请从候选记录中选择一个明确对象。",
                    "candidates": [self._candidate(scope, row) for row in matches],
                }
                return
            if not matches:
                label = entity.id or entity.name or "未命名实体"
                warnings.append(f"entity not found in snapshot: {label}")
                incomplete = True
                continue
            source_row = matches[0]
            source_id = source_row[scope.record_id_column]
            source_record = self._record(scope, source_row, source_selected)
            source_rows.append((source_id, source_record))
            if source_selected:
                source_provenance.append(
                    self._provenance(connection, scope, source_row, source_selected)
                )

        if not source_rows:
            response["status"] = "not_found"
            response["warnings"] = warnings or ["entity not found in snapshot"]
            return

        if len(plan.entities) == 1:
            response["data"]["source"] = source_rows[0][1]
        else:
            response["data"]["sources"] = [record for _, record in source_rows]

        all_records: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        deliverable_items: list[dict[str, Any]] = []

        for source_id, source_record in source_rows:
            source_reference = {
                "scope": scope.name,
                "record_id": str(source_id),
                "fields": dict(source_record.get("fields") or {}),
            }
            for relation in plan.relations:
                registered = _RELATIONS.get(relation.name)
                if registered is None:
                    continue
                _, target_scope_name, target_table = registered
                target_scope = _SCOPES[target_scope_name]
                target_ids = connection.execute(
                    "SELECT to_id FROM relations WHERE from_table = ? AND from_id = ? "
                    "AND relation_type = ? ORDER BY to_id",
                    (scope.table, source_id, relation.name),
                ).fetchall()
                if not target_ids:
                    warnings.append(f"relation not maintained: {relation.name}:{source_id}")
                    incomplete = True
                    continue
                selected = self._related_select(plan.select, target_scope)
                self._validate_fields(target_scope, selected)
                for target_id_row in target_ids[:100]:
                    target = connection.execute(
                        f"SELECT * FROM {target_table} WHERE "
                        f"{target_scope.record_id_column} = ?",
                        (target_id_row[0],),
                    ).fetchone()
                    if target is None:
                        warnings.append(
                            f"relation target missing: {relation.name}:{target_id_row[0]}"
                        )
                        incomplete = True
                        continue
                    record = self._record(target_scope, target, selected)
                    record["relation"] = relation.name
                    if len(source_rows) > 1:
                        record["source"] = source_reference
                    record["attachments"] = self._attachments(
                        connection, target_scope, target, selected
                    )
                    has_attachment_select = any(
                        field in ATTACHMENT_FIELDS for field in plan.select
                    )
                    if (
                        plan.response_mode in {"deliver_files", "answer_and_files"}
                        or has_attachment_select
                    ):
                        for logical_field, entries in record["attachments"].items():
                            for entry in entries:
                                if entry.get("status") != "available":
                                    continue
                                deliverable_items.append(
                                    {
                                        **entry,
                                        "logical_field": logical_field,
                                        "record_id": str(target[target_scope.record_id_column]),
                                        "scope": target_scope_name,
                                        "source_record_id": str(source_id),
                                    }
                                )
                    all_records.append(record)
                    source = self._provenance(connection, target_scope, target, selected)
                    source["relation"] = relation.name
                    provenance.append(source)

        response["data"]["records"] = all_records
        response["data"]["count"] = len(all_records)
        response["provenance"] = source_provenance + provenance
        response["warnings"] = warnings
        if deliverable_items:
            response["data"]["items"] = deliverable_items
            response["data"]["deliverables"] = [
                {
                    "media_id": str(entry["media_id"]),
                    "kind": self._delivery_kind(str(entry["display_name"])),
                    "name": str(entry["display_name"]),
                    "caption": (
                        f"{entry['scope']} / {entry['record_id']} / {entry['logical_field']}"
                        + (
                            f" / 来源 {scope.name} / {entry['source_record_id']}"
                            if len(source_rows) > 1
                            else ""
                        )
                    ),
                }
                for entry in deliverable_items
                if entry.get("media_id")
            ]

        if incomplete:
            response["status"] = "not_found"
            response["data"] = {"count": 0, "records": [], "items": []}
            response["provenance"] = []

    @staticmethod
    def _related_select(selected: list[str], target_scope: _Scope) -> list[str]:
        if not selected:
            return SqliteQueryExecutor._default_select(target_scope)
        fields = [field for field in selected if field in target_scope.fields]
        fields.extend(
            field
            for field in selected
            if field in ATTACHMENT_FIELDS and ATTACHMENT_FIELDS[field].scope == target_scope.name
        )
        if not fields:
            return SqliteQueryExecutor._default_select(target_scope)

        # 如果匹配到的字段全都是 ID 标识字段，说明缺少目标实体的名称等描述属性，自动补充默认展示字段
        id_fields = {target_scope.record_id_column, target_scope.business_id_column, "record_id"}
        if all(f in id_fields for f in fields):
            for default_f in SqliteQueryExecutor._default_select(target_scope):
                if default_f not in fields:
                    fields.append(default_f)

        return fields

    def _resolve_entity(
        self,
        connection: sqlite3.Connection,
        scope: _Scope,
        entity: QueryEntity,
    ) -> list[sqlite3.Row]:
        val = (entity.id or "").strip()
        name_val = (entity.name or "").strip()

        # 1. Try exact match on record_id or business_id
        if val:
            matches = connection.execute(
                f"SELECT * FROM {scope.table} WHERE {scope.record_id_column} = ? "
                f"OR {scope.business_id_column} = ? ORDER BY {scope.record_id_column}",
                (val, val),
            ).fetchall()
            if matches:
                return matches

        # 2. Try match on name_column (using entity.name or fallback to entity.id)
        search_name = name_val or val
        if search_name and scope.name_column:
            matches = connection.execute(
                f"SELECT * FROM {scope.table} WHERE {scope.name_column} = ? "
                f"OR {scope.name_column} LIKE ? ORDER BY {scope.record_id_column}",
                (search_name, f"%{search_name}%"),
            ).fetchall()
            if matches:
                return matches

        return []

    @staticmethod
    def _candidate(scope: _Scope, row: sqlite3.Row) -> dict[str, str]:
        return {
            "scope": scope.name,
            "record_id": str(row[scope.record_id_column]),
            "id": str(row[scope.business_id_column]),
            "name": str(row[scope.name_column]) if scope.name_column else "",
        }

    @staticmethod
    def _record(scope: _Scope, row: sqlite3.Row, selected: list[str]) -> dict[str, Any]:
        fields = {
            field: row[column]
            for field, column in scope.fields.items()
            if field in selected
        }
        return {
            "scope": scope.name,
            "record_id": str(row[scope.record_id_column]),
            "fields": fields,
        }

    @staticmethod
    def _provenance(
        connection: sqlite3.Connection,
        scope: _Scope,
        row: sqlite3.Row,
        selected: list[str],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "table": scope.table,
            "record_id": str(row[scope.record_id_column]),
            "fields": selected,
        }
        source = connection.execute(
            "SELECT source_file, source_row_number, source_key FROM source_rows "
            "WHERE table_name = ? AND source_row_id = ?",
            (scope.table, row[scope.record_id_column]),
        ).fetchone()
        if source is not None:
            result.update(
                {
                    "source_file": str(source["source_file"]),
                    "source_row_number": int(source["source_row_number"]),
                    "source_key": str(source["source_key"]),
                }
            )
        return result

    @staticmethod
    def _attachment_name_filter(plan: QueryPlan) -> str | None:
        for item in plan.filters:
            if item.field == ATTACHMENT_NAME_FILTER and isinstance(item.value, str):
                return item.value
        return None

    @staticmethod
    def _filter_new_packaging_back_labels(
        entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not entries:
            return []
        pass1 = [
            e
            for e in entries
            if any(k in str(e.get("display_name") or "") for k in ["背标", "背签", "背面"])
        ]
        if pass1:
            return pass1
        pass2 = [
            e
            for e in entries
            if not any(
                k in str(e.get("display_name") or "")
                for k in ["白底", "效果图", "透明", "10片装", "1片装"]
            )
            and not (
                str(e.get("display_name") or "").startswith("一次性")
                and "背" not in str(e.get("display_name") or "")
            )
        ]
        return pass2 or entries

    @staticmethod
    def _fetch_attachment_rows(
        connection: sqlite3.Connection,
        table_name: str,
        record_id: Any,
        source_field: str,
        attachment_name: str | None = None,
    ) -> list[dict[str, Any]]:
        columns = "field_name, display_name, media_id, status, valid_until, validity_source"
        statement = (
            f"SELECT {columns} FROM attachments "
            f"WHERE table_name = ? AND source_row_id = ? AND field_name = ?"
        )
        params: list[Any] = [table_name, record_id, source_field]
        if attachment_name is not None:
            statement += " AND display_name = ?"
            params.append(attachment_name)
        statement += " ORDER BY display_name"
        entries = connection.execute(statement, params).fetchall()
        result = [dict(entry) for entry in entries]
        if not result and source_field == "第三方检测报告（附件）":
            statement = (
                f"SELECT {columns} FROM attachments "
                f"WHERE table_name = ? AND source_row_id = ? AND field_name LIKE ?"
            )
            params = [table_name, record_id, "第三方检测报告%"]
            if attachment_name is not None:
                statement += " AND display_name = ?"
                params.append(attachment_name)
            statement += " ORDER BY display_name"
            result = [dict(e) for e in connection.execute(statement, params).fetchall()]
        return result

    @staticmethod
    def _attachments(
        connection: sqlite3.Connection,
        scope: _Scope,
        row: sqlite3.Row,
        selected: list[str],
        attachment_name: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for logical_name in selected:
            attachment = ATTACHMENT_FIELDS.get(logical_name)
            if attachment is None or attachment.scope != scope.name:
                continue
            if scope.name == "sku" and logical_name == "sku_product_back_label":
                new_pkg_entries = SqliteQueryExecutor._fetch_attachment_rows(
                    connection,
                    scope.table,
                    row[scope.record_id_column],
                    "新包装&白底图&背标",
                    attachment_name,
                )
                filtered_new_pkg = SqliteQueryExecutor._filter_new_packaging_back_labels(
                    [e for e in new_pkg_entries if e.get("status") == "available"]
                )
                if filtered_new_pkg:
                    result[logical_name] = filtered_new_pkg
                    continue
            entries = SqliteQueryExecutor._fetch_attachment_rows(
                connection,
                scope.table,
                row[scope.record_id_column],
                attachment.source_field,
                attachment_name,
            )
            result[logical_name] = entries
        return result

    @staticmethod
    def _attachment_count(
        connection: sqlite3.Connection,
        scope: _Scope,
        row: sqlite3.Row,
        selected: list[str],
        attachment_name: str | None = None,
    ) -> int:
        attachments = SqliteQueryExecutor._attachments(
            connection, scope, row, selected, attachment_name
        )
        return sum(
            sum(1 for entry in entries if entry.get("status") == "available")
            for entries in attachments.values()
        )

    @staticmethod
    def _attachment_items(
        connection: sqlite3.Connection,
        scope: _Scope,
        rows: list[sqlite3.Row],
        selected: list[str],
        attachment_name: str | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row in rows:
            attachments = SqliteQueryExecutor._attachments(
                connection, scope, row, selected, attachment_name
            )
            for logical_field, entries in attachments.items():
                for entry in entries:
                    if entry.get("status") != "available":
                        continue
                    items.append(
                        {
                            **entry,
                            "logical_field": logical_field,
                            "record_id": str(row[scope.record_id_column]),
                        }
                    )
        return items

    @staticmethod
    def _delivery_kind(display_name: str) -> str:
        suffix = display_name.rsplit(".", 1)[-1].casefold() if "." in display_name else ""
        return "image" if suffix in {"jpg", "jpeg", "png", "gif", "webp"} else "file"
