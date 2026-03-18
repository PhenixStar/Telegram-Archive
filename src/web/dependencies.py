"""Shared state, authentication, and dependency injection for the web viewer.

Contains connection managers, session handling, auth dependencies, and
module-level state variables that are initialised during app lifespan.
"""

import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import Cookie, Depends, HTTPException, Request

if TYPE_CHECKING:
    from ..config import Config
    from ..db import DatabaseAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state (set via set_app_state during lifespan)
# ---------------------------------------------------------------------------

db: "DatabaseAdapter | None" = None
config: "Config | None" = None
manager: "ConnectionManager | None" = None
listener_mgr = None  # ListenerManager instance
push_manager = None  # PushNotificationManager instance
realtime_listener = None  # RealtimeListener instance
_media_root = None  # Path | None


def set_app_state(
    db_: "DatabaseAdapter",
    config_: "Config",
    manager_: "ConnectionManager",
    listener_mgr_,
    *,
    push_manager_=None,
    realtime_listener_=None,
    media_root_=None,
) -> None:
    """Called from main.py lifespan to inject shared state without circular imports."""
    global db, config, manager, listener_mgr, push_manager, realtime_listener, _media_root
    db = db_
    config = config_
    manager = manager_
    listener_mgr = listener_mgr_
    push_manager = push_manager_
    realtime_listener = realtime_listener_
    _media_root = media_root_


def set_push_manager(pm) -> None:
    """Update push manager reference after initialisation."""
    global push_manager
    push_manager = pm


def set_realtime_listener(rl) -> None:
    """Update realtime listener reference after initialisation."""
    global realtime_listener
    realtime_listener = rl


# ---------------------------------------------------------------------------
# WebSocket Connection Manager
# ---------------------------------------------------------------------------

from fastapi import WebSocket


