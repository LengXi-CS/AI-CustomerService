"""Lark (Feishu) Bitable sync manager and SQLite read-model builder.

Pulls records and attachments directly from Lark OpenAPI, downloads attachments
to local disk, and atomically constructs a Schema v2 SQLite read model.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from fastgpt_assistant.core.media_store import derive_media_id

logger = logging.getLogger(__name__)

# Known Feishu Bitable table IDs
TABLE_SCOPES: dict[str, str] = {
    "tbl0ap0h4IEqdlrM": "factories",
    "tbl6WQbr7TJi37nj": "skus",
    "tblhAcESQf4kE1Zv": "categories",
    "tblqDGLA9SyGPkHx": "trademarks",
    "tblzEUmko4QcFiPE": "batch_reports",
}

_DOMAIN_TABLES = ("factories", "skus", "categories", "trademarks", "batch_reports")
_DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})\s*(?:年|[-/.])\s*(?P<month>\d{1,2})\s*(?:月|[-/.])\s*"
    r"(?P<day>\d{1,2})\s*日?"
)


def _safe_filename(name: str) -> str:
    """Clean filename for safe cross-platform filesystem storage."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned[:180] or "attachment"


def _parse_date(value: str) -> str | None:
    """Parse ISO date from text containing calendar dates."""
    match = _DATE_PATTERN.search(value)
    if not match:
        return None
    year = int(match.group("year"))
    month = int(match.group("month"))
    day = int(match.group("day"))
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


class LarkBitableClient:
    """Minimal, robust Lark OpenAPI HTTP client."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        base_url: str = "https://open.feishu.cn",
        timeout: float = 60.0,
    ) -> None:
        if not app_id or not app_secret:
            raise ValueError("Lark app_id and app_secret are required")
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._client = httpx.Client(timeout=self.timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LarkBitableClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def get_tenant_access_token(self) -> str:
        """Obtain or reuse cached tenant_access_token."""
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token

        url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        resp = self._client.post(
            url,
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Lark auth error: {data.get('msg')}")

        self._token = data["tenant_access_token"]
        self._token_expires_at = now + int(data.get("expire", 7200))
        return self._token

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_tenant_access_token()}"}

    def list_records(
        self,
        app_token: str,
        table_id: str,
        *,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch all records from a table with automatic pagination."""
        records: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            params: dict[str, Any] = {
                "page_size": max(1, min(page_size, 500)),
                "field_name": "true",
            }
            if page_token:
                params["page_token"] = page_token

            url = f"{self.base_url}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
            resp = self._client.get(url, headers=self._auth_headers(), params=params)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != 0:
                raise RuntimeError(f"Lark list_records failed: {data.get('msg')}")

            items = data.get("data", {}).get("items") or []
            records.extend(items)

            if not data.get("data", {}).get("has_more"):
                break
            page_token = data.get("data", {}).get("page_token")
            if not page_token:
                break

        return records

    def download_media(
        self,
        file_token: str,
        destination_path: Path,
        *,
        url: str | None = None,
        skip_if_exists: bool = True,
    ) -> Path:
        """Download an attachment file from Feishu drive medias endpoint or direct url."""
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if skip_if_exists and destination_path.is_file() and destination_path.stat().st_size > 0:
            return destination_path

        download_url = url or f"{self.base_url}/open-apis/drive/v1/medias/{file_token}/download"
        with self._client.stream(
            "GET", download_url, headers=self._auth_headers(), follow_redirects=True
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(f"Download failed for {file_token}: HTTP {response.status_code}")
            temp_path = destination_path.with_suffix(destination_path.suffix + ".tmp")
            with temp_path.open("wb") as file:
                for chunk in response.iter_bytes(chunk_size=1024 * 64):
                    file.write(chunk)
            temp_path.replace(destination_path)

        return destination_path


@dataclass(slots=True)
class SyncTableSummary:
    table_id: str
    scope: str
    records_total: int = 0
    records_imported: int = 0
    attachments_indexed: int = 0
    attachments_downloaded: int = 0
    attachments_skipped: int = 0
    attachments_failed: int = 0


@dataclass(slots=True)
class SyncSummary:
    database_path: str
    snapshot_id: str
    tables: list[SyncTableSummary] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "database_path": self.database_path,
            "snapshot_id": self.snapshot_id,
            "tables": [asdict(t) for t in self.tables],
            "warnings": self.warnings,
        }


