"""Observe AidNet and its own frpc process without reading account or node data."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

_PROCESS_QUERY = """
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$items = Get-CimInstance Win32_Process -Filter "Name='AidNet.exe' OR Name='frpc.exe'"
$selected = @($items | Select-Object Name,ProcessId,ParentProcessId,ExecutablePath)
ConvertTo-Json -Compress -InputObject $selected
"""


def windows_supported() -> bool:
    return os.name == "nt"


def aidnet_processes() -> list[dict]:
    root = Path(os.environ.get("SYSTEMROOT", "C:/Windows"))
    executable = root / "System32/WindowsPowerShell/v1.0/powershell.exe"
    response = subprocess.run(
        [str(executable), "-NoProfile", "-NonInteractive", "-Command", _PROCESS_QUERY],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=4, check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if response.returncode:
        raise ValueError("process observation unavailable")
    rows = json.loads(response.stdout)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid process observation")
    return rows


def _aidnet_frpc(process: dict, app_ids: set[int], roots: set[PureWindowsPath]) -> bool:
    if process.get("ParentProcessId") in app_ids:
        return True
    path = process.get("ExecutablePath")
    if not isinstance(path, str) or not path:
        return False
    executable = PureWindowsPath(path)
    if executable.name.lower() != "frpc.exe":
        return False
    # Parent matching handles alternate install directories. Path matching also
    # recognises an orphaned AidNet frpc after the main program has closed.
    return any(root in executable.parents for root in roots) or (
        tuple(part.lower() for part in executable.parts[-5:])
        == ("aidnet", "resources", "bin", "win32-x64", "frpc.exe")
    )


class AidNetStatus:
    refresh_seconds = 15

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._cached: dict | None = None
        self._checked_clock = float("-inf")

    def snapshot(self) -> dict:
        with self._guard:
            expired = time.monotonic() - self._checked_clock >= self.refresh_seconds
            if self._cached is None or expired:
                self._cached = self._probe()
                self._cached["checked_at"] = datetime.now(UTC).isoformat()
                self._checked_clock = time.monotonic()
            return copy.deepcopy(self._cached)

    @staticmethod
    def _probe() -> dict:
        result = {
            "status": "unknown", "message": "AidNet 进程状态尚未确认。",
            "app": "unknown", "frpc": "unknown",
        }
        if not windows_supported():
            result["message"] = "AidNet 进程检测仅支持 Windows 客服主机。"
            return result
        try:
            rows = aidnet_processes()
            apps = [row for row in rows if str(row.get("Name", "")).lower() == "aidnet.exe"]
            app_ids = {row["ProcessId"] for row in apps if type(row.get("ProcessId")) is int}
            roots = {
                PureWindowsPath(row["ExecutablePath"]).parent for row in apps
                if isinstance(row.get("ExecutablePath"), str) and row["ExecutablePath"]
            }
            frpcs = [row for row in rows if str(row.get("Name", "")).lower() == "frpc.exe"]
            matching = any(_aidnet_frpc(row, app_ids, roots) for row in frpcs)
            unreadable = any(not row.get("ExecutablePath") for row in frpcs)
            result["app"] = "running" if apps else "stopped"
            result["frpc"] = "running" if matching else "unknown" if unreadable else "stopped"
            if result["app"] == "stopped":
                result.update(
                    status="degraded",
                    message="未检测到 AidNet 主程序，请打开 AidNet 并检查节点连接。",
                )
            elif result["frpc"] == "stopped":
                result.update(
                    status="degraded", message=("AidNet 主程序运行中，但内置连接进程未运行，"
                                                "请在 AidNet 中检查节点。"),
                )
            elif result["frpc"] == "unknown":
                result["message"] = (
                    "AidNet 主程序运行中，内置连接进程归属无法确认，请检查进程读取权限。"
                )
            else:
                result.update(status="healthy", message="AidNet 主程序与内置连接进程均在运行。")
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            result["message"] = "无法读取 AidNet 进程状态，请检查客服服务账户权限或稍后刷新。"
        return result
