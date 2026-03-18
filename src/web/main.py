"""Web viewer for Telegram Backup.

FastAPI application providing a web interface to browse backed-up messages.
v3.0: Async database operations with SQLAlchemy.
v5.0: WebSocket support for real-time updates and notifications.
v12.0: Modular APIRouter architecture.
"""

import asyncio
import json
import logging
import mimetypes
import os
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from ..config import Config
from ..db import DatabaseAdapter, close_database, get_db_manager, init_database
from ..realtime import RealtimeListener

if TYPE_CHECKING:
    from .push import PushNotificationManager

# Register MIME types for media files
mimetypes.add_type("audio/ogg", ".ogg")
mimetypes.add_type("audio/opus", ".opus")
mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/wav", ".wav")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/x-m4a", ".m4a")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("image/webp", ".webp")

# Configure logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize config
config = Config()

# Import shared state and dependencies
from .dependencies import (
    AUTH_ENABLED,
    AUTH_SESSION_SECONDS,
    ConnectionManager,
    ListenerManager,
    SessionData,
    _sessions,
    set_app_state,
    set_push_manager,
    set_realtime_listener,
)

# Create global instances
ws_manager = ConnectionManager()
listener_manager = ListenerManager(config)

# Global database adapter (initialized on startup)
db: DatabaseAdapter | None = None

# Background tasks
stats_task: asyncio.Task | None = None
_session_cleanup_task: asyncio.Task | None = None
_fts_task: asyncio.Task | None = None
_post_backup_task: asyncio.Task | None = None

# Real-time listener (PostgreSQL LISTEN/NOTIFY)
realtime_listener: RealtimeListener | None = None

# Push notification manager (Web Push API)
push_manager: "PushNotificationManager | None" = None

# Media root path
_media_root = Path(config.media_path).resolve() if os.path.exists(config.media_path) else None


# ---------------------------------------------------------------------------
# Helpers kept in main.py (used only during lifespan / background tasks)
# ---------------------------------------------------------------------------


async def _normalize_display_chat_ids():
    """Normalize DISPLAY_CHAT_IDS to use marked format.

    If a positive ID doesn't exist in DB but -100{id} does, auto-correct it.
    """
    if not config.display_chat_ids or not db:
        return

    all_chats = await db.get_all_chats()
    existing_ids = {c["id"] for c in all_chats}

    normalized = set()
    for chat_id in config.display_chat_ids:
        if chat_id in existing_ids:
            normalized.add(chat_id)
        elif chat_id > 0:
            marked_id = -1000000000000 - chat_id
            if marked_id in existing_ids:
                logger.warning(
                    f"DISPLAY_CHAT_IDS: Auto-correcting {chat_id} -> {marked_id} "
                    f"(use marked format for channels/supergroups)"
                )
                normalized.add(marked_id)
            else:
                logger.warning(f"DISPLAY_CHAT_IDS: Chat ID {chat_id} not found in database")
                normalized.add(chat_id)
        else:
            logger.warning(f"DISPLAY_CHAT_IDS: Chat ID {chat_id} not found in database")
            normalized.add(chat_id)

    config.display_chat_ids = normalized


# AI config defaults (imported from routes_ai for seeding)
from .routes_ai import get_ai_config_defaults


async def _seed_ai_config_defaults():
    """Seed default AI config values into app_settings if not already set."""
    existing = await db.get_all_settings()
    seeded = 0
    migrated = 0
    for key, default_value in get_ai_config_defaults().items():
        if key not in existing:
            await db.set_setting(key, default_value)
            seeded += 1
        elif key.endswith(".api_url") or key.endswith(".fallback_url"):
            current = existing[key]
            if current and "localhost:11434" in current:
                fixed = current.replace("localhost:11434", "host.docker.internal:11434")
                await db.set_setting(key, fixed)
                migrated += 1
    if seeded:
        logger.info(f"Seeded {seeded} default AI config values")
    if migrated:
        logger.info(f"Migrated {migrated} AI config URLs from localhost to host.docker.internal")


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------


