"""History and recovery operate on disposable configs, never real credentials."""

import json
import sqlite3
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastgpt_assistant.core.admin_auth import configure_admin
from fastgpt_assistant.core.config import Settings
from fastgpt_assistant.core.config_journal import ConfigJournal, HistoryUnavailable
from fastgpt_assistant.core.config_recovery import (
    MANAGED_ENV,
    BackupSelection,
    BackupUnavailable,
    ConfigRecovery,
    RestoreBackupRequest,
)
from fastgpt_assistant.core.connection_config import (
    ConnectionConfigStore,
    SaveConnectionsRequest,
    SecretEdit,
)
from fastgpt_assistant.core.dotenv_store import EnvironmentDocument, InvalidEnvFile, setting_aliases
from fastgpt_assistant.core.group_config_store import (
    ConfigConflict,
    GroupConfigStore,
    SaveGroupsRequest,
    SaveSootheRequest,
    revision_of,
)
from fastgpt_assistant.core.reply_policy_store import ReplyPolicyStore, SaveRuntimeReplyRequest
from fastgpt_assistant.server.admin import install_admin

GROUPS = """# retain operator comment
[global]
enabled = true
soothe_mode = "fixed"
soothe_fixed_reply = "原有安抚文案"
unmanaged = "private-group-extra"

[[groups]]
group_id = "100@chatroom"
group_name = "演示客户群"
mode = "active"
future_rule = "retained"

[[groups]]
group_id = "200@chatroom"
mode = "shadow"
"""


@pytest.fixture
def system(tmp_path, monkeypatch):
    for name in MANAGED_ENV:
        for alias in setting_aliases(name):
            monkeypatch.delenv(alias, raising=False)
    journal = ConfigJournal(tmp_path / "admin" / "history.db")
    groups = GroupConfigStore(tmp_path / "groups.toml", tmp_path / "group-backups", journal=journal)
    groups.path.write_text(GROUPS, encoding="utf-8")
    document = EnvironmentDocument(tmp_path / ".env", tmp_path / "env-backups", journal=journal)
    document.path.write_bytes(
        b'\xef\xbb\xbf# retained note\r\nLLM_MODEL="old-model"\r\n'
        b'LLM_API_KEY="private-old-key"\r\n'
        b'PAYLOAD_VAULT_KEY="private-original-vault"\r\n'
        b'FASTGPT_ROOT_KEY="private-root-key"\r\n'
    )
    settings = Settings(_env_file=None)
    reply = ReplyPolicyStore(
        document.path, document.backup_dir, document=document, runtime_settings=settings, environ={}
    )
    connections = ConnectionConfigStore(document, runtime_settings=settings, environ={})
    return journal, groups, reply, connections, ConfigRecovery(groups, reply)


def connection_change(connections):
    snapshot = connections.snapshot()
    request = SaveConnectionsRequest(
        revision=snapshot["revision"],
        values=snapshot["values"],
        secrets={"llm_api_key": SecretEdit(action="set", value="private-new-key")},
    )
    request.values.llm_model = "new-model"
    return connections.save(request)


def restore_request(preview):
    return RestoreBackupRequest(
        **{
            key: preview[key]
            for key in ("backup_id", "current_revision", "backup_revision", "plan_revision")
        }
    )


def test_group_wait_history_and_legacy_backup_restore(system):
    journal, groups, _, _, recovery = system
    snapshot = groups.snapshot()
    request = SaveGroupsRequest(
        revision=snapshot["revision"], global_enabled=snapshot["global_enabled"],
        groups=snapshot["groups"],
    )
    request.groups[0].conversation_quiet_window_seconds = 0
    saved = groups.save(request)
    assert "groups.conversation_quiet_window_seconds" in groups.changed_fields(
        (groups.backup_dir / saved["backup_id"]).read_bytes(), groups.path.read_bytes()
    )
    assert journal.list()["items"][0]["status"] == "applied"
    preview = recovery.preview("groups", BackupSelection(backup_id=saved["backup_id"]))
    change = next(item for item in preview["changes"]
                  if item["field"] == "groups.conversation_quiet_window_seconds")
    assert change["before"] == "0" and change["after"] == "跟随默认"
    recovery.restore("groups", restore_request(preview))
    assert groups.snapshot()["groups"][0]["conversation_quiet_window_seconds"] is None


