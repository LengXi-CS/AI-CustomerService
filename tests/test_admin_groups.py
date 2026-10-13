"""Group management checks use temporary files, never the deployed configuration."""

from pathlib import Path

import pytest
import tomlkit
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from fastgpt_assistant.core.admin_auth import COOKIE_NAME, AdminAuth, configure_admin
from fastgpt_assistant.core.group_config_store import (
    ConfigConflict,
    GroupConfigStore,
    GroupEdit,
    SaveGroupsRequest,
)
from fastgpt_assistant.core.wechat_groups import WechatGroupsProvider
from fastgpt_assistant.server.admin import install_admin

PASSWORD = "a-test-password-only-123"
SOURCE = """# Operator's header comment
[global]
enabled = true # Enable group routing
soothe_mode = "fixed"
soothe_fixed_reply = "原来的安抚文案"
future_global = "keep me"

[[groups]]
group_id = "19990001122@chatroom"
group_name = "测试客户群" # Keep the note
mode = "active"
owner_id = "wxid_owner"
customer_service_ids = ["wxid_staff"]
bot_aliases = ["瀚思商品助理"]
future_field = "preserved"
"""


@pytest.fixture
def store(tmp_path: Path) -> GroupConfigStore:
    path = tmp_path / "groups.toml"
    path.write_text(SOURCE, encoding="utf-8")
    return GroupConfigStore(path, tmp_path / "backups")


@pytest.fixture
def admin(tmp_path: Path, store: GroupConfigStore):
    auth_path = tmp_path / "auth.json"
    configure_admin(auth_path, PASSWORD)
    app = FastAPI()
    install_admin(app, groups_path=store.path, auth_path=auth_path, backup_dir=store.backup_dir)
    return TestClient(app), auth_path


def request_for(store: GroupConfigStore) -> SaveGroupsRequest:
    snapshot = store.snapshot()
    snapshot.pop("apply_mode")
    return SaveGroupsRequest.model_validate(snapshot)


def test_native_quote_switch_survives_admin_save_and_policy_reload(store):
    payload = request_for(store)
    assert payload.groups[0].native_quote_replies is False
    payload.groups[0].native_quote_replies = True
    store.save(payload)
    provider = WechatGroupsProvider(store.path)
    assert provider.current().get(payload.groups[0].group_id).native_quote_replies is True
    renamed = request_for(store)
    renamed.groups[0].group_name = "只修改群名"
    store.save(renamed)
    assert provider.current().get(payload.groups[0].group_id).native_quote_replies is True


def test_issue_association_switch_survives_save_and_legacy_backup_restore(store):
    payload = request_for(store)
    assert payload.groups[0].associate_customer_demands is False
    payload.groups[0].associate_customer_demands = True
    saved = store.save(payload)
    provider = WechatGroupsProvider(store.path)
    assert provider.current().get(payload.groups[0].group_id).associate_customer_demands is True
    # The legacy backup contains neither switch and must retain the safe defaults.
    backup = store.backup_dir / saved["backup_id"]
    restored = WechatGroupsProvider(backup).current()
    assert restored.get(payload.groups[0].group_id).associate_customer_demands is False
    renamed = request_for(store)
    renamed.groups[0].group_name = "只修改群名"
    store.save(renamed)
    assert provider.current().get(payload.groups[0].group_id).associate_customer_demands is True


def login(client: TestClient) -> str:
    response = client.post("/api/admin/login", json={"password": PASSWORD})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    return response.json()["csrf_token"]


@pytest.mark.parametrize("length", [6, 256])
def test_password_length_boundaries_can_be_configured_and_used(tmp_path, length):
    auth_path = tmp_path / "auth.json"
    password = "x" * length
    configure_admin(auth_path, password)
    auth = AdminAuth(auth_path)
    token, _ = auth.login(password, "127.0.0.1")
    assert auth.session(token) is not None