async def session_cleanup_task():
    """Periodically evict expired sessions and stale rate limit entries."""
    from .dependencies import _login_attempts, _SESSION_CLEANUP_INTERVAL

    while True:
        try:
            await asyncio.sleep(_SESSION_CLEANUP_INTERVAL)
            now = time.time()
            expired = [k for k, v in _sessions.items() if now - v.created_at > AUTH_SESSION_SECONDS]
            for k in expired:
                _sessions.pop(k, None)
            if expired:
                logger.info(f"Cleaned up {len(expired)} expired sessions from cache")
            if db:
                try:
                    db_cleaned = await db.cleanup_expired_sessions(AUTH_SESSION_SECONDS)
                    if db_cleaned:
                        logger.info(f"Cleaned up {db_cleaned} expired sessions from database")
                except Exception as e:
                    logger.warning(f"DB session cleanup failed: {e}")
            stale_ips = [ip for ip, ts in _login_attempts.items() if all(now - t > 300 for t in ts)]
            for ip in stale_ips:
                _login_attempts.pop(ip, None)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Session cleanup error: {e}")


async def stats_calculation_scheduler():
    """Background task that runs stats calculation daily at configured hour."""
    while True:
        try:
            tz = ZoneInfo(config.viewer_timezone)
            now = datetime.now(tz)

            target_hour = config.stats_calculation_hour
            next_run = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)

            if now.hour >= target_hour:
                next_run = next_run.replace(day=now.day + 1)

            wait_seconds = (next_run - now).total_seconds()
            logger.info(
                f"Stats calculation scheduled for {next_run.strftime('%Y-%m-%d %H:%M')} ({wait_seconds / 3600:.1f}h from now)"
            )
            await asyncio.sleep(wait_seconds)

            logger.info("Running scheduled stats calculation...")
            await db.calculate_and_store_statistics()
            logger.info("Stats calculation completed")

        except asyncio.CancelledError:
            logger.info("Stats calculation scheduler cancelled")
            break
        except Exception as e:
            logger.error(f"Error in stats calculation scheduler: {e}")
            await asyncio.sleep(3600)


async def _fts_index_worker() -> None:
    """Background worker: build FTS5 index on startup only."""
    try:
        await db.init_fts()
        status = await db.get_fts_status()
        if status != "ready":
            await db.set_fts_status("building")
            total = await db.rebuild_fts_index()
            await db.set_fts_status("ready")
            logger.info("FTS index build complete: %d messages indexed", total)
        else:
            logger.info("FTS index ready")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("FTS index build failed: %s", e)
        try:
            await db.set_fts_status("error")
        except Exception:
            pass