def group_change(groups):
    snapshot = groups.snapshot()
    request = SaveGroupsRequest(
        global_enabled=False, groups=snapshot["groups"][:1], revision=snapshot["revision"]
    )
    request.groups[0].mode = "disabled"
    return groups.save(request)


def test_empty_history_and_backup_reads_do_not_create_files(system):
    journal, _, _, _, recovery = system
    assert journal.list() == {"items": [], "next_cursor": None}
    assert recovery.backups("environment")["items"] == []
    assert not journal.path.exists()


def test_saves_from_all_pages_record_only_field_names(system):
    journal, groups, reply, connections, _ = system
    group_change(groups)
    soothe = groups.soothe_snapshot()
    groups.save_soothe(
        SaveSootheRequest(
            revision=soothe["revision"], soothe_mode="fixed", soothe_fixed_reply="修改后的文案"
        )
    )
    snapshot = reply.snapshot()
    request = SaveRuntimeReplyRequest(**snapshot["values"], revision=snapshot["revision"])
    request.conversation_quiet_window_seconds = 17
    reply.save(request)
    connection_change(connections)
    records = journal.list()["items"]
    assert [record["scope"] for record in records] == ["connections", "reply", "soothe", "groups"]
    assert all(record["status"] == "applied" for record in records)
    assert {field["key"] for field in records[0]["fields"]} == {"llm_api_key", "llm_model"}
    serialized = json.dumps(records)
    assert "private" not in serialized and "old-model" not in serialized
    assert "100@chatroom" not in serialized
    assert "修改后的文案" not in serialized


def test_noop_save_does_not_create_history_or_backup(system):
    journal, _, reply, _, recovery = system
    snapshot = reply.snapshot()
    reply.save(SaveRuntimeReplyRequest(**snapshot["values"], revision=snapshot["revision"]))
    assert not journal.path.exists()
    assert recovery.backups("environment")["items"] == []


def test_environment_preview_is_read_only_and_hides_every_secret(system):
    journal, _, reply, connections, recovery = system
    connection_change(connections)
    backup = recovery.backups("environment")["items"][0]
    before = reply.path.read_bytes()
    history = journal.list()
    files = list(reply.backup_dir.iterdir())
    preview = recovery.preview("environment", BackupSelection(backup_id=backup["backup_id"]))
    assert preview["can_restore"] is True
    assert {change["field"] for change in preview["changes"]} == {"llm_model", "llm_api_key"}
    assert "private" not in json.dumps(preview)
    assert reply.path.read_bytes() == before
    assert journal.list() == history and list(reply.backup_dir.iterdir()) == files


def test_environment_restore_preserves_unmanaged_values_and_can_be_undone(system):
    journal, _, reply, connections, recovery = system
    result = connection_change(connections)
    source_id = result["backup_id"]
    reply.path.write_bytes(
        reply.path.read_bytes().replace(b"private-original-vault", b"private-new-vault")
        + b'UNKNOWN="leave this unchanged"\r\n'
    )
    before_restore = reply.path.read_bytes()
    preview = recovery.preview("environment", BackupSelection(backup_id=source_id))
    restored = recovery.restore("environment", restore_request(preview))
    assert connections.snapshot()["values"]["llm_model"] == "old-model"
    assert b"private-old-key" in reply.path.read_bytes()
    assert b"private-new-vault" in reply.path.read_bytes()
    assert b'UNKNOWN="leave this unchanged"\r\n' in reply.path.read_bytes()
    assert reply.path.read_bytes().startswith(b"\xef\xbb\xbf# retained note\r\n")
    assert (reply.backup_dir / restored["backup_id"]).read_bytes() == before_restore
    event = journal.list()["items"][0]
    assert event["operation"] == "restore" and event["source_backup_id"] == source_id
    reverse = recovery.preview("environment", BackupSelection(backup_id=restored["backup_id"]))
    recovery.restore("environment", restore_request(reverse))
    assert connections.snapshot()["values"]["llm_model"] == "new-model"
    assert b"private-new-key" in reply.path.read_bytes()


