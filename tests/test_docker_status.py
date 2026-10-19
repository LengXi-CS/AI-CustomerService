"""Docker monitoring is local, bounded, read-only, and does not expose CLI output."""

import json
import os
import subprocess

import pytest

from fastgpt_assistant.core import docker_status as module
from fastgpt_assistant.core.docker_status import DockerStatus


def test_docker_cli_on_path_takes_priority(monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda name: "path-docker")
    monkeypatch.setattr(module, "windows_user_profiles", lambda: pytest.fail("PATH is enough"))
    assert module.docker_executable() == "path-docker"


def cli_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    monkeypatch.setenv("PROGRAMW6432", str(tmp_path / "Program Files"))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "Program Files"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "systemprofile/AppData/Local"))
    monkeypatch.setattr(module, "windows_user_profiles", lambda: [])


def create_cli(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test-only-not-executed")
    return str(path)


def test_cli_system_install_is_found_without_path(monkeypatch, tmp_path):
    cli_environment(monkeypatch, tmp_path)
    expected = create_cli(tmp_path / "Program Files/Docker/Docker/resources/bin/docker.exe")
    assert module.docker_executable() == expected


def test_cli_current_user_install_is_found_without_path(monkeypatch, tmp_path):
    cli_environment(monkeypatch, tmp_path)
    user_data = tmp_path / "desktop-user/AppData/Local"
    monkeypatch.setenv("LOCALAPPDATA", str(user_data))
    expected = create_cli(user_data / "Programs/DockerDesktop/resources/bin/docker.exe")
    assert module.docker_executable() == expected


def test_service_finds_registered_users_docker_without_users_path_or_environment(
    monkeypatch, tmp_path
):
    cli_environment(monkeypatch, tmp_path)
    profile = tmp_path / "registered-desktop-user"
    monkeypatch.setattr(module, "windows_user_profiles", lambda: [profile])
    expected = create_cli(profile / "AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe")
    assert module.docker_executable() == expected


def test_missing_cli_remains_missing_after_searching_user_profiles(monkeypatch, tmp_path):
    cli_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "windows_user_profiles", lambda: [tmp_path / "missing-user"])
    assert module.docker_executable() is None