class ConnectionManager:
    """Manages WebSocket connections for real-time updates."""

    def __init__(self):
        self.active_connections: dict[WebSocket, set[int]] = {}
        self._allowed_chats: dict[WebSocket, set[int] | None] = {}

    async def connect(self, websocket: WebSocket, allowed_chat_ids: set[int] | None = None):
        await websocket.accept()
        self.active_connections[websocket] = set()
        self._allowed_chats[websocket] = allowed_chat_ids
        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.pop(websocket, None)
        self._allowed_chats.pop(websocket, None)
        logger.info(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")

    def subscribe(self, websocket: WebSocket, chat_id: int):
        """Subscribe a connection to updates for a specific chat."""
        if websocket in self.active_connections:
            allowed = self._allowed_chats.get(websocket)
            if allowed is not None and chat_id not in allowed:
                return
            self.active_connections[websocket].add(chat_id)

    def unsubscribe(self, websocket: WebSocket, chat_id: int):
        """Unsubscribe a connection from a specific chat."""
        if websocket in self.active_connections:
            self.active_connections[websocket].discard(chat_id)

    async def broadcast_to_chat(self, chat_id: int, message: dict):
        """Broadcast a message to all connections subscribed to a chat."""
        disconnected = []
        for websocket, subscribed_chats in self.active_connections.items():
            allowed = self._allowed_chats.get(websocket)
            if allowed is not None and chat_id not in allowed:
                continue
            if chat_id in subscribed_chats or not subscribed_chats:
                try:
                    await websocket.send_json(message)
                except Exception as e:
                    logger.warning(f"Failed to send to websocket: {e}")
                    disconnected.append(websocket)
        for ws in disconnected:
            self.disconnect(ws)

    async def broadcast_to_all(self, message: dict):
        """Broadcast a message to all connected clients."""
        disconnected = []
        for websocket in self.active_connections:
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.warning(f"Failed to send to websocket: {e}")
                disconnected.append(websocket)
        for ws in disconnected:
            self.disconnect(ws)


# ---------------------------------------------------------------------------
# Listener Manager
# ---------------------------------------------------------------------------

import asyncio


class ListenerManager:
    """Manages Telegram listener lifecycle based on viewer presence (LISTENER_MODE=auto).

    In viewer-only containers (no Telethon/listener module), reports config
    status without attempting to start the listener.
    """

    def __init__(self, cfg):
        self._config = cfg
        self._listener = None
        self._listener_task: asyncio.Task | None = None
        self._grace_task: asyncio.Task | None = None
        self._status = "stopped"
        self._lock = asyncio.Lock()
        try:
            from ..listener import TelegramListener  # noqa: F401
            self._listener_available = True
        except (ImportError, ModuleNotFoundError):
            self._listener_available = False

    @property
    def status(self) -> str:
        return self._status

    async def on_viewer_connect(self, viewer_count: int):
        if self._config.listener_mode != "auto":
            return
        if self._grace_task and not self._grace_task.done():
            self._grace_task.cancel()
            self._grace_task = None
            if self._status == "grace_period":
                self._status = "running"
                logger.info("[ListenerManager] Grace period cancelled - viewer reconnected")
            return
        if self._status == "stopped":
            await self._start()

    async def on_viewer_disconnect(self, viewer_count: int):
        if self._config.listener_mode != "auto":
            return
        if viewer_count == 0 and self._status == "running":
            self._status = "grace_period"
            grace = self._config.listener_grace_period
            logger.info(f"[ListenerManager] Last viewer disconnected, grace period: {grace}s")
            self._grace_task = asyncio.create_task(self._grace_then_stop(grace))

    async def _grace_then_stop(self, seconds: int):
        try:
            await asyncio.sleep(seconds)
            logger.info("[ListenerManager] Grace period expired, stopping listener")
            await self._stop()
        except asyncio.CancelledError:
            pass

    async def _start(self):
        if not self._listener_available:
            return
        async with self._lock:
            if self._status != "stopped":
                return
            self._status = "starting"
            try:
                from ..listener import TelegramListener
                self._listener = await TelegramListener.create(self._config)
                await self._listener.connect()
                self._listener_task = asyncio.create_task(self._run_listener())
                self._status = "running"
                logger.info("[ListenerManager] Listener started (auto mode)")
            except Exception:
                logger.exception("[ListenerManager] Failed to start listener")
                self._status = "stopped"

    async def _run_listener(self):
        try:
            await self._listener.run()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[ListenerManager] Listener error")
        finally:
            self._status = "stopped"

    async def _stop(self):
        async with self._lock:
            if self._status not in ("running", "grace_period"):
                return
            self._status = "stopping"
            if self._listener_task and not self._listener_task.done():
                self._listener_task.cancel()
                try:
                    await asyncio.wait_for(self._listener_task, timeout=10)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            if self._listener:
                try:
                    await self._listener.stop()
                except Exception:
                    pass
                self._listener = None
            self._status = "stopped"
            logger.info("[ListenerManager] Listener stopped")

    async def shutdown(self):
        if self._grace_task and not self._grace_task.done():
            self._grace_task.cancel()
        await self._stop()


# ---------------------------------------------------------------------------
# Authentication constants and state
# ---------------------------------------------------------------------------

SUPER_ADMIN_USERNAME = os.getenv("SUPER_ADMIN_USERNAME", "").strip()
SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD", "").strip()
VIEWER_USERNAME = os.getenv("VIEWER_USERNAME", "").strip()
VIEWER_PASSWORD = os.getenv("VIEWER_PASSWORD", "").strip()
_SA_USERNAME = SUPER_ADMIN_USERNAME or VIEWER_USERNAME
_SA_PASSWORD = SUPER_ADMIN_PASSWORD or VIEWER_PASSWORD
AUTH_ENABLED = bool(_SA_USERNAME and _SA_PASSWORD)
AUTH_COOKIE_NAME = "viewer_auth"

ROLE_HIERARCHY = {"super_admin": 4, "master": 3, "admin": 2, "viewer": 1, "token": 0}


def _has_role(user_role: str, required_role: str) -> bool:
    """Check if user_role meets or exceeds required_role in the hierarchy."""
    return ROLE_HIERARCHY.get(user_role, 0) >= ROLE_HIERARCHY.get(required_role, 0)


AUTH_SESSION_DAYS = int(os.getenv("AUTH_SESSION_DAYS", "30"))
AUTH_SESSION_SECONDS = AUTH_SESSION_DAYS * 24 * 60 * 60
_MAX_SESSIONS_PER_USER = 10
_SESSION_CLEANUP_INTERVAL = 900  # 15 minutes
_LOGIN_RATE_LIMIT = 15
_LOGIN_RATE_WINDOW = 300

if AUTH_ENABLED:
    logger.info(f"Authentication ENABLED (Super Admin: {_SA_USERNAME}, Session: {AUTH_SESSION_DAYS} days)")
else:
    logger.info("Authentication DISABLED (no SUPER_ADMIN_USERNAME/VIEWER_USERNAME set)")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class UserContext:
    username: str
    role: str
    allowed_chat_ids: set[int] | None = None
    no_download: bool = False
    allowed_profile_ids: list[str] | None = None


@dataclass
class SessionData:
    username: str
    role: str
    allowed_chat_ids: set[int] | None = None
    allowed_profile_ids: list[str] | None = None
    no_download: bool = False
    source_token_id: int | None = None
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)


_sessions: dict[str, SessionData] = {}
_login_attempts: dict[str, list[float]] = {}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000).hex()


def _verify_password(password: str, salt: str, password_hash: str) -> bool:
    return secrets.compare_digest(_hash_password(password, salt), password_hash)