def test_initial_empty_file_has_a_default_snapshot(system):
    _, _, reply, connections, recovery = system
    reply.path.unlink()
    result = connection_change(connections)
    assert (reply.backup_dir / result["backup_id"]).read_bytes() == b""
    preview = recovery.preview("environment", BackupSelection(backup_id=result["backup_id"]))
    recovery.restore("environment", restore_request(preview))
    assert (
        connections.snapshot()["values"]["llm_model"] == Settings.model_fields["llm_model"].default
    )
    assert connections.snapshot()["secrets"]["llm_api_key"]["configured"] is False


def test_group_restore_preserves_current_comments_extras_and_restores_soothe(system):
    journal, groups, _, _, recovery = system
    result = group_change(groups)
    soothe = groups.soothe_snapshot()
    groups.save_soothe(
        SaveSootheRequest(
            revision=soothe["revision"], soothe_mode="model", soothe_fixed_reply="新文案"
        )
    )
    groups.path.write_text(
        groups.path.read_text(encoding="utf-8").replace(
            "private-group-extra", "private-current-extra"
        ),
        encoding="utf-8",
    )
    preview = recovery.preview("groups", BackupSelection(backup_id=result["backup_id"]))
    assert "private" not in json.dumps(preview)
    assert preview["can_restore"] is True
    before = groups.path.read_bytes()
    restored = recovery.restore("groups", restore_request(preview))
    snapshot = groups.snapshot()
    assert snapshot["global_enabled"] is True and len(snapshot["groups"]) == 2
    assert snapshot["groups"][0]["mode"] == "active"
    assert groups.soothe_snapshot()["values"]["soothe_mode"] == "fixed"
    content = groups.path.read_text(encoding="utf-8")
    assert "# retain operator comment" in content and "private-current-extra" in content
    assert 'future_rule = "retained"' in content
    assert (groups.backup_dir / restored["backup_id"]).read_bytes() == before
    assert journal.list()["items"][0]["operation"] == "restore"


def test_invalid_numeric_current_setting_can_be_repaired_from_valid_backup(system):
    _, _, reply, connections, recovery = system
    result = connection_change(connections)
    reply.path.write_bytes(reply.path.read_bytes() + b"CONVERSATION_QUIET_WINDOW_SECONDS=-1\r\n")
    preview = recovery.preview("environment", BackupSelection(backup_id=result["backup_id"]))
    change = next(
        item for item in preview["changes"] if item["field"] == "conversation_quiet_window_seconds"
    )
    assert change["before"] == "格式无效（原值已隐藏）"
    recovery.restore("environment", restore_request(preview))
    assert reply.snapshot()["values"]["conversation_quiet_window_seconds"] == 120


def test_service_environment_override_blocks_restore_and_changed_conditions_reject_preview(system):
    _, _, reply, connections, recovery = system
    result = connection_change(connections)
    preview = recovery.preview("environment", BackupSelection(backup_id=result["backup_id"]))
    reply.environ["LLM_MODEL"] = "enforced-model"
    before = reply.path.read_bytes()
    with pytest.raises(ConfigConflict):
        recovery.restore("environment", restore_request(preview))
    blocked = recovery.preview("environment", BackupSelection(backup_id=result["backup_id"]))
    assert blocked["can_restore"] is False
    assert any(change["blocked"] for change in blocked["changes"])
    assert reply.path.read_bytes() == before


