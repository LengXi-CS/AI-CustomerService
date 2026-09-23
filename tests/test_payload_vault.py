"""Unit tests for AES-GCM payload vault."""

import tempfile
from pathlib import Path

import pytest

from fastgpt_assistant.orchestration.payload_vault import (
    PayloadVault,
    PayloadVaultCorruption,
    generate_payload_vault_key,
)


def test_payload_vault_store_and_retrieve() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        key = generate_payload_vault_key()
        db_path = Path(tmp_dir) / "vault.db"
        vault = PayloadVault(key=key, db_path=db_path)
        raw_text = "客户敏感信息与临时文本数据"

        record = vault.store_text(event_id="evt_001", text=raw_text)
        assert record.event_id == "evt_001"
        assert record.content_length == len(raw_text)

        decrypted = vault.retrieve_text("evt_001")
        assert decrypted == raw_text


def test_payload_vault_tamper_detection() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        key = generate_payload_vault_key()
        db_path = Path(tmp_dir) / "vault.db"
        vault = PayloadVault(key=key, db_path=db_path)
        raw_text = "重要订单数据"

        vault.store_text(event_id="evt_002", text=raw_text)

        # Tamper with the ciphertext directly in SQLite using safe context manager
        with vault._connection() as conn:
            conn.execute(
                "UPDATE encrypted_payloads SET ciphertext = X'00112233' WHERE event_id = 'evt_002'"
            )

        with pytest.raises(PayloadVaultCorruption):
            vault.retrieve_text("evt_002")
