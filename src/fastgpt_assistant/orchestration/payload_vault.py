"""Short-lived encrypted storage for sensitive WeChat message text and context.

Uses AES-GCM (via cryptography) to store encrypted conversation text and
sliding-window context with authenticated associated data.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_VERSION = b"FASTGPT-PAYLOAD-VAULT-v1"
_NONCE_BYTES = 12
_TAG_BYTES = 16


class PayloadVaultKeyError(ValueError):
    """Raised when the vault key is absent or has an unsafe format."""


class PayloadVaultCorruption(ValueError):
    """Raised when a vault record cannot be authenticated or decoded."""


@dataclass(frozen=True, slots=True)
class PayloadVaultRecord:
    event_id: str
    content_sha256: str
    content_length: int
    created_at: str
    expires_at: str


def decode_payload_vault_key(value: str) -> bytes:
    """Decode the 32-byte URL-safe base64 key."""
    if not isinstance(value, str):
        raise PayloadVaultKeyError("payload vault key must be a string")
    encoded = value.strip()
    if not encoded or len(encoded) > 64 or not _KEY_RE.fullmatch(encoded):
        raise PayloadVaultKeyError("payload vault key must be URL-safe base64")
    if len(encoded) % 4:
        encoded += "=" * (4 - len(encoded) % 4)
    try:
        decoded = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise PayloadVaultKeyError("payload vault key must be URL-safe base64") from exc
    if len(decoded) != 32:
        raise PayloadVaultKeyError("payload vault key must decode to 32 bytes")
    return decoded


def generate_payload_vault_key() -> str:
    """Generate a deployment key suitable for EA_WECHAT_PAYLOAD_VAULT_KEY."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