def test_current_file_change_after_preview_is_rejected_without_new_record(system):
    journal, _, reply, connections, recovery = system
    result = connection_change(connections)
    preview = recovery.preview("environment", BackupSelection(backup_id=result["backup_id"]))
    reply.path.write_bytes(reply.path.read_bytes() + b"UNRELATED=changed\r\n")
    before = reply.path.read_bytes()
    history = journal.list()
    with pytest.raises(ConfigConflict):
        recovery.restore("environment", restore_request(preview))
    assert reply.path.read_bytes() == before and journal.list() == history


def test_tampered_backup_is_rejected(system):
    _, _, reply, connections, recovery = system
    result = connection_change(connections)
    preview = recovery.preview("environment", BackupSelection(backup_id=result["backup_id"]))
    (reply.backup_dir / result["backup_id"]).write_bytes(b"LLM_MODEL=tampered\n")
    before = reply.path.read_bytes()
    with pytest.raises(BackupUnavailable):
        recovery.restore("environment", restore_request(preview))
    assert reply.path.read_bytes() == before


def test_malformed_backup_and_current_syntax_never_overwrite_config(system):
    _, _, reply, connections, recovery = system
    result = connection_change(connections)
    reply.path.write_text('UNKNOWN="unterminated', encoding="utf-8")
    with pytest.raises(InvalidEnvFile):
        recovery.preview("environment", BackupSelection(backup_id=result["backup_id"]))
    invalid = b'LLM_BASE_URL="https://private-key@example.com"\n'
    name = f"20261012T010000000000Z-{revision_of(invalid)[:12]}.env"
    (reply.backup_dir / name).write_bytes(invalid)
    reply.path.write_bytes(b"LLM_MODEL=current\n")
    with pytest.raises(InvalidEnvFile):
        recovery.preview("environment", BackupSelection(backup_id=name))


def test_backup_path_traversal_wrong_scope_and_nonregular_files_are_rejected(system, tmp_path):
    _, groups, _, connections, recovery = system
    result = connection_change(connections)
    with pytest.raises(BackupUnavailable):
        recovery._read_backup("environment", "../groups.toml")
    with pytest.raises(BackupUnavailable):
        recovery._read_backup("groups", result["backup_id"])
    groups.backup_dir.mkdir()
    name = f"20261012T010000000000Z-{revision_of(GROUPS.encode())[:12]}.toml"
    link = groups.backup_dir / name
    try:
        link.symlink_to(groups.path)
    except OSError:
        link.mkdir()
    assert recovery.backups("groups")["items"] == []
    with pytest.raises(BackupUnavailable):
        recovery._read_backup("groups", name)


def test_history_and_backup_pagination_are_stable(system):
    journal, groups, _, _, recovery = system
    group_change(groups)
    for i in range(3):
        snapshot = groups.snapshot()
        snapshot["groups"][0]["group_name"] = f"演示 {i}"
        groups.save(
            SaveGroupsRequest(
                revision=snapshot["revision"],
                global_enabled=snapshot["global_enabled"],
                groups=snapshot["groups"],
            )
        )
    first = journal.list(limit=2)
    second = journal.list(limit=2, before=first["next_cursor"])
    assert len(first["items"]) == len(second["items"]) == 2
    assert not {row["id"] for row in first["items"]} & {row["id"] for row in second["items"]}
    one = recovery.backups("groups", limit=2)
    two = recovery.backups("groups", limit=2, before=one["next_cursor"])
    assert len(one["items"]) == len(two["items"]) == 2
    assert two["next_cursor"] is None


def test_history_write_failure_prevents_config_changes(system, monkeypatch):
    journal, _, reply, connections, _ = system

    def denied(*args, **kwargs):
        raise HistoryUnavailable("无法写入修改记录")

    monkeypatch.setattr(journal, "_update", denied)
    before = reply.path.read_bytes()
    with pytest.raises(HistoryUnavailable):
        connection_change(connections)
    assert reply.path.read_bytes() == before and not reply.backup_dir.exists()


