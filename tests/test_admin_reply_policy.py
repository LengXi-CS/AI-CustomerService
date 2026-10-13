"""Reply strategy editing uses disposable files and never touches production secrets."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from fastgpt_assistant.core.admin_auth import configure_admin
from fastgpt_assistant.core.group_config_store import (
    ConfigConflict,
    GroupConfigStore,
    SaveGroupsRequest,
    SaveSootheRequest,
)
from fastgpt_assistant.core.reply_policy_store import (
    InvalidReplyFile,
    ReplyPolicyStore,
    RuntimeReplyPolicy,
    SaveRuntimeReplyRequest,
)
from fastgpt_assistant.core.wechat_groups import WechatGroupsProvider
from fastgpt_assistant.server.admin import install_admin

PASSWORD = "test-only-123"
TOML = """# Original operator note
[global]
enabled = true
soothe_mode = "fixed" # Existing mode note
soothe_fixed_reply = "原有文案"
future_value = "retain"

[[groups]]
group_id = "19990001122@chatroom"
mode = "active"
owner_id = "wxid_test_owner"
"""


@pytest.fixture
def runtime_store(tmp_path):
    return ReplyPolicyStore(tmp_path / ".env", tmp_path / "backups", environ={})


def runtime_request(store):
    snapshot = store.snapshot()
    return SaveRuntimeReplyRequest(**snapshot["values"], revision=snapshot["revision"])


@pytest.fixture
def admin(tmp_path, monkeypatch):
    # The test process itself may have policy env vars; isolate just these aliases.
    from fastgpt_assistant.core.reply_policy_store import policy_aliases

    for name in RuntimeReplyPolicy.model_fields:
        for key in policy_aliases(name):
            monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text('MODEL_API_KEY="private-test-secret"\n', encoding="utf-8")
    groups = tmp_path / "groups.toml"
    groups.write_text(TOML, encoding="utf-8")
    auth = tmp_path / "auth.json"
    configure_admin(auth, PASSWORD)
    app = FastAPI()
    install_admin(
        app,
        groups_path=groups,
        env_path=env,
        auth_path=auth,
        backup_dir=tmp_path / "groups-backups",
    )
    client = TestClient(app)
    csrf = client.post("/api/admin/login", json={"password": PASSWORD}).json()["csrf_token"]
    return client, {"X-CSRF-Token": csrf}, env, groups


def test_empty_runtime_file_uses_defaults_and_noop_does_not_create_it(runtime_store):
    snapshot = runtime_store.snapshot()
    assert snapshot["values"] == RuntimeReplyPolicy().model_dump()
    assert snapshot["restart_required"] is False
    runtime_store.save(runtime_request(runtime_store))
    assert not runtime_store.path.exists()


def test_runtime_save_retains_secret_bytes_comments_bom_and_line_endings(runtime_store):
    original = (
        b'\xef\xbb\xbf# retained header\r\nMODEL_API_KEY="test-only-secret # inside"\r\n'
        b'export EA_CONVERSATION_QUIET_WINDOW_SECONDS = "120" # wait note\r\n'
        b'EA_OWNER_NOTIFY_ENABLED=true\r\nUNKNOWN="line one\r\nline two"\r\n'
    )
    runtime_store.path.write_bytes(original)
    payload = runtime_request(runtime_store)
    payload.conversation_quiet_window_seconds = 60
    payload.owner_notify_enabled = False
    result = runtime_store.save(payload)
    saved = runtime_store.path.read_bytes()
    assert saved.startswith(b"\xef\xbb\xbf# retained header\r\n")
    assert b'MODEL_API_KEY="test-only-secret # inside"\r\n' in saved
    assert b"export EA_CONVERSATION_QUIET_WINDOW_SECONDS = 60 # wait note\r\n" in saved
    assert b'UNKNOWN="line one\r\nline two"\r\n' in saved
    assert result["values"]["owner_notify_enabled"] is False
    assert result["active_values"]["conversation_quiet_window_seconds"] == 120
    assert result["restart_required"] is True
    assert next(runtime_store.backup_dir.glob("*.env")).read_bytes() == original


def test_all_existing_aliases_and_duplicate_assignments_are_updated(runtime_store):
    runtime_store.path.write_text(
        "CONVERSATION_QUIET_WINDOW_SECONDS=90\n"
        "EA_CONVERSATION_QUIET_WINDOW_SECONDS=80\n"
        "CONVERSATION_QUIET_WINDOW_SECONDS=70\n",
        encoding="utf-8",
    )
    payload = runtime_request(runtime_store)
    assert payload.conversation_quiet_window_seconds == 70
    payload.conversation_quiet_window_seconds = 60
    runtime_store.save(payload)
    lines = runtime_store.path.read_text(encoding="utf-8").splitlines()
    assert all(line.endswith("=60") for line in lines)


def test_runtime_save_appends_only_changed_allowlisted_fields(runtime_store):
    runtime_store.path.write_bytes(b"OTHER=value")
    payload = runtime_request(runtime_store)
    payload.conversation_poll_interval_seconds = 15
    runtime_store.save(payload)
    assert (
        runtime_store.path.read_bytes() == b"OTHER=value\nCONVERSATION_POLL_INTERVAL_SECONDS=15\n"
    )


def test_runtime_conflict_does_not_overwrite_external_change(runtime_store):
    payload = runtime_request(runtime_store)
    runtime_store.path.write_bytes(b"# externally edited\n")
    with pytest.raises(ConfigConflict):
        runtime_store.save(payload)
    assert runtime_store.path.read_bytes() == b"# externally edited\n"
    assert not runtime_store.backup_dir.exists()


def test_environment_override_is_visible_and_readonly(tmp_path):
    env = tmp_path / ".env"
    env.write_text("CONVERSATION_QUIET_WINDOW_SECONDS=60\n", encoding="utf-8")
    store = ReplyPolicyStore(
        env, tmp_path / "backups", environ={"EA_CONVERSATION_QUIET_WINDOW_SECONDS": "90"}
    )
    snapshot = store.snapshot()
    assert snapshot["values"]["conversation_quiet_window_seconds"] == 90
    assert snapshot["fields"]["conversation_quiet_window_seconds"]["editable"] is False
    assert (
        snapshot["fields"]["conversation_quiet_window_seconds"]["source_key"]
        == "EA_CONVERSATION_QUIET_WINDOW_SECONDS"
    )
    payload = runtime_request(store)
    payload.conversation_quiet_window_seconds = 30
    with pytest.raises(ConfigConflict):
        store.save(payload)
    assert env.read_text(encoding="utf-8") == "CONVERSATION_QUIET_WINDOW_SECONDS=60\n"


def test_runtime_noop_preserves_entire_file(runtime_store):
    original = b"# comment\nOWNER_NOTIFY_ENABLED = 'true' # comment\n"
    runtime_store.path.write_bytes(original)
    runtime_store.save(runtime_request(runtime_store))
    assert runtime_store.path.read_bytes() == original
    assert not runtime_store.backup_dir.exists()


def test_environment_override_uses_same_precedence_as_settings(tmp_path):
    path = tmp_path / ".env"
    path.write_text("CONVERSATION_QUIET_WINDOW_SECONDS=invalid\n", encoding="utf-8")
    store = ReplyPolicyStore(
        path, tmp_path / "backups", environ={"CONVERSATION_QUIET_WINDOW_SECONDS": "120"}
    )
    assert store.snapshot()["values"]["conversation_quiet_window_seconds"] == 120


def test_external_edit_during_backup_is_preserved(runtime_store, monkeypatch):
    import fastgpt_assistant.core.reply_policy_store as module

    runtime_store.path.write_bytes(b"# original\n")
    payload = runtime_request(runtime_store)
    payload.owner_notify_enabled = False
    write = module.atomic_write
    external = b"# operator edited during backup\n"

    def interrupted_write(path, content, **kwargs):
        write(path, content, **kwargs)
        if path.parent == runtime_store.backup_dir:
            runtime_store.path.write_bytes(external)

    monkeypatch.setattr(module, "atomic_write", interrupted_write)
    with pytest.raises(ConfigConflict):
        runtime_store.save(payload)
    assert runtime_store.path.read_bytes() == external


@pytest.mark.parametrize(
    "content",
    [
        b"ROUTING_NORMAL_CONFIDENCE_THRESHOLD=nan\n",
        b"CONVERSATION_POLL_INTERVAL_SECONDS=0\n",
        b"ROUTING_HUMAN_CONFIDENCE_THRESHOLD=0.9\n",
        b"OTHER='unterminated\n",
    ],
)
def test_invalid_dotenv_or_policy_is_not_returned_or_written(runtime_store, content):
    runtime_store.path.write_bytes(content)
    with pytest.raises(InvalidReplyFile):
        runtime_store.snapshot()
    assert runtime_store.path.read_bytes() == content


@pytest.mark.parametrize(
    "updates",
    [
        {"conversation_quiet_window_seconds": -1},
        {"conversation_quiet_window_seconds": 86401},
        {"conversation_quiet_window_seconds": True},
        {"conversation_poll_interval_seconds": 0},
        {"conversation_poll_interval_seconds": 86401},
        {"conversation_poll_interval_seconds": 1.2},
        {"attachment_dispatch_interval_seconds": -1},
        {"attachment_dispatch_interval_seconds": 86401},
        {"routing_normal_confidence_threshold": 1.1},
        {"routing_human_confidence_threshold": 0.9},
        {"routing_normal_confidence_threshold": float("nan")},
        {"owner_notify_enabled": "false"},
        {"MODEL_API_KEY": "not editable here"},
    ],
)
def test_runtime_request_validation(updates):
    with pytest.raises(ValidationError):
        RuntimeReplyPolicy.model_validate(updates)


def test_runtime_pending_status_is_cleared_when_service_loads_saved_values(runtime_store):
    from types import SimpleNamespace

    payload = runtime_request(runtime_store)
    payload.conversation_quiet_window_seconds = 0
    saved = runtime_store.save(payload)
    assert saved["fields"]["conversation_quiet_window_seconds"]["status"] == "pending_restart"
    # Simulate a new process reading the saved values at startup.
    restarted = ReplyPolicyStore(
        runtime_store.path,
        runtime_store.backup_dir,
        runtime_settings=SimpleNamespace(**saved["values"]),
        environ={},
    )
    assert restarted.snapshot()["restart_required"] is False


def test_soothe_save_retains_groups_comments_and_hot_reloads(tmp_path):
    path = tmp_path / "groups.toml"
    path.write_text(TOML, encoding="utf-8")
    store = GroupConfigStore(path, tmp_path / "backups")
    provider = WechatGroupsProvider(path)
    provider.current()
    snapshot = store.soothe_snapshot()
    payload = SaveSootheRequest(**snapshot["values"], revision=snapshot["revision"])
    payload.soothe_fixed_reply = "新的测试安抚文案"
    store.save_soothe(payload)
    text = path.read_text(encoding="utf-8")
    assert "# Existing mode note" in text
    assert 'future_value = "retain"' in text
    assert 'owner_id = "wxid_test_owner"' in text
    assert provider.current().soothe_fixed_reply == "新的测试安抚文案"
    assert provider.current().get("19990001122@chatroom").mode == "active"


def test_soothe_and_groups_share_version_conflicts(tmp_path):
    path = tmp_path / "groups.toml"
    path.write_text(TOML, encoding="utf-8")
    store = GroupConfigStore(path, tmp_path / "backups")
    groups_snapshot = store.snapshot()
    groups_snapshot.pop("apply_mode")
    group_payload = SaveGroupsRequest.model_validate(groups_snapshot)
    soothe_snapshot = store.soothe_snapshot()
    soothe_payload = SaveSootheRequest(
        **soothe_snapshot["values"], revision=soothe_snapshot["revision"]
    )
    soothe_payload.soothe_mode = "model"
    store.save_soothe(soothe_payload)
    with pytest.raises(ConfigConflict):
        store.save(group_payload)
    group_payload = SaveGroupsRequest.model_validate(
        {key: value for key, value in store.snapshot().items() if key != "apply_mode"}
    )
    group_payload.global_enabled = False
    store.save(group_payload)
    with pytest.raises(ConfigConflict):
        store.save_soothe(soothe_payload)


def test_soothe_validation_and_noop_do_not_modify_file(tmp_path):
    path = tmp_path / "groups.toml"
    path.write_text(TOML, encoding="utf-8")
    store = GroupConfigStore(path, tmp_path / "backups")
    snapshot = store.soothe_snapshot()
    store.save_soothe(SaveSootheRequest(**snapshot["values"], revision=snapshot["revision"]))
    assert path.read_text(encoding="utf-8") == TOML
    with pytest.raises(ValidationError):
        SaveSootheRequest(
            soothe_mode="fixed", soothe_fixed_reply=" ", revision=snapshot["revision"]
        )
    assert not store.backup_dir.exists()


def test_reply_api_auth_csrf_validation_and_secret_redaction(admin):
    client, headers, env, groups = admin
    outsider = TestClient(client.app)
    for scope in ("soothe", "runtime"):
        assert outsider.get(f"/api/admin/reply-policy/{scope}").status_code == 401
        assert outsider.put(f"/api/admin/reply-policy/{scope}", json={}).status_code == 401
    response = client.get("/api/admin/reply-policy/runtime")
    assert response.status_code == 200
    assert "private-test-secret" not in response.text
    assert "MODEL_API_KEY" not in response.text
    snapshot = response.json()
    payload = {**snapshot["values"], "revision": snapshot["revision"]}
    assert client.put("/api/admin/reply-policy/runtime", json=payload).status_code == 403
    payload["conversation_quiet_window_seconds"] = 10
    assert (
        client.put("/api/admin/reply-policy/runtime", json=payload, headers=headers).json()[
            "restart_required"
        ]
        is True
    )
    invalid = {**payload, "MODEL_API_KEY": "rejected-secret"}
    response = client.put("/api/admin/reply-policy/runtime", json=invalid, headers=headers)
    assert response.status_code == 422
    assert "rejected-secret" not in response.text
    assert env.read_text(encoding="utf-8").startswith('MODEL_API_KEY="private-test-secret"\n')
    snapshot = client.get("/api/admin/reply-policy/soothe").json()
    payload = {
        **snapshot["values"],
        "revision": snapshot["revision"],
        "soothe_fixed_reply": "新的安抚话术",
    }
    assert (
        client.put("/api/admin/reply-policy/soothe", json=payload, headers=headers).status_code
        == 200
    )
    assert "新的安抚话术" in groups.read_text(encoding="utf-8")


def test_backup_failure_never_replaces_dotenv(runtime_store, monkeypatch):
    import fastgpt_assistant.core.reply_policy_store as module

    runtime_store.path.write_bytes(b"# original\n")
    original = runtime_store.path.read_bytes()
    payload = runtime_request(runtime_store)
    payload.owner_notify_enabled = False
    monkeypatch.setattr(
        module, "atomic_write", lambda *args, **kwargs: (_ for _ in ()).throw(OSError())
    )
    with pytest.raises(OSError):
        runtime_store.save(payload)
    assert runtime_store.path.read_bytes() == original
