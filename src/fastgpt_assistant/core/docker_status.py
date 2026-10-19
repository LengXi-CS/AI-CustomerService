"""Bounded, read-only observations of the local Docker engine and frpc container."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

CONTAINER_STATES = {"created", "running", "paused", "restarting", "removing", "exited", "dead"}
HEALTH_STATES = {"healthy", "unhealthy", "starting"}
_STATE_FORMAT = (
    '{"status":{{json .State.Status}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
    '"started_at":{{json .State.StartedAt}},"exit_code":{{json .State.ExitCode}}}'
)


def windows_user_profiles() -> list[Path]:
    """Read registered profile locations, including when running as LocalSystem."""
    try:
        import winreg
    except ImportError:
        return []
    profiles = []
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList",
            access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as parent:
            for index in range(winreg.QueryInfoKey(parent)[0]):
                try:
                    with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as child:
                        value = winreg.QueryValueEx(child, "ProfileImagePath")[0]
                    if isinstance(value, str) and value:
                        profiles.append(Path(os.path.expandvars(value)))
                except OSError:
                    continue
    except OSError:
        pass
    return profiles


def docker_executable() -> str | None:
    if executable := shutil.which("docker"):
        return executable
    # Services inherit neither the desktop user's PATH nor LOCALAPPDATA. Docker
    # Desktop also supports per-user installs outside the system Program Files.
    candidates = []
    program_files = os.environ.get("PROGRAMW6432") or os.environ.get("PROGRAMFILES")
    if program_files:
        candidates.append(Path(program_files) / "Docker/Docker/resources/bin/docker.exe")
    if local_app_data := os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(local_app_data) / "Programs/DockerDesktop/resources/bin/docker.exe")
    for profile in windows_user_profiles():
        candidates.append(profile / "AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _started_at(value: object) -> str | None:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
        if parsed is None or parsed.year < 1970:
            return None
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC).isoformat()
    except (ValueError, OverflowError):
        return None


class DockerStatus:
    """One shared probe per refresh interval; never start or stop containers.

    Docker is contacted through the local Linux engine pipe/socket explicitly.
    An operator's remote DOCKER_HOST/context must not be labelled local Docker.
    No CLI output, environment, container log, or inspection error is exposed.
    """

    refresh_seconds = 15
    command_timeout = 3

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._cached: dict | None = None
        self._checked_clock = float("-inf")

    @staticmethod
    def _empty() -> dict:
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "status": "unknown",
            "message": "Docker 状态尚未确认。",
            "engine": "unknown",
            "frpc": {
                "name": "frpc", "status": "unknown", "health": None,
                "started_at": None, "exit_code": None,
            },
        }

    def snapshot(self) -> dict:
        with self._guard:
            expired = time.monotonic() - self._checked_clock >= self.refresh_seconds
            if self._cached is None or expired:
                self._cached = self._probe()
                self._cached["checked_at"] = datetime.now(UTC).isoformat()
                self._checked_clock = time.monotonic()
            return copy.deepcopy(self._cached)

    def _run(self, executable: str, arguments: list[str]) -> subprocess.CompletedProcess:
        endpoint = (
            "npipe:////./pipe/dockerDesktopLinuxEngine"
            if os.name == "nt" else "unix:///var/run/docker.sock"
        )
        environment = {
            key: value for key, value in os.environ.items()
            if key not in {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}
        }
        return subprocess.run(
            [executable, "--host", endpoint, *arguments],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=self.command_timeout, check=False, env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    def _probe(self) -> dict:
        result = self._empty()
        executable = docker_executable()
        if not executable:
            result.update(
                engine="cli_missing", status="degraded",
                message=("未找到 Docker 命令，无法检测。"
                         "请确认已安装 Docker Desktop，且客服服务可访问 Docker。"),
            )
            return result
        try:
            listed = self._run(executable, [
                "container", "ls", "--all", "--filter", "name=^/frpc$", "--format", "{{.Names}}",
            ])
            if listed.returncode:
                result.update(
                    engine="unavailable", status="degraded",
                    message=("无法连接本机 Docker 引擎。"
                             "请确认 Docker Desktop 已启动，且客服服务账户有访问权限。"),
                )
                return result
            result["engine"] = "running"
            names = listed.stdout.strip().splitlines()
            if not names:
                result["frpc"]["status"] = "missing"
                result.update(
                    status="degraded",
                    message="Docker 已连接，但未找到 frpc 容器，回调隧道需要检查。",
                )
                return result
            if names != ["frpc"]:
                raise ValueError("unexpected container listing")
            inspected = self._run(executable, [
                "container", "inspect", "frpc", "--format", _STATE_FORMAT,
            ])
            if inspected.returncode:
                result["message"] = "Docker 已连接，但 frpc 状态暂时无法读取，请刷新重试。"
                return result
            state = json.loads(inspected.stdout)
            if not isinstance(state, dict) or state.get("status") not in CONTAINER_STATES:
                raise ValueError("unrecognised container state")
            health = state.get("health")
            if health is not None and health not in HEALTH_STATES:
                raise ValueError("unrecognised health state")
            exit_code = state.get("exit_code")
            result["frpc"].update(
                status=state["status"], health=health,
                started_at=_started_at(state.get("started_at")),
                exit_code=exit_code if type(exit_code) is int else None,
            )
            if state["status"] != "running":
                result.update(
                    status="degraded", message=("frpc 容器未运行，回调隧道需要检查。"
                                                "请在 Docker Desktop 中查看该容器。"),
                )
            elif health == "unhealthy":
                result.update(
                    status="degraded",
                    message="frpc 容器健康检查未通过，请在 Docker Desktop 中查看容器日志。",
                )
            elif health == "starting":
                result["message"] = "frpc 容器运行中，健康检查正在启动，请稍后刷新。"
            else:
                result.update(status="healthy", message="Docker 引擎已连接，frpc 容器运行正常。")
        except (OSError, subprocess.SubprocessError):
            result.update(
                status="degraded",
                message="Docker 检测超时或无法执行，请确认 Docker Desktop 已启动及服务账户权限。",
            )
            if result["engine"] == "unknown":
                result["engine"] = "unavailable"
        except (ValueError, TypeError):
            result["message"] = "Docker 状态响应无法识别，当前状态尚未确认，请刷新重试。"
        return result