@pytest.mark.parametrize("length", [5, 257])
def test_invalid_password_length_does_not_replace_credentials(tmp_path, length):
    auth_path = tmp_path / "auth.json"
    configure_admin(auth_path, PASSWORD)
    original = auth_path.read_bytes()
    with pytest.raises(ValueError, match="6–256"):
        configure_admin(auth_path, "x" * length)
    assert auth_path.read_bytes() == original


def test_save_preserves_comments_unknown_fields_and_makes_exact_backup(store):
    original = store.path.read_bytes()
    payload = request_for(store)
    # Reading doesn't materialize owner_id into the explicit staff list.
    assert payload.groups[0].customer_service_ids == ["wxid_staff"]
    payload.groups[0].group_name = "改名后的客户群"
    payload.groups[0].mode = "shadow"
    result = store.save(payload)
    content = store.path.read_text(encoding="utf-8")
    assert "# Operator's header comment" in content
    assert "# Keep the note" in content
    assert "# Enable group routing" in content
    data = tomlkit.parse(content).unwrap()
    assert data["global"]["soothe_fixed_reply"] == "原来的安抚文案"
    assert data["global"]["future_global"] == "keep me"
    assert data["groups"][0]["future_field"] == "preserved"
    assert data["groups"][0]["mode"] == "shadow"
    assert (store.backup_dir / result["backup_id"]).read_bytes() == original


def test_save_conflict_keeps_external_edit_and_creates_no_backup(store):
    payload = request_for(store)
    store.path.write_text(SOURCE + "\n# Changed by operator\n", encoding="utf-8")
    external = store.path.read_bytes()
    with pytest.raises(ConfigConflict):
        store.save(payload)
    assert store.path.read_bytes() == external
    assert not store.backup_dir.exists()


def test_saved_policy_is_picked_up_by_existing_provider(store):
    provider = WechatGroupsProvider(store.path)
    assert provider.current().get("19990001122@chatroom").mode == "active"
    payload = request_for(store)
    payload.groups[0].mode = "disabled"
    payload.groups.append(GroupEdit(group_id="19990003344@chatroom", mode="shadow"))
    store.save(payload)
    assert provider.current().get("19990001122@chatroom").mode == "disabled"
    assert provider.current().get("19990003344@chatroom").mode == "shadow"
    payload = request_for(store)
    payload.groups = []
    store.save(payload)
    assert provider.current().groups == {}


def test_missing_file_can_be_created_but_starts_disabled(tmp_path):
    store = GroupConfigStore(tmp_path / "missing.toml", tmp_path / "backups")
    assert store.snapshot()["global_enabled"] is False
    payload = request_for(store)
    payload.groups.append(GroupEdit(group_id="19990001122@chatroom"))
    store.save(payload)
    assert store.path.is_file()
    assert not store.backup_dir.exists()


def test_backup_failure_does_not_replace_original(store, monkeypatch):
    import fastgpt_assistant.core.group_config_store as module

    original = store.path.read_bytes()
    payload = request_for(store)
    payload.global_enabled = False
    monkeypatch.setattr(
        module, "atomic_write", lambda *args, **kwargs: (_ for _ in ()).throw(OSError())
    )
    with pytest.raises(OSError):
        store.save(payload)
    assert store.path.read_bytes() == original


def test_unchanged_save_does_not_add_defaults_or_make_a_backup(store):
    original = store.path.read_bytes()
    result = store.save(request_for(store))
    assert result["changed"] is False
    assert result["backup_id"] is None
    assert store.path.read_bytes() == original


def test_edit_during_backup_is_detected_before_replacement(store, monkeypatch):
    import fastgpt_assistant.core.group_config_store as module

    original_write = module.atomic_write
    payload = request_for(store)
    payload.global_enabled = False
    external = (SOURCE + "\n# External edit during backup\n").encode("utf-8")

    def write_with_external_edit(path, content, **kwargs):
        original_write(path, content, **kwargs)
        if path.parent == store.backup_dir:
            store.path.write_bytes(external)

    monkeypatch.setattr(module, "atomic_write", write_with_external_edit)
    with pytest.raises(ConfigConflict):
        store.save(payload)
    assert store.path.read_bytes() == external