def monitor(monkeypatch, *, state="running", health="healthy", names="frpc\n",
            list_error=False, inspect_error=False, payload=None, failure=None):
    calls = []
    monkeypatch.setattr(module, "docker_executable", lambda: "/private/docker")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if failure:
            raise failure
        if "ls" in command:
            return subprocess.CompletedProcess(
                command, int(list_error), names, "private-list-error"
            )
        assert "inspect" in command
        body = payload if payload is not None else json.dumps({
            "status": state, "health": health, "started_at": "2026-10-16T01:37:29.6167717Z",
            "exit_code": 7 if state == "exited" else 0,
            "Error": "private-container-error", "Log": "private-container-logs",
        })
        return subprocess.CompletedProcess(
            command, int(inspect_error), body, "private-inspect-error"
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    return DockerStatus(), calls


def test_local_healthy_docker_and_frpc_use_only_read_only_commands(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://private-remote-server:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "private-remote-context")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "private-cert-path")
    probe, calls = monitor(monkeypatch)
    result = probe.snapshot()
    assert result["status"] == "healthy" and result["engine"] == "running"
    assert result["frpc"] == {
        "name": "frpc", "status": "running", "health": "healthy",
        "started_at": "2026-10-16T01:37:29.616771+00:00", "exit_code": 0,
    }
    assert len(calls) == 2
    for command, kwargs in calls:
        assert command[:2] == ["/private/docker", "--host"]
        assert command[2].startswith("npipe:" if os.name == "nt" else "unix:")
        assert command[3:5] in [["container", "ls"], ["container", "inspect"]]
        assert kwargs["timeout"] == 3 and kwargs.get("shell", False) is False
        assert not any(key in kwargs["env"] for key in (
            "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"
        ))
    assert "private" not in json.dumps(result)


def test_docker_engine_unreachable_does_not_claim_frpc_is_stopped(monkeypatch):
    probe, calls = monitor(monkeypatch, list_error=True)
    result = probe.snapshot()
    assert result["engine"] == "unavailable" and result["status"] == "degraded"
    assert result["frpc"]["status"] == "unknown"
    assert len(calls) == 1
    assert "private" not in json.dumps(result)


def test_missing_cli_is_distinct_from_unreachable_engine(monkeypatch):
    monkeypatch.setattr(module, "docker_executable", lambda: None)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("no CLI"))
    result = DockerStatus().snapshot()
    assert result["engine"] == "cli_missing"
    assert result["frpc"]["status"] == "unknown"


def test_missing_frpc_is_distinct_from_docker_engine_failure(monkeypatch):
    probe, calls = monitor(monkeypatch, names="")
    result = probe.snapshot()
    assert result["engine"] == "running" and result["frpc"]["status"] == "missing"
    assert result["status"] == "degraded" and len(calls) == 1


@pytest.mark.parametrize(("state", "health", "status"), [
    ("running", "unhealthy", "degraded"), ("running", "starting", "unknown"),
    ("running", None, "healthy"), ("exited", "healthy", "degraded"),
    ("paused", None, "degraded"), ("restarting", None, "degraded"),
    ("created", None, "degraded"), ("dead", None, "degraded"),
])
def test_container_lifecycle_and_health_are_not_conflated(monkeypatch, state, health, status):
    probe, _ = monitor(monkeypatch, state=state, health=health)
    result = probe.snapshot()
    assert result["engine"] == "running" and result["status"] == status
    assert result["frpc"]["status"] == state and result["frpc"]["health"] == health
    if state == "exited":
        assert result["frpc"]["exit_code"] == 7


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired("private-command", 3, output="private-output"),
    PermissionError("private-path"),
])
def test_probe_timeout_or_permissions_report_unknown_container_without_error_details(
    monkeypatch, failure
):
    probe, _ = monitor(monkeypatch, failure=failure)
    result = probe.snapshot()
    assert result["status"] == "degraded" and result["engine"] == "unavailable"
    assert result["frpc"]["status"] == "unknown"
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("payload", [
    "private-invalid-json", "[]", '{"status":"private-unknown"}',
    '{"status":"running","health":"private-unknown"}',
])
def test_invalid_inspect_response_is_unknown_and_never_echoed(monkeypatch, payload):
    probe, _ = monitor(monkeypatch, payload=payload)
    result = probe.snapshot()
    assert result["engine"] == "running" and result["status"] == "unknown"
    assert result["frpc"]["status"] == "unknown"
    assert "private" not in json.dumps(result)


def test_inspect_failure_does_not_erase_successful_engine_check(monkeypatch):
    probe, _ = monitor(monkeypatch, inspect_error=True)
    result = probe.snapshot()
    assert result["engine"] == "running" and result["frpc"]["status"] == "unknown"
    assert result["status"] == "unknown"


def test_refresh_cache_expires_and_caller_cannot_mutate_it(monkeypatch):
    probe, calls = monitor(monkeypatch, list_error=True)
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    first = probe.snapshot()
    first["engine"] = "running"
    first["frpc"]["status"] = "running"
    assert probe.snapshot()["engine"] == "unavailable" and len(calls) == 1
    clock[0] += 14
    assert probe.snapshot()["frpc"]["status"] == "unknown" and len(calls) == 1
    clock[0] += 1
    probe.snapshot()
    assert len(calls) == 2


def test_never_started_container_has_no_year_one_start_time(monkeypatch):
    probe, _ = monitor(monkeypatch, payload=json.dumps({
        "status": "created", "health": None, "started_at": "0001-01-01T00:00:00Z",
        "exit_code": False,
    }))
    result = probe.snapshot()
    assert result["frpc"]["started_at"] is None and result["frpc"]["exit_code"] is None