def _check_rate_limit(ip: str) -> bool:
    """Returns True if the request is within rate limits."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < _LOGIN_RATE_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) < _LOGIN_RATE_LIMIT


def _record_login_attempt(ip: str) -> None:
    _login_attempts.setdefault(ip, []).append(time.time())


async def _create_session(
    username: str,
    role: str,
    allowed_chat_ids: set[int] | None = None,
    no_download: bool = False,
    source_token_id: int | None = None,
    allowed_profile_ids: list[str] | None = None,
) -> str:
    """Create a new session, evicting oldest if user exceeds max sessions."""
    user_sessions = [(k, v) for k, v in _sessions.items() if v.username == username]
    if len(user_sessions) >= _MAX_SESSIONS_PER_USER:
        user_sessions.sort(key=lambda x: x[1].created_at)
        for token, _ in user_sessions[: len(user_sessions) - _MAX_SESSIONS_PER_USER + 1]:
            _sessions.pop(token, None)
            if db:
                try:
                    await db.delete_session(token)
                except Exception:
                    pass

    now = time.time()
    token = secrets.token_urlsafe(32)
    _sessions[token] = SessionData(
        username=username,
        role=role,
        allowed_chat_ids=allowed_chat_ids,
        allowed_profile_ids=allowed_profile_ids,
        no_download=no_download,
        source_token_id=source_token_id,
        created_at=now,
        last_accessed=now,
    )

    if db:
        try:
            chat_ids_json = json.dumps(list(allowed_chat_ids)) if allowed_chat_ids is not None else None
            await db.save_session(
                token=token,
                username=username,
                role=role,
                allowed_chat_ids=chat_ids_json,
                created_at=now,
                last_accessed=now,
                no_download=1 if no_download else 0,
                source_token_id=source_token_id,
            )
        except Exception as e:
            logger.warning(f"Failed to persist session to database: {e}")

    return token


async def _invalidate_user_sessions(username: str) -> None:
    """Remove all sessions for a given username."""
    to_remove = [k for k, v in _sessions.items() if v.username == username]
    for k in to_remove:
        _sessions.pop(k, None)
    if db:
        try:
            await db.delete_user_sessions(username)
        except Exception as e:
            logger.warning(f"Failed to delete DB sessions for {username}: {e}")


async def _invalidate_token_sessions(token_id: int) -> None:
    """Remove all sessions created from a specific share token."""
    to_remove = [k for k, v in _sessions.items() if v.source_token_id == token_id]
    for k in to_remove:
        _sessions.pop(k, None)
    if db:
        try:
            await db.delete_sessions_by_source_token_id(token_id)
        except Exception as e:
            logger.warning(f"Failed to delete token sessions for token_id={token_id}: {e}")


def _get_secure_cookies(request: Request) -> bool:
    secure_env = os.getenv("SECURE_COOKIES", "").strip().lower()
    if secure_env == "true":
        return True
    if secure_env == "false":
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return forwarded_proto == "https" or str(request.url.scheme) == "https"


async def _resolve_session(auth_cookie: str) -> SessionData | None:
    """Look up session from in-memory cache, falling back to DB if needed."""
    session = _sessions.get(auth_cookie)
    if session:
        return session

    if not db:
        return None

    try:
        row = await db.get_session(auth_cookie)
    except Exception:
        return None

    if not row or time.time() - row["created_at"] > AUTH_SESSION_SECONDS:
        return None

    allowed = None
    if row["allowed_chat_ids"]:
        try:
            allowed = set(json.loads(row["allowed_chat_ids"]))
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Corrupted allowed_chat_ids for session {row['username']}, denying access")
            return None

    session = SessionData(
        username=row["username"],
        role=row["role"],
        allowed_chat_ids=allowed,
        no_download=bool(row.get("no_download", 0)),
        source_token_id=row.get("source_token_id"),
        created_at=row["created_at"],
        last_accessed=row["last_accessed"],
    )
    _sessions[auth_cookie] = session
    return session


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


async def require_auth(auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)) -> UserContext:
    """Dependency that enforces session-based auth. Returns UserContext."""
    if not AUTH_ENABLED:
        return UserContext(username="anonymous", role="master", allowed_chat_ids=None)

    if not auth_cookie:
        raise HTTPException(status_code=401, detail="Unauthorized")

    session = await _resolve_session(auth_cookie)
    if not session:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if time.time() - session.created_at > AUTH_SESSION_SECONDS:
        _sessions.pop(auth_cookie, None)
        raise HTTPException(status_code=401, detail="Session expired")

    session.last_accessed = time.time()
    return UserContext(
        username=session.username,
        role=session.role,
        allowed_chat_ids=session.allowed_chat_ids,
        allowed_profile_ids=session.allowed_profile_ids,
        no_download=session.no_download,
    )


def require_master(request: Request, user: UserContext = Depends(require_auth)) -> UserContext:
    """Dependency that requires master-level role (admin, master, or super_admin)."""
    if not _has_role(user.role, "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    if request.headers.get("x-viewer-only", "").lower() == "true":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_super_admin(user: UserContext = Depends(require_auth)) -> UserContext:
    """Dependency that requires super_admin role exclusively."""
    if not _has_role(user.role, "super_admin"):
        raise HTTPException(status_code=403, detail="Super admin access required")
    return user


def get_user_chat_ids(user: UserContext) -> set[int] | None:
    """Get the effective chat IDs a user can access.

    Returns None if the user can see all chats (no restriction).
    """
    master_filter = config.display_chat_ids or None

    if user.role == "master":
        return master_filter

    if user.allowed_chat_ids is None:
        return master_filter
    if master_filter is None:
        return user.allowed_chat_ids
    return user.allowed_chat_ids & master_filter