async def _post_backup_watcher() -> None:
    """Watch for backup completion and periodically index new content."""
    last_seen = await db.get_metadata("last_backup_time") or ""
    logger.info("Post-backup watcher started (last_backup_time=%s)", last_seen[:19] if last_seen else "none")

    ticks = 0
    OCR_CATCHUP_TICKS = 30

    while True:
        await asyncio.sleep(60)
        ticks += 1
        try:
            current = await db.get_metadata("last_backup_time") or ""
            backup_changed = current and current != last_seen
            ocr_catchup = ticks % OCR_CATCHUP_TICKS == 0

            if backup_changed:
                last_seen = current
                logger.info("Backup completed — running post-backup tasks")

            if backup_changed or ocr_catchup:
                try:
                    added = await db.incremental_fts_index()
                    if added:
                        reason = "post-backup" if backup_changed else "OCR catch-up"
                        logger.info("FTS incremental (%s): %d new rows indexed", reason, added)
                except Exception as e:
                    logger.warning("FTS incremental failed: %s", e)

            if backup_changed and _media_root:
                try:
                    from .thumbnails import pregenerate_video_thumbnails
                    count = await pregenerate_video_thumbnails(_media_root, size=400, max_items=200)
                    if count:
                        logger.info("Post-backup thumbnails: %d generated", count)
                except Exception as e:
                    logger.warning("Post-backup thumbnails failed: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Post-backup watcher error: %s", e)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage application lifecycle - initialize and cleanup database."""
    global db, stats_task, _session_cleanup_task, _fts_task, _post_backup_task
    global realtime_listener, push_manager

    logger.info("Initializing database connection...")
    db_manager = await init_database()
    db = DatabaseAdapter(db_manager)
    logger.info("Database connection established")

    # Inject shared state into dependencies module
    set_app_state(
        db_=db,
        config_=config,
        manager_=ws_manager,
        listener_mgr_=listener_manager,
        media_root_=_media_root,
    )

    # Normalize display chat IDs
    await _normalize_display_chat_ids()

    # Check if stats have ever been calculated
    stats_calculated_at = await db.get_metadata("stats_calculated_at")
    if not stats_calculated_at:
        logger.info("No cached stats found, running initial calculation...")
        try:
            await db.calculate_and_store_statistics()
        except Exception as e:
            logger.warning(f"Initial stats calculation failed: {e}")

    # Restore persistent sessions from database
    if AUTH_ENABLED:
        try:
            rows = await db.load_all_sessions()
            now = time.time()
            restored = 0
            for row in rows:
                if now - row["created_at"] > AUTH_SESSION_SECONDS:
                    continue
                allowed = None
                if row["allowed_chat_ids"]:
                    try:
                        allowed = set(json.loads(row["allowed_chat_ids"]))
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(f"Skipping session with corrupted allowed_chat_ids for {row['username']}")
                        continue
                _sessions[row["token"]] = SessionData(
                    username=row["username"],
                    role=row["role"],
                    allowed_chat_ids=allowed,
                    no_download=bool(row.get("no_download", 0)),
                    source_token_id=row.get("source_token_id"),
                    created_at=row["created_at"],
                    last_accessed=row["last_accessed"],
                )
                restored += 1
            if restored:
                logger.info(f"Restored {restored} sessions from database")
        except Exception as e:
            logger.warning(f"Failed to restore sessions from database: {e}")

    # Start background tasks
    stats_task = asyncio.create_task(stats_calculation_scheduler())
    _session_cleanup_task = asyncio.create_task(session_cleanup_task())
    logger.info(
        f"Stats calculation scheduler started (runs daily at {config.stats_calculation_hour}:00 {config.viewer_timezone})"
    )

    # Start real-time listener
    db_manager_instance = await get_db_manager()
    from .routes_websocket import handle_realtime_notification
    realtime_listener = RealtimeListener(db_manager_instance, callback=handle_realtime_notification)
    await realtime_listener.init()
    await realtime_listener.start()
    set_realtime_listener(realtime_listener)
    logger.info("Real-time listener started (auto-detected database type)")

    # Initialize Web Push notifications
    if config.push_notifications == "full":
        from .push import PushNotificationManager
        push_manager = PushNotificationManager(db, config)
        push_enabled = await push_manager.initialize()
        set_push_manager(push_manager)
        if push_enabled:
            logger.info("Web Push notifications enabled (PUSH_NOTIFICATIONS=full)")
        else:
            logger.warning("Web Push notifications failed to initialize")
    else:
        logger.info(f"Push notifications mode: {config.push_notifications}")

    # Start FTS5 index worker
    _fts_task = asyncio.create_task(_fts_index_worker())
    _post_backup_task = asyncio.create_task(_post_backup_watcher())

    # Seed default AI configuration
    await _seed_ai_config_defaults()

    # Start background OCR worker
    from ..ocr_worker import OcrWorker
    ocr_worker = OcrWorker(db, config)
    app.state.ocr_worker = ocr_worker
    await ocr_worker.start()

    # Start background voice transcription worker
    from ..transcription_worker import TranscriptionWorker
    transcription_worker = TranscriptionWorker(db, config)
    app.state.transcription_worker = transcription_worker
    await transcription_worker.start()

    yield

    # Shutdown
    if hasattr(app.state, "transcription_worker") and app.state.transcription_worker:
        await app.state.transcription_worker.stop()

    if hasattr(app.state, "ocr_worker") and app.state.ocr_worker:
        await app.state.ocr_worker.stop()

    await listener_manager.shutdown()

    if realtime_listener:
        await realtime_listener.stop()

    for task in [stats_task, _session_cleanup_task, _fts_task, _post_backup_task]:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    logger.info("Closing database connection...")
    await close_database()
    logger.info("Database connection closed")


# ---------------------------------------------------------------------------
# App instance
# ---------------------------------------------------------------------------

app = FastAPI(title="Telegram Archive", lifespan=lifespan)

# CORS
_cors_origins_raw = os.getenv("CORS_ORIGINS", "*").strip()
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
_cors_allow_credentials = _cors_origins != ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://unpkg.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self' ws: wss:; "
        "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com"
    )
    return response


# Static files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ---------------------------------------------------------------------------
# Include routers
# ---------------------------------------------------------------------------

from .routes_auth import router as auth_router
from .routes_chat import router as chat_router
from .routes_media import router as media_router
from .routes_admin import router as admin_router
from .routes_ai import router as ai_router
from .routes_websocket import router as ws_router

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(media_router)
app.include_router(admin_router)
app.include_router(ai_router)
app.include_router(ws_router)

# ---------------------------------------------------------------------------
# Re-export broadcast functions for external callers
# ---------------------------------------------------------------------------

from .routes_websocket import (  # noqa: F401, E402
    broadcast_message_delete,
    broadcast_message_edit,
    broadcast_new_message,
)