class BitableSyncManager:
    """Coordinates fetching Feishu Bitable records and building the SQLite read model."""

    def __init__(
        self,
        client: LarkBitableClient,
        app_token: str,
        database_path: str | Path,
        attachment_dir: str | Path,
    ) -> None:
        self.client = client
        self.app_token = app_token
        self.database_path = Path(database_path).resolve()
        self.attachment_dir = Path(attachment_dir).resolve()

    def sync(self, *, download_attachments: bool = True) -> SyncSummary:
        """Fetch all tables, download attachments, and atomically produce the read-model DB."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.attachment_dir.mkdir(parents=True, exist_ok=True)

        now_str = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        snapshot_id = f"bitable-{now_str}-{uuid.uuid4().hex[:8]}"
        summary = SyncSummary(str(self.database_path), snapshot_id)

        temp_db_path = self.database_path.with_name(
            f".{self.database_path.name}.{uuid.uuid4().hex}.tmp"
        )
        conn = sqlite3.connect(temp_db_path)
        try:
            self._configure_db(conn)
            self._create_schema(conn)

            # Insert snapshot record
            conn.execute(
                "INSERT INTO source_snapshots "
                "(snapshot_id, source_root, created_at, status, table_count, record_count) "
                "VALUES (?, ?, ?, 'building', ?, 0)",
                (
                    snapshot_id,
                    f"lark://bitable/{self.app_token}",
                    datetime.now(UTC).isoformat(),
                    len(TABLE_SCOPES),
                ),
            )

            total_records_imported = 0
            for table_id, scope in TABLE_SCOPES.items():
                table_summary = SyncTableSummary(table_id=table_id, scope=scope)
                logger.info("Fetching records for %s (%s)...", scope, table_id)
                records = self.client.list_records(self.app_token, table_id)
                table_summary.records_total = len(records)

                self._process_table_records(
                    conn=conn,
                    snapshot_id=snapshot_id,
                    table_id=table_id,
                    scope=scope,
                    records=records,
                    table_summary=table_summary,
                    warnings=summary.warnings,
                    download_attachments=download_attachments,
                )
                total_records_imported += table_summary.records_imported
                summary.tables.append(table_summary)

            # Insert relations
            self._insert_relations(conn, snapshot_id, summary.warnings)

            # Update snapshot status to ready
            conn.execute(
                "UPDATE source_snapshots SET status = 'ready', record_count = ? "
                "WHERE snapshot_id = ?",
                (total_records_imported, snapshot_id),
            )
            conn.commit()
            conn.close()

            # Atomically replace target database
            if self.database_path.exists():
                self.database_path.unlink()
            temp_db_path.replace(self.database_path)
        except Exception:
            conn.rollback()
            conn.close()
            temp_db_path.unlink(missing_ok=True)
            raise

        logger.info(
            "Bitable read model build finished: %d records imported -> %s",
            total_records_imported,
            self.database_path,
        )
        return summary

    @staticmethod
    def _configure_db(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA user_version = 2")

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE source_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                source_root TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                table_count INTEGER NOT NULL DEFAULT 0,
                record_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE source_files (
                snapshot_id TEXT NOT NULL,
                table_name TEXT NOT NULL,
                source_file TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                modified_at TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                PRIMARY KEY (snapshot_id, source_file),
                FOREIGN KEY (snapshot_id) REFERENCES source_snapshots(snapshot_id)
            );
            CREATE TABLE source_rows (
                snapshot_id TEXT NOT NULL,
                table_name TEXT NOT NULL,
                source_row_id TEXT NOT NULL,
                source_row_number INTEGER NOT NULL,
                source_file TEXT NOT NULL,
                source_key TEXT NOT NULL,
                data_json TEXT NOT NULL,
                PRIMARY KEY (snapshot_id, table_name, source_row_id),
                FOREIGN KEY (snapshot_id) REFERENCES source_snapshots(snapshot_id)
            );
            CREATE TABLE factories (
                record_id TEXT PRIMARY KEY,
                factory_id TEXT NOT NULL,
                factory_name TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                source_row_id TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE skus (
                sku_code TEXT PRIMARY KEY,
                product_name TEXT NOT NULL,
                trademark_code TEXT,
                factory_id TEXT,
                category_id TEXT,
                product_size TEXT,
                snapshot_id TEXT NOT NULL,
                source_row_id TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE categories (
                record_id TEXT PRIMARY KEY,
                category_id TEXT NOT NULL,
                category_name TEXT NOT NULL,
                factory_id TEXT,
                snapshot_id TEXT NOT NULL,
                source_row_id TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE trademarks (
                trademark_code TEXT PRIMARY KEY,
                trademark_name TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                source_row_id TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE batch_reports (
                sku_code TEXT PRIMARY KEY,
                batch_no TEXT,
                report_name TEXT,
                snapshot_id TEXT NOT NULL,
                source_row_id TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE attachments (
                media_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                table_name TEXT NOT NULL,
                source_row_id TEXT NOT NULL,
                field_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL,
                archive_file TEXT,
                archive_member TEXT,
                valid_until TEXT,
                validity_source TEXT
            );
            CREATE TABLE relations (
                snapshot_id TEXT NOT NULL,
                from_table TEXT NOT NULL,
                from_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                to_table TEXT NOT NULL,
                to_id TEXT NOT NULL,
                source_row_id TEXT NOT NULL,
                PRIMARY KEY (
                    snapshot_id, from_table, from_id, relation_type, to_table, to_id
                )
            );
            CREATE TABLE build_warnings (
                snapshot_id TEXT NOT NULL,
                table_name TEXT,
                source_row_id TEXT,
                code TEXT NOT NULL,
                message TEXT NOT NULL
            );
            CREATE INDEX idx_source_rows_table ON source_rows(snapshot_id, table_name);
            CREATE INDEX idx_attachments_source
                ON attachments(snapshot_id, table_name, source_row_id);
            CREATE INDEX idx_relations_from ON relations(snapshot_id, from_table, from_id);
            CREATE INDEX idx_relations_to ON relations(snapshot_id, to_table, to_id);
            """
        )

    def _process_table_records(
        self,
        conn: sqlite3.Connection,
        snapshot_id: str,
        table_id: str,
        scope: str,
        records: list[dict[str, Any]],
        table_summary: SyncTableSummary,
        warnings: list[str],
        download_attachments: bool,
    ) -> None:
        # Pre-calculate business key frequencies to resolve duplicates
        key_counts: dict[str, int] = {}
        valid_records: list[tuple[int, str, dict[str, Any]]] = []

        for row_num, rec in enumerate(records, start=1):
            fields = rec.get("fields") or {}
            rec_id = str(rec.get("record_id") or rec.get("id") or f"row_{row_num}")
            has_val = any(v is not None and v != "" and v != [] for v in fields.values())
            if not has_val:
                continue
            valid_records.append((row_num, rec_id, fields))
            bkey = self._extract_business_key(scope, fields)
            if bkey:
                key_counts[bkey] = key_counts.get(bkey, 0) + 1

        seen_keys: set[str] = set()
        for row_num, rec_id, fields in valid_records:
            bkey = self._extract_business_key(scope, fields)
            record_id, skip = self._determine_record_id(
                scope, row_num, rec_id, bkey, key_counts, warnings
            )
            if skip:
                continue

            if record_id in seen_keys:
                warnings.append(
                    f"duplicate primary key {record_id} in {scope}; row {row_num} skipped"
                )
                continue
            seen_keys.add(record_id)

            raw_json = json.dumps(fields, ensure_ascii=False, sort_keys=True)
            conn.execute(
                "INSERT INTO source_rows "
                "(snapshot_id, table_name, source_row_id, source_row_number, source_file, "
                "source_key, data_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    scope,
                    record_id,
                    row_num,
                    f"lark://bitable/{self.app_token}/{table_id}/records/{rec_id}",
                    bkey or rec_id,
                    raw_json,
                ),
            )

            self._insert_domain_record(conn, snapshot_id, scope, record_id, bkey, fields, raw_json)
            table_summary.records_imported += 1

            self._process_record_attachments(
                conn=conn,
                snapshot_id=snapshot_id,
                table_id=table_id,
                scope=scope,
                record_id=record_id,
                bitable_rec_id=rec_id,
                fields=fields,
                table_summary=table_summary,
                warnings=warnings,
                download=download_attachments,
            )

    @staticmethod
    def _extract_business_key(scope: str, fields: dict[str, Any]) -> str:
        if scope == "factories":
            return str(fields.get("工厂 ID") or "").strip()
        if scope == "skus":
            return str(fields.get("SKU编码") or fields.get("SKU 编码") or "").strip()
        if scope == "categories":
            return str(fields.get("品类ID") or "").strip()
        if scope == "trademarks":
            return str(fields.get("商标编码") or "").strip()
        if scope == "batch_reports":
            return str(fields.get("SKU 编码") or fields.get("SKU编码") or "").strip()
        return ""

    @staticmethod
    def _determine_record_id(
        scope: str,
        row_num: int,
        rec_id: str,
        bkey: str,
        key_counts: dict[str, int],
        warnings: list[str],
    ) -> tuple[str, bool]:
        if not bkey:
            if scope not in {"factories", "categories"}:
                warnings.append(f"{scope} row {row_num}: empty key skipped")
                return "", True
            record_id = f"无业务键-{rec_id}"
            warnings.append(f"{scope} row {row_num}: empty key uses {record_id}")
            return record_id, False

        count = key_counts.get(bkey, 0)
        if count > 1 and scope not in {"factories", "categories"}:
            warnings.append(f"duplicate key {bkey} in {scope}; row {row_num} skipped")
            return "", True
        if count > 1:
            record_id = f"{bkey}-{rec_id}"
            warnings.append(f"{scope} key {bkey} duplicated; row {row_num} uses {record_id}")
        else:
            record_id = bkey
        return record_id, False

    @staticmethod
    def _insert_domain_record(
        conn: sqlite3.Connection,
        snapshot_id: str,
        scope: str,
        record_id: str,
        bkey: str,
        fields: dict[str, Any],
        raw_json: str,
    ) -> None:
        if scope == "factories":
            conn.execute(
                "INSERT INTO factories VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    bkey,
                    str(fields.get("工厂名称") or "").strip(),
                    snapshot_id,
                    record_id,
                    raw_json,
                ),
            )
        elif scope == "skus":
            conn.execute(
                "INSERT INTO skus VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bkey,
                    str(fields.get("产品名称") or "").strip(),
                    str(fields.get("商标编码") or "").strip() or None,
                    str(fields.get("关联工厂 ID") or "").strip() or None,
                    str(fields.get("关联品类 ID") or "").strip() or None,
                    str(fields.get("产品尺寸") or "").strip() or None,
                    snapshot_id,
                    bkey,
                    raw_json,
                ),
            )
        elif scope == "categories":
            conn.execute(
                "INSERT INTO categories VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    bkey,
                    str(fields.get("品类名称") or "").strip(),
                    str(fields.get("工厂ID") or "").strip() or None,
                    snapshot_id,
                    record_id,
                    raw_json,
                ),
            )
        elif scope == "trademarks":
            conn.execute(
                "INSERT INTO trademarks VALUES (?, ?, ?, ?, ?)",
                (
                    bkey,
                    str(fields.get("商标名称") or "").strip(),
                    snapshot_id,
                    bkey,
                    raw_json,
                ),
            )
        elif scope == "batch_reports":
            report_name = None
            rep_field = fields.get("批次检测报告")
            if isinstance(rep_field, list) and rep_field:
                report_name = rep_field[0].get("name")
            elif isinstance(rep_field, str):
                report_name = rep_field.strip()
            conn.execute(
                "INSERT INTO batch_reports VALUES (?, ?, ?, ?, ?, ?)",
                (
                    bkey,
                    str(fields.get("批次号") or "").strip() or None,
                    report_name,
                    snapshot_id,
                    bkey,
                    raw_json,
                ),
            )

    def _process_record_attachments(
        self,
        conn: sqlite3.Connection,
        snapshot_id: str,
        table_id: str,
        scope: str,
        record_id: str,
        bitable_rec_id: str,
        fields: dict[str, Any],
        table_summary: SyncTableSummary,
        warnings: list[str],
        download: bool,
    ) -> None:
        table_name = scope if scope in _DOMAIN_TABLES else table_id
        valid_until, validity_source = self._validity_for_record(fields)

        attachment_items: list[tuple[str, str, str, int, str | None]] = []
        for fname, val in fields.items():
            if (
                isinstance(val, list)
                and val
                and isinstance(val[0], dict)
                and "file_token" in val[0]
            ):
                for idx, entry in enumerate(val, start=1):
                    file_token = str(entry.get("file_token") or "")
                    name = str(entry.get("name") or "")
                    direct_url = str(entry.get("url") or entry.get("tmp_url") or "")
                    if file_token and name:
                        attachment_items.append((fname, name, file_token, idx, direct_url or None))

        seen_media_ids: set[str] = set()
        for field_name, display_name, file_token, idx, direct_url in attachment_items:
            media_id = derive_media_id(
                record_id=bitable_rec_id,
                field_name=field_name,
                display_name=display_name,
                content_hash=f"tok_{file_token}",
                attachment_index=idx,
            )
            if media_id in seen_media_ids:
                continue
            seen_media_ids.add(media_id)

            safe_name = _safe_filename(display_name)
            target_filename = f"{file_token}_{safe_name}"
            target_file_path = self.attachment_dir / target_filename
            relative_archive_file = f"resources/attachments/{target_filename}"

            status = "missing"
            if download:
                try:
                    if not target_file_path.is_file() or target_file_path.stat().st_size == 0:
                        self.client.download_media(file_token, target_file_path, url=direct_url)
                        table_summary.attachments_downloaded += 1
                        if table_summary.attachments_downloaded % 20 == 0:
                            logger.info(
                                "[%s] Downloaded %d media files...",
                                scope,
                                table_summary.attachments_downloaded,
                            )
                    else:
                        table_summary.attachments_skipped += 1
                    status = "available"
                except Exception as exc:
                    err_msg = f"Failed to download attachment {display_name} ({file_token}): {exc}"
                    warnings.append(err_msg)
                    logger.warning(err_msg)
                    conn.execute(
                        "INSERT INTO build_warnings VALUES (?, ?, ?, ?, ?)",
                        (snapshot_id, table_name, record_id, "attachment_download_error", err_msg),
                    )
                    table_summary.attachments_failed += 1
            else:
                if target_file_path.is_file() and target_file_path.stat().st_size > 0:
                    status = "available"

            conn.execute(
                "INSERT OR REPLACE INTO attachments "
                "(media_id, snapshot_id, table_name, source_row_id, field_name, "
                "display_name, status, archive_file, archive_member, valid_until, "
                "validity_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    media_id,
                    snapshot_id,
                    table_name,
                    record_id,
                    field_name,
                    display_name,
                    status,
                    relative_archive_file,
                    None,
                    valid_until,
                    validity_source,
                ),
            )

            if status == "available":
                table_summary.attachments_indexed += 1

    @staticmethod
    def _validity_for_record(fields: dict[str, Any]) -> tuple[str | None, str | None]:
        for name, value in fields.items():
            if not value or not isinstance(value, str):
                continue
            if any(marker in name for marker in ("有效期", "到期", "生效")):
                parsed = _parse_date(value)
                if parsed:
                    return parsed, "字段值"
        return None, None

    @staticmethod
    def _insert_relations(
        conn: sqlite3.Connection,
        snapshot_id: str,
        warnings: list[str],
    ) -> None:
        targets: dict[str, dict[str, list[str]]] = {
            table: _target_index(conn, table, key_col)
            for table, key_col in (
                ("factories", "factory_id"),
                ("categories", "category_id"),
                ("trademarks", "trademark_code"),
                ("skus", "sku_code"),
            )
        }

        def add(
            from_table: str,
            from_id: str,
            relation_type: str,
            to_table: str,
            to_key: str | None,
        ) -> None:
            if not to_key:
                return
            candidates = targets[to_table].get(to_key, [])
            if not candidates:
                msg = f"unresolved relation {from_table}:{from_id} -> {to_table}:{to_key}"
                warnings.append(msg)
                conn.execute(
                    "INSERT INTO build_warnings VALUES (?, ?, ?, ?, ?)",
                    (snapshot_id, from_table, from_id, "unresolved_relation", msg),
                )
                return
            if len(candidates) > 1:
                msg = f"ambiguous relation {from_table}:{from_id} -> {to_table}:{to_key}"
                warnings.append(msg)
                conn.execute(
                    "INSERT INTO build_warnings VALUES (?, ?, ?, ?, ?)",
                    (snapshot_id, from_table, from_id, "ambiguous_relation", msg),
                )
                return
            conn.execute(
                "INSERT OR REPLACE INTO relations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    from_table,
                    from_id,
                    relation_type,
                    to_table,
                    candidates[0],
                    from_id,
                ),
            )

        query = "SELECT sku_code, factory_id, category_id, trademark_code FROM skus"
        for row in conn.execute(query):
            add("skus", row[0], "sku_to_factory", "factories", row[1])
            add("skus", row[0], "sku_to_category", "categories", row[2])
            add("skus", row[0], "sku_to_trademark", "trademarks", row[3])

        for row in conn.execute("SELECT record_id, factory_id FROM categories"):
            add("categories", row[0], "category_to_factory", "factories", row[1])

        for row in conn.execute("SELECT sku_code FROM batch_reports"):
            add("batch_reports", row[0], "batch_to_sku", "skus", row[0])

        # Reverse edges
        reverse_names = {
            "sku_to_factory": "factory_to_sku",
            "sku_to_category": "category_to_sku",
            "sku_to_trademark": "trademark_to_sku",
            "category_to_factory": "factory_to_category",
            "batch_to_sku": "sku_to_batch",
        }
        forward_rows = conn.execute(
            "SELECT from_table, from_id, relation_type, to_table, to_id, source_row_id "
            "FROM relations WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchall()
        for from_table, from_id, rel_type, to_table, to_id, s_row_id in forward_rows:
            rev_name = reverse_names.get(rel_type)
            if rev_name is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO relations VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (snapshot_id, to_table, to_id, rev_name, from_table, from_id, s_row_id),
                )


def _target_index(
    conn: sqlite3.Connection,
    table: str,
    key_column: str,
) -> dict[str, list[str]]:
    record_column = "record_id" if table in {"factories", "categories"} else key_column
    index: dict[str, list[str]] = {}
    for record_id, business_key in conn.execute(
        f"SELECT {record_column}, {key_column} FROM {table}"
    ):
        index.setdefault(str(business_key), []).append(str(record_id))
    return index
