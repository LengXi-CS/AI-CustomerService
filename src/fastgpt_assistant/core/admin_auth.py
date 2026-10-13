"""Local administrator bootstrap and short-lived, in-memory browser sessions."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastgpt_assistant.core.group_config_store import atomic_write

SESSION_TTL = 8 * 60 * 60
COOKIE_NAME = "handsin_admin_session"


def password_digest(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32).hex()


def configure_admin(path: Path, password: str) -> None:
    if not 6 <= len(password) <= 256:
        raise ValueError("密码长度须为 6–256 个字符")
    salt = secrets.token_bytes(16)
    content = json.dumps({"salt": salt.hex(), "digest": password_digest(password, salt)})
    atomic_write(path, content.encode("utf-8"))


@dataclass(frozen=True)
class AdminSession:
    csrf_token: str
    expires_at: float
    credential_revision: str


class AdminAuth:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._sessions: dict[str, AdminSession] = {}
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.path.is_file()

    def _credentials(self) -> tuple[dict, str]:
        content = self.path.read_bytes()
        return json.loads(content), hashlib.sha256(content).hexdigest()

    def login(self, password: str, client_ip: str) -> tuple[str, AdminSession]:
        with self._lock:
            now = time.time()
            self._sessions = {
                key: value for key, value in self._sessions.items() if value.expires_at > now
            }
            self._failures = {
                key: [stamp for stamp in values if now - stamp < 60]
                for key, values in self._failures.items()
                if any(now - stamp < 60 for stamp in values)
            }
            failures = self._failures.setdefault(client_ip, [])
            if len(failures) >= 5:
                raise TimeoutError("尝试过于频繁，请 60 秒后重试")
            try:
                credential, revision = self._credentials()
                expected = credential["digest"]
                actual = password_digest(password, bytes.fromhex(credential["salt"]))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise FileNotFoundError("管理员账户尚未配置或凭证文件无效") from exc
            if not secrets.compare_digest(actual, expected):
                failures.append(now)
                raise PermissionError("密码错误")
            self._failures.pop(client_ip, None)
            token = secrets.token_urlsafe(32)
            session = AdminSession(secrets.token_urlsafe(32), now + SESSION_TTL, revision)
            self._sessions[token] = session
            return token, session

    def session(self, token: str) -> AdminSession | None:
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            try:
                _, revision = self._credentials()
            except (OSError, ValueError, KeyError):
                revision = ""
            if session.expires_at <= time.time() or revision != session.credential_revision:
                self._sessions.pop(token, None)
                return None
            return session

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)
