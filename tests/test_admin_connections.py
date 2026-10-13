"""Credential editing and connection probes never use live provider accounts."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from fastgpt_assistant.core.admin_auth import configure_admin
from fastgpt_assistant.core.config import Settings
from fastgpt_assistant.core.connection_config import (
    SECRET_NAMES,
    ConnectionConfigStore,
    ConnectionValues,
    SaveConnectionsRequest,
    SecretEdit,
    probe_connection,
)
from fastgpt_assistant.core.dotenv_store import EnvironmentDocument, setting_aliases
from fastgpt_assistant.core.group_config_store import ConfigConflict
from fastgpt_assistant.core.reply_policy_store import ReplyPolicyStore, SaveRuntimeReplyRequest
from fastgpt_assistant.server.admin import install_admin


@pytest.fixture
def store(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# retain\nLLM_BASE_URL='https://model.example/v1'\n"
        "MODEL_API_KEY='private-test-key' # note\n"
        "EA_LARK_APP_SECRET='private-lark-secret'\n"
        "VAULT_SECRET_KEY='unrelated-vault-secret'\n",
        encoding="utf-8",
    )
    return ConnectionConfigStore(EnvironmentDocument(path, tmp_path / "backups"), environ={})


def request_for(store, secrets=None):
    snapshot = store.snapshot()
    return SaveConnectionsRequest(
        revision=snapshot["revision"], values=snapshot["values"], secrets=secrets or {}
    )


def test_snapshot_never_returns_credentials_or_digests(store):
    text = str(store.snapshot())
    assert "private-test-key" not in text
    assert "private-lark-secret" not in text
    assert "unrelated-vault-secret" not in text
    assert "digest" not in text
    assert store.snapshot()["secrets"]["llm_api_key"]["configured"] is True


def test_keep_retain_and_noop_do_not_modify_or_backup(store):
    original = store.document.read()
    store.save(request_for(store, {"llm_api_key": SecretEdit(action="keep")}))
    assert store.document.read() == original
    assert not store.document.backup_dir.exists()


def test_secret_replacement_and_clear_preserve_unrelated_values_and_comments(store):
    original = store.document.read()
    new = "new-test-'quote'\\backslash#fragment"
    payload = request_for(
        store,
        {
            "llm_api_key": SecretEdit(action="set", value=new),
            "lark_app_secret": SecretEdit(action="clear"),
        },
    )
    response = store.save(payload)
    text = store.document.read().decode("utf-8")
    assert "# note" in text
    assert "VAULT_SECRET_KEY='unrelated-vault-secret'" in text
    assert "private-test-key" not in text
    assert "private-lark-secret" not in text
    assert next(store.document.backup_dir.glob("*.env")).read_bytes() == original
    assert store.probe_values(request_for(store))["llm_api_key"] == new
    assert response["secrets"]["lark_app_secret"]["configured"] is False
    assert new not in str(response)
    assert response["restart_required"] is True


def test_secret_duplicate_aliases_are_updated_together(store):
    store.document.path.write_text(
        "LLM_API_KEY=first\nMODEL_API_KEY=second\nEA_DEEPSEEK_API_KEY=third\n", encoding="utf-8"
    )
    store.save(request_for(store, {"llm_api_key": SecretEdit(action="set", value="replacement")}))
    lines = store.document.read().decode().splitlines()
    assert all(line.endswith("='replacement'") for line in lines)


def test_runtime_and_connections_share_lock_and_conflict_check(store):
    reply = ReplyPolicyStore(
        store.document.path, store.document.backup_dir, document=store.document, environ={}
    )
    assert reply._lock is store.document.lock
    payload = request_for(store)
    snapshot = reply.snapshot()
    edit = SaveRuntimeReplyRequest(**snapshot["values"], revision=snapshot["revision"])
    edit.owner_notify_enabled = False
    reply.save(edit)
    with pytest.raises(ConfigConflict):
        store.save(payload)


def test_environment_secret_is_readonly_and_never_returned(store):
    guarded = ConnectionConfigStore(store.document, environ={"LLM_API_KEY": "env-private-secret"})
    assert guarded.snapshot()["secrets"]["llm_api_key"]["editable"] is False
    assert "env-private-secret" not in str(guarded.snapshot())
    with pytest.raises(ConfigConflict):
        guarded.save(request_for(guarded, {"llm_api_key": SecretEdit(action="clear")}))


def test_saved_secret_status_becomes_effective_after_restart(store):
    saved = store.save(
        request_for(store, {"llm_api_key": SecretEdit(action="set", value="new-test-key")})
    )
    assert saved["secrets"]["llm_api_key"]["pending_restart"] is True
    values = store.probe_values(request_for(store))
    restarted = ConnectionConfigStore(
        store.document, runtime_settings=SimpleNamespace(**values), environ={}
    )
    assert restarted.snapshot()["secrets"]["llm_api_key"]["pending_restart"] is False


def test_legacy_url_credentials_are_not_returned_as_active_values(store):
    values = store.probe_values(request_for(store))
    values["llm_base_url"] = "https://user:private-legacy-password@model.example"
    legacy = ConnectionConfigStore(
        store.document, runtime_settings=SimpleNamespace(**values), environ={}
    )
    assert "private-legacy-password" not in str(legacy.snapshot())
    assert legacy.snapshot()["fields"]["llm_base_url"]["pending_restart"] is True


@pytest.mark.parametrize(
    "edit",
    [
        {"action": "set", "value": ""},
        {"action": "set", "value": "\n"},
        {"action": "set", "value": "${SECRET}"},
        {"action": "clear", "value": "wrong"},
        {"action": "keep", "value": "wrong"},
        {"action": "invalid"},
    ],
)
def test_secret_action_validation(edit):
    with pytest.raises(ValidationError):
        SecretEdit.model_validate(edit)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "https://user:pass@example.com",
        "https://example.com?key=secret",
        "http://[",
        "https://example.com:99999",
    ],
)
def test_url_validation(url):
    with pytest.raises(ValidationError):
        ConnectionValues(llm_base_url=url)


def probe_fixture():
    return {
        **ConnectionValues(
            llm_base_url="https://model.example/v1",
            llm_model="test-model",
            fastgpt_dataset_id="test-dataset",
            gewe_app_id="test-app",
            lark_app_id="test-app",
            lark_bitable_app_token="test-base",
        ).model_dump(),
        **dict.fromkeys(SECRET_NAMES, "private-probe-credential"),
        "fastgpt_root_key": "private-compatibility-key",
    }


@pytest.mark.parametrize("service", ["llm", "fastgpt", "gewe", "lark"])
def test_readonly_probes_do_not_write_files_or_send_messages(service):
    calls = []

    def transport(request):
        calls.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        if request.url.path.endswith("searchTest"):
            return httpx.Response(200, json={"code": 200, "data": {"list": []}})
        if request.url.path.endswith("checkOnline"):
            return httpx.Response(200, json={"ret": 200, "data": True})
        if request.url.path.endswith("internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "private-tenant-token"}
            )
        return httpx.Response(200, json={"code": 0, "data": {"items": []}})

    result = probe_connection(service, probe_fixture(), transport=httpx.MockTransport(transport))
    assert result["level"] == "passed"
    assert "private-probe-credential" not in str(result)
    assert "private-tenant-token" not in str(result)
    assert not any("message/" in str(call.url) or "setCallback" in str(call.url) for call in calls)


@pytest.mark.parametrize("status", [401, 403, 500, 308])
def test_probe_failures_redact_upstream_messages_and_do_not_follow_redirects(status):
    result = probe_connection(
        "gewe",
        probe_fixture(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                json={"msg": "private-probe-credential"},
                headers={"Location": "https://other.example"},
            )
        ),
    )
    assert result["level"] == "failed"
    assert "private-probe-credential" not in str(result)


def test_probe_offline_and_unsupported_list_are_partial():
    assert (
        probe_connection(
            "gewe",
            probe_fixture(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"ret": 200, "data": False})
            ),
        )["level"]
        == "partial"
    )
    assert (
        probe_connection(
            "llm",
            probe_fixture(),
            transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        )["level"]
        == "partial"
    )


def test_draft_probe_does_not_save_credentials(store):
    original = store.document.read()
    payload = request_for(store, {"llm_api_key": SecretEdit(action="set", value="draft-key")})
    assert store.probe_values(payload)["llm_api_key"] == "draft-key"
    assert store.document.read() == original
    assert not store.document.backup_dir.exists()


def test_connection_api_is_authenticated_and_write_only(tmp_path, monkeypatch):
    for name in [*ConnectionValues.model_fields, *SECRET_NAMES, "fastgpt_root_key"]:
        for key in setting_aliases(name):
            monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=private-current-key\n", encoding="utf-8")
    auth = tmp_path / "auth.json"
    configure_admin(auth, "test-password")
    app = FastAPI()
    install_admin(
        app,
        groups_path=tmp_path / "groups.toml",
        env_path=env,
        auth_path=auth,
        backup_dir=tmp_path / "backups",
    )
    client = TestClient(app)
    assert client.get("/api/admin/connections").status_code == 401
    assert client.put("/api/admin/connections", json={}).status_code == 401
    assert client.post("/api/admin/connections/test/gewe", json={}).status_code == 401
    csrf = client.post("/api/admin/login", json={"password": "test-password"}).json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}
    response = client.get("/api/admin/connections")
    assert "private-current-key" not in response.text
    snapshot = response.json()
    payload = {
        "revision": snapshot["revision"],
        "values": snapshot["values"],
        "secrets": {"llm_api_key": {"action": "set", "value": "private-new-key"}},
    }
    assert client.put("/api/admin/connections", json=payload).status_code == 403
    response = client.put("/api/admin/connections", json=payload, headers=headers)
    assert response.status_code == 200
    assert "private-new-key" not in response.text
    assert b"private-new-key" in env.read_bytes()
    payload["secrets"] = {"llm_api_key": {"action": "keep", "value": "rejected-secret-value"}}
    response = client.put("/api/admin/connections", json=payload, headers=headers)
    assert response.status_code == 422
    assert "rejected-secret-value" not in response.text


def test_lark_aliases_are_read_consistently_by_shared_settings(tmp_path, monkeypatch):
    for name in ("lark_app_id", "lark_app_secret", "lark_bitable_app_token"):
        for key in setting_aliases(name):
            monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "LARK_APP_ID=new-id\nEA_LARK_APP_SECRET=legacy-secret\nLARK_BITABLE_APP_TOKEN=new-base\n",
        encoding="utf-8",
    )
    configuration = Settings(_env_file=path)
    assert configuration.lark_app_id == "new-id"
    assert configuration.lark_app_secret == "legacy-secret"
    assert configuration.lark_bitable_app_token == "new-base"


def test_connection_api_probe_uses_draft_without_saving(tmp_path, monkeypatch):
    for name in [*ConnectionValues.model_fields, *SECRET_NAMES]:
        for key in setting_aliases(name):
            monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=current-test-key\n", encoding="utf-8")
    original = env.read_bytes()
    auth = tmp_path / "auth.json"
    configure_admin(auth, "test-password")
    app = FastAPI()
    install_admin(
        app,
        groups_path=tmp_path / "groups.toml",
        env_path=env,
        auth_path=auth,
        backup_dir=tmp_path / "backups",
    )
    client = TestClient(app)
    csrf = client.post("/api/admin/login", json={"password": "test-password"}).json()["csrf_token"]
    snapshot = client.get("/api/admin/connections").json()
    payload = {
        "revision": snapshot["revision"],
        "values": snapshot["values"],
        "secrets": {"llm_api_key": {"action": "set", "value": "draft-test-key"}},
    }
    captured = []

    def probe(service, values):
        captured.append((service, values["llm_api_key"]))
        return {"service": service, "level": "passed", "message": "测试验证通过"}

    monkeypatch.setattr("fastgpt_assistant.server.admin.probe_connection", probe)
    assert client.post("/api/admin/connections/test/llm", json=payload).status_code == 403
    response = client.post(
        "/api/admin/connections/test/llm", json=payload, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200
    assert captured == [("llm", "draft-test-key")]
    assert "draft-test-key" not in response.text
    assert env.read_bytes() == original