class PayloadVault:
    """Encrypted storage manager for sensitive text and conversation context."""

    def __init__(self, key: str | bytes, db_path: str | Path) -> None:
        self.raw_key = decode_payload_vault_key(key) if isinstance(key, str) else key
        if len(self.raw_key) != 32:
            raise PayloadVaultKeyError("Raw key must be exactly 32 bytes")
        self.aesgcm = AESGCM(self.raw_key)
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextlib.contextmanager
    def _connection(self):
        conn = self._get_connection()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS encrypted_payloads (
                    event_id TEXT PRIMARY KEY,
                    ciphertext BLOB NOT NULL,
                    nonce BLOB NOT NULL,
                    auth_tag BLOB NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    content_length INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_payload_expiry
                    ON encrypted_payloads(expires_at);

                CREATE TABLE IF NOT EXISTS encrypted_contexts (
                    session_id TEXT PRIMARY KEY,
                    ciphertext BLOB NOT NULL,
                    nonce BLOB NOT NULL,
                    auth_tag BLOB NOT NULL,
                    context_sha256 TEXT NOT NULL,
                    context_length INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_context_expiry
                    ON encrypted_contexts(expires_at);
                CREATE TABLE IF NOT EXISTS encrypted_group_messages (
                    session_id TEXT NOT NULL, message_id TEXT NOT NULL, received_at TEXT NOT NULL,
                    nonce BLOB NOT NULL, ciphertext BLOB NOT NULL,
                    PRIMARY KEY(session_id,message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_group_context_time
                    ON encrypted_group_messages(session_id,received_at);
                """
            )

    def store_text(
        self,
        event_id: str,
        text: str,
        *,
        ttl_seconds: int = 86400,
    ) -> PayloadVaultRecord:
        """Encrypt and store an event's plaintext."""
        encoded_text = text.encode("utf-8")
        sha256_hash = hashlib.sha256(encoded_text).hexdigest()
        now = datetime.now(UTC)
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
        created_at = now.isoformat()

        nonce = secrets.token_bytes(_NONCE_BYTES)
        associated_data = f"{event_id}:{sha256_hash}".encode()
        encrypted = self.aesgcm.encrypt(nonce, encoded_text, associated_data)
        ciphertext = encrypted[:-_TAG_BYTES]
        auth_tag = encrypted[-_TAG_BYTES:]

        with self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO encrypted_payloads "
                "(event_id, ciphertext, nonce, auth_tag, content_sha256, "
                "content_length, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    ciphertext,
                    nonce,
                    auth_tag,
                    sha256_hash,
                    len(text),
                    created_at,
                    expires_at,
                ),
            )

        return PayloadVaultRecord(
            event_id=event_id,
            content_sha256=sha256_hash,
            content_length=len(text),
            created_at=created_at,
            expires_at=expires_at,
        )

    def retrieve_text(self, event_id: str) -> str | None:
        """Decrypt and return an event's plaintext if not expired."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT ciphertext, nonce, auth_tag, content_sha256, expires_at "
                "FROM encrypted_payloads WHERE event_id = ?",
                (event_id,),
            ).fetchone()

        if not row:
            return None

        # Check expiry
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
            return None

        associated_data = f"{event_id}:{row['content_sha256']}".encode()
        full_ciphertext = bytes(row["ciphertext"]) + bytes(row["auth_tag"])
        try:
            decrypted = self.aesgcm.decrypt(bytes(row["nonce"]), full_ciphertext, associated_data)
            return decrypted.decode("utf-8")
        except InvalidTag as exc:
            msg = f"Payload authentication failed for event {event_id}"
            raise PayloadVaultCorruption(msg) from exc

    def store_context(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Encrypt and store rolling multi-turn context for a session.

        Context lifetime is bounded by the caller's rolling message window, not
        by wall-clock expiry.  The legacy expiry column remains for schema
        compatibility with existing databases but is unused for contexts.
        """
        raw_json = json.dumps(messages, ensure_ascii=False).encode("utf-8")
        sha256_hash = hashlib.sha256(raw_json).hexdigest()
        now = datetime.now(UTC)
        updated_at = now.isoformat()

        nonce = secrets.token_bytes(_NONCE_BYTES)
        associated_data = f"{session_id}:{sha256_hash}".encode()
        encrypted = self.aesgcm.encrypt(nonce, raw_json, associated_data)
        ciphertext = encrypted[:-_TAG_BYTES]
        auth_tag = encrypted[-_TAG_BYTES:]

        with self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO encrypted_contexts "
                "(session_id, ciphertext, nonce, auth_tag, context_sha256, "
                "context_length, updated_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    ciphertext,
                    nonce,
                    auth_tag,
                    sha256_hash,
                    len(messages),
                    updated_at,
                    "",
                ),
            )

    def retrieve_context(self, session_id: str) -> list[dict[str, Any]]:
        """Decrypt and return rolling context for a session."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT ciphertext, nonce, auth_tag, context_sha256, expires_at "
                "FROM encrypted_contexts WHERE session_id = ?",
                (session_id,),
            ).fetchone()

        if not row:
            return []

        associated_data = f"{session_id}:{row['context_sha256']}".encode()
        full_ciphertext = bytes(row["ciphertext"]) + bytes(row["auth_tag"])
        try:
            decrypted = self.aesgcm.decrypt(bytes(row["nonce"]), full_ciphertext, associated_data)
            return json.loads(decrypted.decode("utf-8"))
        except (InvalidTag, json.JSONDecodeError) as exc:
            msg = f"Context auth/decode failed for session {session_id}"
            raise PayloadVaultCorruption(msg) from exc

    def store_group_message(self, session_id: str, entry: dict[str, str]) -> None:
        """Append encrypted context without rewriting or truncating a whole day."""
        raw = json.dumps(entry, ensure_ascii=False).encode()
        nonce = secrets.token_bytes(_NONCE_BYTES)
        message_id = entry["message_id"]
        encrypted = self.aesgcm.encrypt(nonce, raw, f"{session_id}:{message_id}".encode())
        with self._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO encrypted_group_messages VALUES(?,?,?,?,?)",
                (session_id, message_id, entry["received_at"], nonce, encrypted),
            )

    def retrieve_group_messages(
        self, session_id: str, *, start: str = "", end: str = "", message_id: str | None = None
    ) -> list[dict[str, str]]:
        """Read a bounded interval or one explicit ID; empty IDs never mean all."""
        if message_id is not None:
            if not isinstance(message_id, str) or not message_id.strip():
                return []
            message_id = message_id.strip()
        elif not start or not end:
            raise ValueError("Group context reads require a time interval or a message ID")
        with self._connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='encrypted_group_messages'"
            ).fetchone()
            if not exists:
                return []
            rows = conn.execute(
                "SELECT * FROM encrypted_group_messages WHERE session_id=? "
                "AND (?='' OR received_at>=?) AND (?='' OR received_at<=?) "
                "AND (? IS NULL OR message_id=?) ORDER BY received_at,message_id",
                (session_id, start, start, end, end, message_id, message_id),
            ).fetchall()
        result = []
        for row in rows:
            try:
                raw = self.aesgcm.decrypt(
                    bytes(row["nonce"]), bytes(row["ciphertext"]),
                    f"{session_id}:{row['message_id']}".encode(),
                )
                result.append(json.loads(raw))
            except (InvalidTag, json.JSONDecodeError) as exc:
                raise PayloadVaultCorruption("Group context authentication failed") from exc
        return result

    def cleanup_expired(self) -> tuple[int, int]:
        """Purge expired payloads and context records."""
        now_iso = datetime.now(UTC).isoformat()
        with self._connection() as conn:
            c1 = conn.execute(
                "DELETE FROM encrypted_payloads WHERE expires_at < ?",
                (now_iso,),
            ).rowcount
        return c1, 0
