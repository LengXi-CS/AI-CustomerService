"""Authenticated admin APIs, kept separate from the public WeChat endpoints."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fastgpt_assistant.core.admin_auth import COOKIE_NAME, SESSION_TTL, AdminAuth
from fastgpt_assistant.core.admin_network import lan_http_allowed
from fastgpt_assistant.core.aidnet_status import AidNetStatus
from fastgpt_assistant.core.chat_archive_reader import (
    MAX_ID,
    ChatArchiveReader,
    ChatArchiveUnavailable,
    ChatMessagesRequest,
    ChatRoomsRequest,
)
from fastgpt_assistant.core.config import Settings
from fastgpt_assistant.core.config_journal import BACKUP_PATTERN, ConfigJournal, HistoryUnavailable
from fastgpt_assistant.core.config_recovery import (
    BackupSelection,
    BackupTarget,
    BackupUnavailable,
    ConfigRecovery,
    RestoreBackupRequest,
)
from fastgpt_assistant.core.connection_config import (
    ConnectionConfigStore,
    SaveConnectionsRequest,
    ServiceName,
    probe_connection,
)
from fastgpt_assistant.core.docker_status import DockerStatus
from fastgpt_assistant.core.dotenv_store import EnvironmentDocument
from fastgpt_assistant.core.group_config_store import (
    ConfigConflict,
    GroupConfigStore,
    InvalidGroupFile,
    SaveGroupsRequest,
    SaveSootheRequest,
)
from fastgpt_assistant.core.reply_policy_store import (
    InvalidReplyFile,
    ReplyPolicyStore,
    SaveRuntimeReplyRequest,
)
from fastgpt_assistant.core.runtime_status import RuntimeStatus, pending_configuration

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "admin_static"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=256)


def install_admin(
    app: FastAPI,
    *,
    groups_path: Path,
    auth_path: Path,
    backup_dir: Path,
    env_path: Path = Path(".env"),
    runtime_settings: Settings | None = None,
) -> None:
    auth = AdminAuth(auth_path)
    journal = ConfigJournal(auth_path.parent / "history.db")
    store = GroupConfigStore(groups_path, backup_dir, journal=journal)
    document = EnvironmentDocument(env_path, auth_path.parent / "reply-backups", journal=journal)
    reply_store = ReplyPolicyStore(
        env_path, document.backup_dir, runtime_settings=runtime_settings, document=document
    )
    connection_store = ConnectionConfigStore(
        reply_store.document, runtime_settings=runtime_settings
    )
    app.state.runtime_status = RuntimeStatus()
    app.state.docker_status = DockerStatus()
    app.state.aidnet_status = AidNetStatus()
    recovery = ConfigRecovery(store, reply_store)
    chats = ChatArchiveReader(runtime_settings)
    router = APIRouter(prefix="/api/admin", tags=["admin"])

    @app.exception_handler(ChatArchiveUnavailable)
    async def chat_archive_error(request: Request, exc: ChatArchiveUnavailable):
        return JSONResponse(
            {"detail": str(exc)}, status_code=503, headers={"Cache-Control": "no-store"}
        )

    @app.exception_handler(HistoryUnavailable)
    async def history_error(request: Request, exc: HistoryUnavailable):
        return JSONResponse(
            {"detail": str(exc)}, status_code=503, headers={"Cache-Control": "no-store"}
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api/admin/"):
            # FastAPI's default validation body contains submitted input, which
            # would echo a malformed password or a whole group list.
            return JSONResponse(
                {"detail": [{"loc": error["loc"], "msg": error["msg"]} for error in exc.errors()]},
                status_code=422,
            )
        return await request_validation_exception_handler(request, exc)

    def same_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        try:
            origin_host = urlsplit(origin).netloc if origin else None
        except ValueError as exc:
            raise HTTPException(403, "请求来源无效") from exc
        if origin and origin_host != request.url.netloc:
            raise HTTPException(403, "请从管理页面所在地址提交请求")

    def require_session(request: Request):
        token = request.cookies.get(COOKIE_NAME, "")
        session = auth.session(token)
        if session is None:
            raise HTTPException(401, "请先登录管理员账户")
        return session

    session_dependency = Depends(require_session)

    def require_write(request: Request, session=session_dependency) -> None:
        same_origin(request)
        if not secrets.compare_digest(
            request.headers.get("x-csrf-token", "").encode(), session.csrf_token.encode()
        ):
            raise HTTPException(403, "安全校验失败，请重新登录")

    @router.get("/session")
    def session_status(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        session = auth.session(request.cookies.get(COOKIE_NAME, ""))
        return {
            "configured": auth.configured,
            "authenticated": session is not None,
            "csrf_token": session.csrf_token if session else None,
        }

    @router.post("/login")
    def login(payload: LoginRequest, request: Request, response: Response):
        same_origin(request)
        trusted_lan = runtime_settings is not None and lan_http_allowed(
            request.url.hostname,
            request.client.host if request.client else None,
            runtime_settings.admin_lan_http_networks,
        )
        if request.url.scheme != "https" and not trusted_lan and request.url.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
            "testserver",
        }:
            raise HTTPException(403, "请使用 HTTPS、服务器本机或已开放的局域网地址登录")
        try:
            token, session = auth.login(
                payload.password, request.client.host if request.client else "unknown"
            )
        except TimeoutError as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "60"}) from exc
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                503, "请在服务器运行 scripts/setup_admin.py 配置管理员密码"
            ) from exc
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=SESSION_TTL,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/api/admin",
        )
        response.headers["Cache-Control"] = "no-store"
        return {"csrf_token": session.csrf_token}

    @router.post("/logout", dependencies=[Depends(require_write)])
    def logout(request: Request, response: Response):
        auth.logout(request.cookies.get(COOKIE_NAME, ""))
        response.delete_cookie(COOKIE_NAME, path="/api/admin")
        return {"ok": True}

    @router.get("/groups", dependencies=[Depends(require_session)])
    def get_groups(response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            return store.snapshot()
        except (InvalidGroupFile, ValidationError) as exc:
            raise HTTPException(422, "群策略文件无效，请在本机修复后重新加载") from exc
        except OSError as exc:
            raise HTTPException(503, "无法读取群策略文件，请检查服务账户权限") from exc

    @router.get("/status", dependencies=[Depends(require_session)])
    def get_runtime_status(response: Response):
        response.headers["Cache-Control"] = "no-store"
        result = app.state.runtime_status.snapshot()
        result["configuration"] = pending_configuration(reply_store, connection_store)
        result["docker"] = app.state.docker_status.snapshot()
        result["aidnet"] = app.state.aidnet_status.snapshot()
        if not result["configuration"]["available"] and result["service"]["status"] == "healthy":
            result["service"]["status"] = "degraded"
        return result

    @router.get("/history", dependencies=[Depends(require_session)])
    def get_history(
        response: Response,
        before: str | None = Query(default=None, pattern=r"^[a-f0-9]{32}$"),
        limit: int = Query(default=20, ge=1, le=50),
    ):
        response.headers["Cache-Control"] = "no-store"
        return journal.list(before=before, limit=limit)

    # Read-only POSTs keep customer search terms out of URLs/access logs and
    # reuse the same-origin/CSRF checks. They never write to the archive.
    @router.post("/chats/rooms", dependencies=[Depends(require_write)])
    def chat_rooms(payload: ChatRoomsRequest, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return chats.rooms(payload)

    @router.post("/chats/messages", dependencies=[Depends(require_write)])
    def chat_messages(payload: ChatMessagesRequest, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return chats.messages(payload)

    @router.get("/chats/messages/{row_id}/media", dependencies=[Depends(require_session)])
    def chat_media(row_id: int, download: bool = False):
        if not 1 <= row_id <= MAX_ID:
            raise HTTPException(404, "附件未留存或已不可用")
        asset = chats.media(row_id)
        if asset is None:
            raise HTTPException(404, "附件未留存或已不可用")
        return FileResponse(
            asset["path"],
            filename=asset["name"],
            media_type=asset["mime_type"],
            content_disposition_type="inline"
            if asset["inline_image"] and not download
            else "attachment",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    @router.get("/history/backups/{target}", dependencies=[Depends(require_session)])
    def get_backups(
        target: BackupTarget,
        response: Response,
        before: str | None = Query(default=None, pattern="^" + BACKUP_PATTERN + "$"),
        limit: int = Query(default=20, ge=1, le=50),
    ):
        response.headers["Cache-Control"] = "no-store"
        try:
            return recovery.backups(target, before=before, limit=limit)
        except OSError as exc:
            raise HTTPException(503, "无法读取备份目录，请检查服务账户权限。") from exc

    def recovery_error(exc: Exception):
        if isinstance(exc, ConfigConflict):
            return HTTPException(409, str(exc))
        if isinstance(exc, BackupUnavailable):
            return HTTPException(422, str(exc))
        if isinstance(exc, (InvalidGroupFile, InvalidReplyFile, ValidationError)):
            return HTTPException(422, "当前配置或备份格式无效，无法安全恢复；请在本机检查。")
        return HTTPException(503, "无法读取、备份或恢复配置，请检查服务账户权限。")

    @router.post("/history/backups/{target}/preview", dependencies=[Depends(require_write)])
    def preview_backup(target: BackupTarget, payload: BackupSelection, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            return recovery.preview(target, payload)
        except (
            ConfigConflict,
            BackupUnavailable,
            InvalidGroupFile,
            InvalidReplyFile,
            ValidationError,
            OSError,
        ) as exc:
            raise recovery_error(exc) from exc

    @router.post("/history/backups/{target}/restore", dependencies=[Depends(require_write)])
    def restore_backup(target: BackupTarget, payload: RestoreBackupRequest, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            return recovery.restore(target, payload)
        except (
            ConfigConflict,
            BackupUnavailable,
            InvalidGroupFile,
            InvalidReplyFile,
            ValidationError,
            OSError,
        ) as exc:
            raise recovery_error(exc) from exc

    @router.put("/groups", dependencies=[Depends(require_write)])
    def save_groups(payload: SaveGroupsRequest):
        try:
            result = store.save(payload)
        except ConfigConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except InvalidGroupFile as exc:
            raise HTTPException(422, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, "无法保存配置或备份，请检查服务账户权限") from exc
        logger.info(
            "Admin group configuration saved revision=%s changed=%s count=%d",
            result["revision"][:12],
            result["changed"],
            len(payload.groups),
        )
        return result

    @router.get("/reply-policy/soothe", dependencies=[Depends(require_session)])
    def get_soothe(response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            return store.soothe_snapshot()
        except (InvalidGroupFile, ValidationError) as exc:
            raise HTTPException(422, "安抚话术配置无效，请在本机修复后重新加载") from exc
        except OSError as exc:
            raise HTTPException(503, "无法读取安抚话术配置，请检查服务账户权限") from exc

    @router.put("/reply-policy/soothe", dependencies=[Depends(require_write)])
    def save_soothe(payload: SaveSootheRequest):
        try:
            result = store.save_soothe(payload)
        except ConfigConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except InvalidGroupFile as exc:
            raise HTTPException(422, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, "无法保存安抚话术，请检查配置和备份目录权限") from exc
        logger.info("Admin soothe policy saved revision=%s", result["revision"][:12])
        return result

    @router.get("/reply-policy/runtime", dependencies=[Depends(require_session)])
    def get_runtime_policy(response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            return reply_store.snapshot()
        except InvalidReplyFile as exc:
            raise HTTPException(422, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, "无法读取回复策略配置，请检查服务账户权限") from exc

    @router.put("/reply-policy/runtime", dependencies=[Depends(require_write)])
    def save_runtime_policy(payload: SaveRuntimeReplyRequest):
        try:
            result = reply_store.save(payload)
        except ConfigConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except InvalidReplyFile as exc:
            raise HTTPException(422, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, "无法保存回复策略，请检查配置和备份目录权限") from exc
        logger.info("Admin runtime reply policy saved revision=%s", result["revision"][:12])
        return result

    @router.get("/connections", dependencies=[Depends(require_session)])
    def get_connections(response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            return connection_store.snapshot()
        except InvalidReplyFile as exc:
            raise HTTPException(422, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, "无法读取连接配置，请检查服务账户权限") from exc

    @router.put("/connections", dependencies=[Depends(require_write)])
    def save_connections(payload: SaveConnectionsRequest):
        try:
            result = connection_store.save(payload)
        except ConfigConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except InvalidReplyFile as exc:
            raise HTTPException(422, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, "无法保存连接配置，请检查配置和备份目录权限") from exc
        logger.info("Admin connection configuration saved revision=%s", result["revision"][:12])
        return result

    @router.post("/connections/test/{service}", dependencies=[Depends(require_write)])
    def test_connection(service: ServiceName, payload: SaveConnectionsRequest, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            values = connection_store.probe_values(payload)
        except ConfigConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except InvalidReplyFile as exc:
            raise HTTPException(422, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, "无法读取连接配置，请检查服务账户权限") from exc
        return probe_connection(service, values)

    app.include_router(router)

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_index():
        index = STATIC_DIR / "index.html"
        if not index.is_file():
            return HTMLResponse("请先在 webui 目录执行 pnpm install 和 pnpm build。", 503)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    app.mount(
        "/admin/assets",
        StaticFiles(directory=STATIC_DIR / "assets", check_dir=False),
        name="admin-assets",
    )
