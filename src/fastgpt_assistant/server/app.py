"""FastAPI Web server for FastGPT Assistant and WeChat GeWe Integration."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from fastgpt_assistant.core.config import settings
from fastgpt_assistant.core.models import Deliverable
from fastgpt_assistant.orchestration.conversation_scheduler import ConversationPoller
from fastgpt_assistant.orchestration.conversation_state import iso_timestamp
from fastgpt_assistant.orchestration.engine import AssistantEngine, InboundEvent, event_occurred_at
from fastgpt_assistant.server.admin import install_admin

logger = logging.getLogger("fastgpt_assistant")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


@asynccontextmanager
async def _customer_event_order(chat_id: str, sender_id: str):
    """Split texts concurrently, but commit them in customer arrival order."""
    pool = getattr(app.state, "customer_event_order", None)
    if pool is None:
        pool = app.state.customer_event_order = {}
    key = (chat_id, sender_id)
    previous = pool.get(key)
    finished = asyncio.get_running_loop().create_future()
    pool[key] = finished

    def release(_=None):
        if not finished.done():
            finished.set_result(None)
        if pool.get(key) is finished:
            pool.pop(key, None)

    try:
        yield previous
    finally:
        # Cancelled requests cannot let successors bypass an earlier commit.
        if previous is not None and not previous.done():
            previous.add_done_callback(release)
        else:
            release()


def resolve_log_level(value: str | int | None) -> int:
    """Map a textual level name (e.g. ``"debug"``) onto a stdlib level.

    Unrecognised names fall back to ``INFO`` instead of raising, so a typo in
    ``LOG_LEVEL`` can never stop the service from booting.
    """
    if isinstance(value, bool):
        return logging.INFO
    if isinstance(value, int):
        return value
    if not value:
        return logging.INFO
    candidate = getattr(logging, str(value).strip().upper(), None)
    return candidate if isinstance(candidate, int) else logging.INFO


def configure_logging(level: str | int | None = None) -> int:
    """Attach a root handler so this package's own logs actually reach the console.

    uvicorn only configures its ``uvicorn.*`` loggers and never installs a root
    handler. Without one, every ``logger.info`` in this package falls through to
    ``logging.lastResort``, which silently discards anything below ``WARNING``.
    That is why the attachment dispatch diagnostics (``Dispatching attachment``,
    ``Deduplicated attachments N -> M``) never showed up in ``logs/5330.err.log``
    while investigating the 2026-09-18 GeWe ``postFile`` timeout, even though the
    code logging them ran correctly.

    Idempotent: an existing root handler is never duplicated, so this is safe to
    call on every startup (including uvicorn's ``--reload`` child).

    Returns the numeric level that was applied.
    """
    resolved = resolve_log_level(level if level is not None else settings.log_level)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=resolved, format=LOG_FORMAT)
    root.setLevel(resolved)
    # Level is set on the package logger, not on uvicorn's, so ``uvicorn.*`` output
    # keeps its own handlers/format. Child loggers inherit this effective level.
    logging.getLogger("fastgpt_assistant").setLevel(resolved)
    return resolved


_engine: AssistantEngine | None = None
_poller_task: asyncio.Task | None = None
_poller_stop: asyncio.Event | None = None


def get_engine() -> AssistantEngine:
    global _engine
    if _engine is None:
        _engine = AssistantEngine(settings=settings)
    return _engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler initializing engine resources."""
    applied_level = configure_logging()
    runtime_status = app.state.runtime_status
    runtime_status.start(settings)
    global _poller_task, _poller_stop
    try:
        logger.info(
            "Initializing FastGPT Enterprise Assistant... (log level=%s)",
            logging.getLevelName(applied_level),
        )
        engine = get_engine()
        engine.conversation_state.issues.recover_interrupted()
        await asyncio.to_thread(engine.recover_pending_customer_events)
        if not engine.settings.db_path.exists():
            logger.warning("Database %s does not exist yet", engine.settings.db_path)
        _poller_stop = asyncio.Event()
        poller = ConversationPoller(
            engine.conversation_state,
            engine.process_conversation_task,
            quiet_window_seconds=engine.settings.conversation_quiet_window_seconds,
            poll_interval_seconds=engine.settings.conversation_poll_interval_seconds,
            reconciler=engine.reconcile_customer_issues,
            quiet_windows_provider=engine.conversation_quiet_windows,
        )
        _poller_task = asyncio.create_task(poller.run(_poller_stop))
        runtime_status.ready(_poller_task)
        yield
    finally:
        try:
            if _poller_stop:
                _poller_stop.set()
            if _poller_task:
                await _poller_task
        finally:
            _poller_task = None
            _poller_stop = None
            logger.info("Shutting down FastGPT Enterprise Assistant...")
            runtime_status.stop()


app = FastAPI(
    title="FastGPT Enterprise Assistant",
    version="0.1.0",
    lifespan=lifespan,
)

install_admin(
    app,
    groups_path=settings.wechat_groups_config_path,
    auth_path=settings.admin_auth_path,
    backup_dir=settings.admin_auth_path.parent / "group-backups",
    runtime_settings=settings,
)


class DirectQueryRequest(BaseModel):
    query: str = Field(..., description="User question or query text")
    user_id: str = Field(default="test_user", description="Simulated user or WeChat ID")
    image_context: str = Field(default="", description="Optional image/barcode context")


class DirectQueryResponse(BaseModel):
    answer: str
    channel: str
    deliverables: list[dict[str, Any]] = Field(default_factory=list)


@app.get("/health")
def health_check() -> dict[str, Any]:
    """Health status check."""
    engine = get_engine()
    db_ok = engine.settings.db_path.exists()
    chat_log = getattr(engine, "chat_log", None)
    return {
        "status": "healthy" if db_ok else "degraded",
        "database": {
            "path": str(engine.settings.db_path),
            "exists": db_ok,
        },
        "chat_log": {
            "enabled": bool(chat_log is not None and chat_log.enabled),
            "path": str(engine.settings.chat_log_db_path),
            "exists": engine.settings.chat_log_db_path.exists(),
        },
        "media_store": {
            "dir": str(engine.settings.media_dir),
            "exists": engine.settings.media_dir.exists(),
        },
        "fastgpt": {
            "api_url": engine.settings.fastgpt_api_url,
            "dataset_configured": bool(engine.settings.fastgpt_dataset_id),
        },
        "llm": {
            "configured": bool(engine.settings.llm_api_key),
            "model": engine.settings.llm_model,
            "vision_model": engine.settings.llm_vision_model or engine.settings.llm_model,
        },
        "wechat": {
            "gewe_configured": bool(engine.settings.gewe_token and engine.settings.gewe_app_id),
        },
    }


@app.post("/api/wechat/callback")
async def wechat_callback(request: Request) -> dict[str, Any]:
    """Handle incoming GeWe webhook callback."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    engine = get_engine()
    engine.observe_public_origin(
        host=request.headers.get("x-forwarded-host") or request.headers.get("host", ""),
        proto=request.headers.get("x-forwarded-proto") or request.url.scheme,
    )
    logger.info(
        "WeChat callback headers: host=%s, x-forwarded-host=%s, current_media_base_url=%s",
        request.headers.get("host"),
        request.headers.get("x-forwarded-host"),
        engine.query_executor.media_base_url,
    )
    event = engine.parse_gewe_callback(body)
    if not event:
        return {"ret": 200, "msg": "ignored"}
    # Capture arrival once, before segmentation/download can complete out of
    # order. Archive, context and durable media binding share this timestamp.
    event = replace(event, raw_payload={
        **event.raw_payload, "received_at": iso_timestamp(event_occurred_at(event)),
    })

    # GeWe sends revocations and other group system notices as AddMsg. They
    # are not customer requests. A revocation also cancels its queued work.
    if event.msg_type >= 10000:
        if event.is_group and event.msg_type == 10002:
            engine.revoke_group_message(event)
        return {"ret": 200, "msg": "ignored", "reason": "system_message"}

    # Archive before routing, so a message the group policy then drops (客服没 @
    # 机器人、客户没按要求的 @) still shows up in the group's history. Only
    # configured groups are written; the call is best-effort and never raises.
    engine.log_group_inbound(event)
    if event.is_group and engine.groups_config.get(event.chat_id) is not None:
        engine.record_group_context_event(event)

    route = engine.route_group_event(event)
    if not route.allowed:
        if event.is_group and event.text and route.reason == "staff_not_mentioned":
            engine.record_conversation_event(event)
        logger.info(
            "Ignored WeChat event chat=%s sender_role=%s reason=%s",
            event.chat_id,
            event.sender_role,
            route.reason,
        )
        return {"ret": 200, "msg": "ignored", "reason": route.reason}

    logger.info(
        "Received WeChat event chat=%s sender=%s role=%s mentioned_bot=%s type=%d",
        event.chat_id,
        event.sender_id,
        event.sender_role,
        event.mentioned_bot,
        event.msg_type,
    )

    if event.is_image:
        engine.record_group_context_event(event)
        engine.process_image(event)
        return {"ret": 200, "msg": "image_buffered"}

    if event.text:
        if event.is_group and event.sender_role == "customer":
            # Decide semantic demand boundaries before creating durable tasks.
            # Run the bounded model call off the event loop so other callbacks
            # and the conversation poller can keep moving.
            async with _customer_event_order(event.chat_id, event.sender_id) as previous:
                prepared = engine.prepare_customer_event(event)
                if not prepared.accepted:
                    return {"ret": 200, "msg": "ignored", "reason": prepared.reason}
                try:
                    demands = await asyncio.to_thread(engine.split_customer_demands, event.text)
                    if previous is not None:
                        await asyncio.shield(previous)
                    receipt = engine.complete_customer_event(
                        event, event_id=prepared.event_id, demands=demands,
                    )
                    if not receipt.accepted:
                        return {"ret": 200, "msg": "ignored", "reason": receipt.reason}
                    return {
                        "ret": 200,
                        "msg": "queued",
                        "task_id": receipt.task_id,
                        "task_ids": list(receipt.task_ids),
                    }
                finally:
                    # Cancelled callbacks retain their encrypted original; the
                    # poller recovers it once no live callback owns the event.
                    engine.release_customer_event(prepared.event_id)

        staff_task_id = ""
        if event.is_group and event.sender_role == "staff":
            receipt = engine.record_conversation_event(event)
            if receipt.duplicate:
                return {"ret": 200, "msg": "ignored", "reason": "duplicate_event"}
            staff_task_id = receipt.task_id or ""
            if staff_task_id:
                event = replace(event, conversation_task_id=staff_task_id)
                engine.conversation_state.set_state(staff_task_id, "processing")
            else:
                # An explicit staff @bot question is an immediate request even
                # when there is no customer task to hand back. Keep any
                # ambiguous human-active tasks untouched and answer this text.
                logger.info(
                    "Handling staff @bot question without a linked task chat=%s",
                    event.chat_id,
                )
        reply = engine.process_text_query(event)
        if not route.shadow:
            # Blocking GeWe sends and the inter-attachment pause run off the event
            # loop so the conversation poller keeps making progress.
            outcome = await asyncio.to_thread(
                engine.dispatch_outbound,
                to_wxid=event.chat_id,
                reply=reply,
            )
            engine.log_group_outbound(
                to_wxid=event.chat_id,
                reply=reply,
                task_id=staff_task_id,
                outcome=outcome,
            )
            if staff_task_id:
                engine.conversation_state.set_state(staff_task_id, "responded")
            msg = "replied"
        else:
            msg = "shadow_processed"
        return {"ret": 200, "msg": msg, "channel": reply.channel_used}

    return {"ret": 200, "msg": "ok"}


@app.post("/api/v1/query", response_model=DirectQueryResponse)
def direct_query(req: DirectQueryRequest) -> DirectQueryResponse:
    """Direct query endpoint for testing and API integration."""
    engine = get_engine()
    event = InboundEvent(
        message_id="direct_req",
        from_user=req.user_id,
        to_user="assistant",
        chat_id=req.user_id,
        user_id=req.user_id,
        msg_type=1,
        text=req.query,
    )
    reply = engine.process_text_query(event)
    deliverables_out: list[dict[str, Any]] = []
    for d in reply.deliverables:
        if isinstance(d, Deliverable):
            deliverables_out.append(
                {
                    "media_id": d.media_id,
                    "file_name": d.filename,
                    "file_path": d.download_url,
                    "media_type": d.mime_type,
                }
            )
        elif isinstance(d, dict):
            deliverables_out.append(d)

    return DirectQueryResponse(
        answer=reply.text,
        channel=reply.channel_used,
        deliverables=deliverables_out,
    )


@app.get("/v1/media/{media_id}")
def get_media_file(media_id: str):
    """Stream media file safely by media_id."""
    engine = get_engine()
    raw_media_id = media_id.rsplit(".", 1)[0] if "." in media_id else media_id
    with engine.read_model.connection() as connection:
        row = connection.execute(
            "SELECT archive_file, status FROM attachments WHERE media_id = ?",
            (raw_media_id,),
        ).fetchone()
    if not row or row["status"] != "available" or not row["archive_file"]:
        raise HTTPException(status_code=404, detail="Media not found")

    project_root = engine.settings.db_path.resolve().parent.parent
    data_root = engine.settings.db_path.resolve().parent
    archive_path = Path(str(row["archive_file"]))
    candidates = [project_root / archive_path, data_root / archive_path]
    file_path = next(
        (candidate.resolve() for candidate in candidates if candidate.is_file()),
        None,
    )
    if file_path is None:
        raise HTTPException(status_code=404, detail="Media not found")
    try:
        file_path.relative_to(project_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Media not found") from exc

    # Record the exact on-disk size for every fetch. GeWe's download window is the
    # only unknown left in the attachment pipeline (measured tolerance: >= 1.09 MB /
    # ~11.6 s over a ~90 KB/s tunnel), so a future timeout must be explicable from
    # this line alone instead of being inferred from a filename after the fact.
    logger.info(
        "Serving media media_id=%s size=%d bytes name=%s",
        raw_media_id,
        file_path.stat().st_size,
        file_path.name,
    )

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
    )