def test_backup_failure_does_not_overwrite_source_and_is_recorded(system, monkeypatch):
    journal, groups, _, _, _ = system

    def denied(*args, **kwargs):
        raise PermissionError("private-directory")

    monkeypatch.setattr("fastgpt_assistant.core.group_config_store.atomic_write", denied)
    before = groups.path.read_bytes()
    with pytest.raises(PermissionError):
        group_change(groups)
    assert groups.path.read_bytes() == before
    assert journal.list()["items"][0]["status"] == "failed"
    assert "private" not in json.dumps(journal.list())


def test_completion_record_failure_preserves_applied_configuration_and_intent(system, monkeypatch):
    journal, _, reply, connections, _ = system
    update = journal._update

    def fail_completion(sql, args, **kwargs):
        if sql.startswith("UPDATE"):
            raise HistoryUnavailable("private-diagnostic")
        return update(sql, args, **kwargs)

    monkeypatch.setattr(journal, "_update", fail_completion)
    result = connection_change(connections)
    assert result["history_pending"] is True
    assert b"new-model" in reply.path.read_bytes()
    assert journal.list()["items"][0]["status"] == "prepared"


def test_tampered_history_does_not_echo_values(system):
    journal, _, _, connections, _ = system
    connection_change(connections)
    with closing(sqlite3.connect(journal.path)) as connection:
        connection.execute("UPDATE config_changes SET fields=?", ('["private-key-value"]',))
        connection.commit()
    with pytest.raises(HistoryUnavailable) as error:
        journal.list()
    assert "private" not in str(error.value)


def test_restore_updates_all_legacy_aliases_and_duplicate_assignments(system):
    _, _, reply, connections, recovery = system
    result = connection_change(connections)
    alias = setting_aliases("llm_model")[-1]
    with reply.path.open("ab") as handle:
        handle.write(f'{alias}="new-model" # legacy note\r\nLLM_MODEL="new-model"\r\n'.encode())
    preview = recovery.preview("environment", BackupSelection(backup_id=result["backup_id"]))
    recovery.restore("environment", restore_request(preview))
    lines = reply.path.read_text(encoding="utf-8-sig").splitlines()
    model_lines = [line for line in lines if line.startswith(("LLM_MODEL=", alias + "="))]
    assert len(model_lines) == 3 and all("old-model" in line for line in model_lines)
    assert any("# legacy note" in line for line in model_lines)


def test_manual_edit_during_restore_is_preserved_and_attempt_is_marked_failed(system, monkeypatch):
    from fastgpt_assistant.core.group_config_store import atomic_write

    journal, _, reply, connections, recovery = system
    result = connection_change(connections)
    preview = recovery.preview("environment", BackupSelection(backup_id=result["backup_id"]))
    before = reply.path.read_bytes()

    def concurrent_edit(path, content, **kwargs):
        if path == reply.path:
            path.write_bytes(before + b"MANUAL=retain\r\n")
        atomic_write(path, content, **kwargs)

    monkeypatch.setattr("fastgpt_assistant.core.config_recovery.atomic_write", concurrent_edit)
    with pytest.raises(ConfigConflict):
        recovery.restore("environment", restore_request(preview))
    assert reply.path.read_bytes() == before + b"MANUAL=retain\r\n"
    assert journal.list()["items"][0]["status"] == "failed"


def test_corrupt_history_blocks_save_without_exposing_diagnostics(system):
    journal, _, reply, connections, _ = system
    journal.path.parent.mkdir(parents=True)
    journal.path.write_bytes(b"private-malformed-database")
    before = reply.path.read_bytes()
    with pytest.raises(HistoryUnavailable) as error:
        connection_change(connections)
    assert "private" not in str(error.value)
    assert reply.path.read_bytes() == before


