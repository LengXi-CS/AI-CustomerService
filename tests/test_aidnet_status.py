"""AidNet's native frpc must not be confused with an unrelated frpc process."""

import json
import subprocess

import pytest

from fastgpt_assistant.core import aidnet_status as module
from fastgpt_assistant.core.aidnet_status import AidNetStatus

APP = {
    "Name": "AidNet.exe", "ProcessId": 101, "ParentProcessId": 1,
    "ExecutablePath": "D:\\private-install\\AidNet.exe",
}
CONNECTION = {
    "Name": "frpc.exe", "ProcessId": 102, "ParentProcessId": 101,
    "ExecutablePath": "D:\\private-install\\resources\\bin\\win32-x64\\frpc.exe",
}


def probe(monkeypatch, rows):
    monkeypatch.setattr(module, "windows_supported", lambda: True)
    monkeypatch.setattr(module, "aidnet_processes", lambda: rows)
    return AidNetStatus().snapshot()


def test_healthy_aidnet_returns_no_process_ids_or_install_paths(monkeypatch):
    result = probe(monkeypatch, [APP, CONNECTION])
    assert result["status"] == "healthy"
    assert result["app"] == result["frpc"] == "running"
    assert "private" not in json.dumps(result)
    assert "ProcessId" not in json.dumps(result)


def test_an_unrelated_frpc_does_not_make_aidnet_connection_healthy(monkeypatch):
    result = probe(monkeypatch, [APP, {
        **CONNECTION, "ParentProcessId": 999, "ExecutablePath": "D:\\other\\frpc.exe",
    }])
    assert result["app"] == "running" and result["frpc"] == "stopped"
    assert result["status"] == "degraded"


def test_matching_parent_can_identify_aidnet_frpc_when_path_is_unreadable(monkeypatch):
    result = probe(monkeypatch, [APP, {**CONNECTION, "ExecutablePath": None}])
    assert result["status"] == "healthy"


def test_matching_install_directory_identifies_detached_child(monkeypatch):
    result = probe(monkeypatch, [APP, {**CONNECTION, "ParentProcessId": 999}])
    assert result["status"] == "healthy"


def test_unattributed_frpc_is_unknown_instead_of_stopped(monkeypatch):
    result = probe(monkeypatch, [APP, {
        **CONNECTION, "ExecutablePath": None, "ParentProcessId": 999,
    }])
    assert result["app"] == "running" and result["frpc"] == "unknown"
    assert result["status"] == "unknown"


def test_aidnet_stopped_but_orphaned_own_frpc_is_visible(monkeypatch):
    result = probe(monkeypatch, [{
        **CONNECTION, "ExecutablePath": "D:\\AidNet\\resources\\bin\\win32-x64\\frpc.exe",
    }])
    assert result["app"] == "stopped" and result["frpc"] == "running"
    assert result["status"] == "degraded"


def test_aidnet_running_without_connection_process_is_degraded(monkeypatch):
    result = probe(monkeypatch, [APP])
    assert result["app"] == "running" and result["frpc"] == "stopped"
    assert result["status"] == "degraded"


def test_no_aidnet_processes_reports_not_running(monkeypatch):
    result = probe(monkeypatch, [])
    assert result["app"] == result["frpc"] == "stopped"
    assert result["status"] == "degraded"


@pytest.mark.parametrize("failure", [
    PermissionError("private-error"), subprocess.TimeoutExpired("private-command", 4),
    ValueError("private-response"),
])
def test_query_failures_are_unknown_without_echoing_details(monkeypatch, failure):
    monkeypatch.setattr(module, "windows_supported", lambda: True)

    def fail():
        raise failure

    monkeypatch.setattr(module, "aidnet_processes", fail)
    result = AidNetStatus().snapshot()
    assert result["app"] == result["frpc"] == result["status"] == "unknown"
    assert "private" not in json.dumps(result)


def test_unsupported_platform_does_not_run_powershell(monkeypatch):
    monkeypatch.setattr(module, "windows_supported", lambda: False)
    monkeypatch.setattr(module, "aidnet_processes", lambda: pytest.fail("unsupported OS"))
    assert AidNetStatus().snapshot()["status"] == "unknown"


def test_process_query_is_bounded_and_does_not_read_command_lines(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps([APP, CONNECTION]), "")

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.aidnet_processes() == [APP, CONNECTION]
    command, kwargs = calls[0]
    assert "-NoProfile" in command and "-NonInteractive" in command
    assert "CommandLine" not in command[-1]
    assert "Get-CimInstance Win32_Process" in command[-1]
    assert "Start-Process" not in command[-1] and "Stop-Process" not in command[-1]
    assert kwargs["timeout"] == 4 and not kwargs.get("shell")


def test_aidnet_cache_expires_and_does_not_share_mutable_results(monkeypatch):
    monkeypatch.setattr(module, "windows_supported", lambda: True)
    calls = []
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def observe():
        calls.append(True)
        return [APP, CONNECTION] if len(calls) == 1 else []

    monkeypatch.setattr(module, "aidnet_processes", observe)
    monitor = AidNetStatus()
    first = monitor.snapshot()
    first["app"] = "changed"
    assert monitor.snapshot()["app"] == "running" and len(calls) == 1
    clock[0] += 15
    assert monitor.snapshot()["app"] == "stopped" and len(calls) == 2