@pytest.mark.parametrize(
    "updates",
    [
        {"group_id": "wxid_invalid"},
        {"mode": "invalid"},
        {"allow_images": "false"},
        {"customer_service_ids": ["wxid_1", " wxid_1 "]},
        {"bot_aliases": [" "]},
        {"arbitrary": "value"},
    ],
)
def test_group_validation(updates):
    with pytest.raises(ValidationError):
        GroupEdit.model_validate({"group_id": "19990001122@chatroom", **updates})


def test_admin_api_requires_session_and_csrf(admin, store):
    client, _ = admin
    assert client.get("/api/admin/groups").status_code == 401
    assert client.put("/api/admin/groups", json={}).status_code == 401
    csrf = login(client)
    snapshot = client.get("/api/admin/groups").json()
    snapshot.pop("apply_mode")
    original = store.path.read_bytes()
    assert client.put("/api/admin/groups", json=snapshot).status_code == 403
    snapshot["groups"][0]["mode"] = "shadow"
    response = client.put("/api/admin/groups", json=snapshot, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert store.path.read_bytes() != original
    assert (
        client.put("/api/admin/groups", json=snapshot, headers={"X-CSRF-Token": csrf}).status_code
        == 409
    )
    assert client.post("/api/admin/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/admin/groups").status_code == 401


def test_invalid_and_duplicate_groups_do_not_write(admin, store):
    client, _ = admin
    csrf = login(client)
    payload = client.get("/api/admin/groups").json()
    payload.pop("apply_mode")
    original = store.path.read_bytes()
    payload["groups"].append(payload["groups"][0])
    assert (
        client.put("/api/admin/groups", json=payload, headers={"X-CSRF-Token": csrf}).status_code
        == 422
    )
    assert store.path.read_bytes() == original


def test_broken_source_file_is_not_overwritten(admin, store):
    client, _ = admin
    csrf = login(client)
    payload = client.get("/api/admin/groups").json()
    payload.pop("apply_mode")
    store.path.write_text("[global\n", encoding="utf-8")
    response = client.get("/api/admin/groups")
    assert response.status_code == 422
    assert (
        client.put("/api/admin/groups", json=payload, headers={"X-CSRF-Token": csrf}).status_code
        == 409
    )
    assert store.path.read_text(encoding="utf-8") == "[global\n"


def test_credentials_rotation_and_expired_session_revoke_access(admin, monkeypatch):
    client, auth_path = admin
    login(client)
    configure_admin(auth_path, "new-test-password-123")
    assert client.get("/api/admin/groups").status_code == 401
    auth = AdminAuth(auth_path)
    token, session = auth.login("new-test-password-123", "127.0.0.1")
    monkeypatch.setattr(
        "fastgpt_assistant.core.admin_auth.time.time", lambda: session.expires_at + 1
    )
    assert auth.session(token) is None


def test_no_default_password_and_login_throttle(tmp_path):
    app = FastAPI()
    path = tmp_path / "auth.json"
    install_admin(app, groups_path=tmp_path / "groups.toml", auth_path=path, backup_dir=tmp_path)
    client = TestClient(app)
    assert client.get("/api/admin/session").json()["configured"] is False
    assert client.post("/api/admin/login", json={"password": PASSWORD}).status_code == 503
    configure_admin(path, PASSWORD)
    for _ in range(5):
        assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/admin/login", json={"password": PASSWORD}).status_code == 429


def test_validation_never_echoes_password(admin):
    client, _ = admin
    value = "private-password-value" * 20
    response = client.post("/api/admin/login", json={"password": value})
    assert response.status_code == 422
    assert value not in response.text


def test_cross_origin_and_remote_http_login_are_rejected(admin):
    client, _ = admin
    assert (
        client.post(
            "/api/admin/login",
            json={"password": PASSWORD},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert (
        client.post("http://admin.example/api/admin/login", json={"password": PASSWORD}).status_code
        == 403
    )
    response = client.post("https://admin.example/api/admin/login", json={"password": PASSWORD})
    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]
    assert COOKIE_NAME in response.cookies
