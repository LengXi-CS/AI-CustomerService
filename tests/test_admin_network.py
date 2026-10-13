"""Trusted LAN HTTP is opt-in, limited to both target and actual client subnet."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from fastgpt_assistant.core.admin_auth import configure_admin
from fastgpt_assistant.core.admin_network import lan_http_allowed
from fastgpt_assistant.core.config import Settings
from fastgpt_assistant.core.dotenv_store import EnvironmentDocument, quote_value
from fastgpt_assistant.server.admin import install_admin


@pytest.mark.parametrize(
    "host,client,networks,allowed",
    [
        ("192.168.2.73", "192.168.2.10", "192.168.2.0/24", True),
        ("192.168.2.73", "::ffff:192.168.2.10", "192.168.2.0/24", True),
        ("192.168.2.73", "192.168.2.10", "", False),
        ("192.168.2.73", "192.168.3.10", "192.168.2.0/24", False),
        ("192.168.2.73", "203.0.113.10", "192.168.2.0/24", False),
        ("203.0.113.73", "192.168.2.10", "192.168.2.0/24", False),
        ("admin.example", "192.168.2.10", "192.168.2.0/24", False),
        ("192.168.2.73", "192.168.2.10", "0.0.0.0/0", False),
    ],
)
def test_lan_scope(host, client, networks, allowed):
    assert lan_http_allowed(host, client, networks) is allowed


@pytest.mark.parametrize("network", ["0.0.0.0/0", "127.0.0.0/8", "203.0.113.0/24", "::/0"])
def test_settings_reject_public_or_unbounded_networks(network):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ADMIN_LAN_HTTP_NETWORKS=network)


def test_login_from_allowed_lan_keeps_auth_csrf_and_public_http_rejected(tmp_path):
    auth = tmp_path / "auth.json"
    configure_admin(auth, "test-password-123")
    app = FastAPI()
    settings = Settings(_env_file=None, ADMIN_LAN_HTTP_NETWORKS="192.168.2.0/24")
    install_admin(
        app,
        groups_path=tmp_path / "groups.toml",
        auth_path=auth,
        backup_dir=tmp_path / "backups",
        env_path=tmp_path / ".env",
        runtime_settings=settings,
    )
    client = TestClient(app, base_url="http://192.168.2.73:5330", client=("192.168.2.10", 12345))
    assert client.post("/api/admin/login", json={"password": "wrong-password"}).status_code == 401
    result = client.post("/api/admin/login", json={"password": "test-password-123"})
    assert result.status_code == 200 and result.json()["csrf_token"]
    assert "HttpOnly" in result.headers["set-cookie"]
    assert "SameSite=strict" in result.headers["set-cookie"]
    assert client.post("/api/admin/logout").status_code == 403
    assert (
        client.post(
            "/api/admin/login",
            json={"password": "test-password-123"},
            headers={"Origin": "http://untrusted.invalid"},
        ).status_code
        == 403
    )
    outsider = TestClient(app, base_url="http://192.168.2.73:5330", client=("203.0.113.10", 12345))
    assert (
        outsider.post("/api/admin/login", json={"password": "test-password-123"}).status_code == 403
    )


def test_enable_lan_setting_preserves_existing_credentials_and_comments(tmp_path):
    path = tmp_path / ".env"
    original = b"# Keep comments\r\nLLM_API_KEY='test-only-secret'\r\nUNMANAGED=value\r\n"
    path.write_bytes(original)
    document = EnvironmentDocument(path, tmp_path / "backups")
    candidate = document.render(
        original, {"admin_lan_http_networks": quote_value("192.168.2.0/24")}
    )
    document.commit(original, candidate)
    assert path.read_bytes().startswith(original)
    assert Settings(_env_file=path).admin_lan_http_networks == "192.168.2.0/24"
    assert next((tmp_path / "backups").glob("*.env")).read_bytes() == original