def test_oversized_backup_is_not_returned_or_restored(system):
    _, _, reply, _, recovery = system
    content = b"#" + b"x" * (2 * 1024 * 1024)
    name = f"20261012T010000000000Z-{revision_of(content)[:12]}.env"
    reply.backup_dir.mkdir()
    (reply.backup_dir / name).write_bytes(content)
    with pytest.raises(BackupUnavailable):
        recovery.preview("environment", BackupSelection(backup_id=name))


def test_preview_hides_secret_copied_into_current_public_field(system):
    _, _, reply, connections, recovery = system
    result = connection_change(connections)
    content = reply.path.read_bytes().replace(b"new-model", b"private-new-key")
    reply.path.write_bytes(content)
    preview = recovery.preview("environment", BackupSelection(backup_id=result["backup_id"]))
    change = next(change for change in preview["changes"] if change["field"] == "llm_model")
    assert change["before"] == "敏感内容已隐藏"
    assert "private" not in json.dumps(preview)


def test_backup_public_field_interpolating_secret_is_rejected(system):
    _, _, reply, _, recovery = system
    content = b'LLM_API_KEY="private-key-value"\nLLM_MODEL="${LLM_API_KEY}"\n'
    name = f"20261012T010000000000Z-{revision_of(content)[:12]}.env"
    reply.backup_dir.mkdir()
    (reply.backup_dir / name).write_bytes(content)
    original = reply.path.read_bytes()
    with pytest.raises(InvalidEnvFile) as error:
        recovery.preview("environment", BackupSelection(backup_id=name))
    assert "private" not in str(error.value)
    assert reply.path.read_bytes() == original


def test_history_apis_auth_csrf_privacy_and_restore_conflicts(system, tmp_path):
    _, groups, reply, _, _ = system
    auth = tmp_path / "auth.json"
    configure_admin(auth, "test-only-password")
    app = FastAPI()
    install_admin(
        app,
        groups_path=groups.path,
        env_path=reply.path,
        auth_path=auth,
        backup_dir=groups.backup_dir,
        runtime_settings=Settings(_env_file=None),
    )
    client = TestClient(app)
    assert client.get("/api/admin/history").status_code == 401
    assert client.get("/api/admin/history/backups/environment").status_code == 401
    assert client.post("/api/admin/history/backups/environment/preview", json={}).status_code == 401
    csrf = client.post("/api/admin/login", json={"password": "test-only-password"}).json()[
        "csrf_token"
    ]
    headers = {"X-CSRF-Token": csrf}
    values = client.get("/api/admin/connections").json()
    values["values"]["llm_model"] = "new-model"
    payload = {
        "revision": values["revision"],
        "values": values["values"],
        "secrets": {"llm_api_key": {"action": "set", "value": "private-api-input"}},
    }
    assert client.put("/api/admin/connections", json=payload, headers=headers).status_code == 200
    for path in ("/api/admin/history", "/api/admin/history/backups/environment"):
        response = client.get(path)
        assert response.headers["cache-control"] == "no-store"
        assert "private" not in response.text
    backup_id = client.get("/api/admin/history/backups/environment").json()["items"][0]["backup_id"]
    url = "/api/admin/history/backups/environment"
    assert client.post(url + "/preview", json={"backup_id": backup_id}).status_code == 403
    assert (
        client.post(
            url + "/preview",
            json={"backup_id": backup_id},
            headers={**headers, "Origin": "https://foreign.example"},
        ).status_code
        == 403
    )
    preview = client.post(url + "/preview", json={"backup_id": backup_id}, headers=headers)
    assert preview.status_code == 200 and "private" not in preview.text
    request = restore_request(preview.json()).model_dump()
    assert client.post(url + "/restore", json=request).status_code == 403
    assert client.post(url + "/restore", json=request, headers=headers).status_code == 200
    assert client.post(url + "/restore", json=request, headers=headers).status_code == 409
    assert client.get("/api/admin/history?limit=0").status_code == 422
    assert (
        client.post(
            url + "/preview", json={"backup_id": "../private-key"}, headers=headers
        ).status_code
        == 422
    )
