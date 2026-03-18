"""Authentication routes: login, logout, auth check, profiles, and token auth."""

import json
import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import dependencies as deps
from .dependencies import (
    AUTH_COOKIE_NAME,
    AUTH_ENABLED,
    AUTH_SESSION_SECONDS,
    UserContext,
    SessionData,
    _SA_USERNAME,
    _SA_PASSWORD,
    _check_rate_limit,
    _create_session,
    _get_secure_cookies,
    _has_role,
    _record_login_attempt,
    _resolve_session,
    _sessions,
    _verify_password,
    get_user_chat_ids,
    logger,
)

router = APIRouter()


@router.get("/api/profiles")
async def get_profiles():
    """Return backup profiles for the login page multi-instance selector.

    Priority: DB backup_profiles -> BACKUP_PROFILES env -> profiles.json -> auto-generated default.
    Always returns at least one profile so the selector is always visible.
    """
    # 1. DB-backed profiles (v11.0.0)
    if db:
        try:
            profiles = await deps.db.list_backup_profiles(active_only=True)
            if profiles:
                return {"profiles": profiles, "show_selector": True}
        except Exception:
            pass

    # 2. Env var profiles
    raw = os.getenv("BACKUP_PROFILES", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed:
                return {"profiles": parsed, "show_selector": True}
        except (json.JSONDecodeError, TypeError):
            pass

    # 3. File-based profiles
    profiles_file = Path(deps.config.backup_path) / "profiles.json"
    if profiles_file.exists():
        try:
            data = json.loads(profiles_file.read_text())
            profiles = data if isinstance(data, list) else data.get("profiles", [])
            if profiles:
                return {"profiles": profiles, "show_selector": True}
        except Exception:
            pass

    # 4. Auto-generate default profile from current instance
    default_profile = {
        "id": "default",
        "name": os.getenv("PROFILE_NAME", "Telegram Archive"),
        "description": os.getenv("PROFILE_DESC", ""),
        "icon": "database",
        "color": "#8774e1",
        "url": "/",
    }
    return {"profiles": [default_profile], "show_selector": True}


@router.get("/api/auth/check")
async def check_auth(auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)):
    """Check current authentication status. Returns role and username if authenticated."""
    if not AUTH_ENABLED:
        return {"authenticated": True, "auth_required": False, "role": "master", "username": "anonymous"}

    if not auth_cookie:
        return {"authenticated": False, "auth_required": True}

    session = await _resolve_session(auth_cookie)
    if not session:
        return {"authenticated": False, "auth_required": True}
    if time.time() - session.created_at > AUTH_SESSION_SECONDS:
        _sessions.pop(auth_cookie, None)
        return {"authenticated": False, "auth_required": True}

    return {
        "authenticated": True,
        "auth_required": True,
        "role": session.role,
        "username": session.username,
        "no_download": session.no_download,
        "is_super_admin": _has_role(session.role, "super_admin"),
        "is_admin": _has_role(session.role, "admin"),
    }


@router.post("/api/login")
async def login(request: Request):
    """Authenticate user (master via env vars or viewer via DB accounts)."""
    if not AUTH_ENABLED:
        return JSONResponse({"success": True, "message": "Auth disabled"})

    direct_ip = request.client.host if request.client else "unknown"
    _trusted = direct_ip.startswith(("172.", "10.", "192.168.", "127.")) or direct_ip in ("::1", "localhost")
    if _trusted:
        client_ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or request.headers.get("x-real-ip", "")
            or direct_ip
        )
    else:
        client_ip = direct_ip

    if not _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")

    try:
        data = await request.json()
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request")

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")

    _record_login_attempt(client_ip)
    user_agent = request.headers.get("user-agent", "")[:500]

    # 1. Check DB user accounts (super_admin / admin) first
    if db:
        user_acct = await deps.db.get_user_by_username(username)
        if user_acct and user_acct["is_active"]:
            if _verify_password(password, user_acct["salt"], user_acct["password_hash"]):
                acct_role = user_acct["role"]
                raw_pids = user_acct.get("allowed_profile_ids")
                profile_ids = None
                if raw_pids:
                    try:
                        profile_ids = json.loads(raw_pids) if isinstance(raw_pids, str) else raw_pids
                    except (json.JSONDecodeError, TypeError):
                        profile_ids = None
                token = await _create_session(username, acct_role, None, allowed_profile_ids=profile_ids)
                response = JSONResponse({"success": True, "role": acct_role, "username": username})
                response.set_cookie(
                    key=AUTH_COOKIE_NAME,
                    value=token,
                    httponly=True,
                    secure=_get_secure_cookies(request),
                    samesite="lax",
                    max_age=AUTH_SESSION_SECONDS,
                )
                if db:
                    await deps.db.create_audit_log(
                        username=username,
                        role=acct_role,
                        action="login_success",
                        endpoint="/api/login",
                        ip_address=client_ip,
                        user_agent=user_agent,
                    )
                return response

    # 2. Check DB viewer accounts
    if db:
        viewer = await deps.db.get_viewer_by_username(username)
        if viewer and viewer["is_active"]:
            if _verify_password(password, viewer["salt"], viewer["password_hash"]):
                allowed = None
                if viewer["allowed_chat_ids"]:
                    try:
                        allowed = set(json.loads(viewer["allowed_chat_ids"]))
                    except (json.JSONDecodeError, TypeError):
                        allowed = None

                viewer_no_download = bool(viewer.get("no_download", 0))
                token = await _create_session(username, "viewer", allowed, no_download=viewer_no_download)
                response = JSONResponse({"success": True, "role": "viewer", "username": username})
                response.set_cookie(
                    key=AUTH_COOKIE_NAME,
                    value=token,
                    httponly=True,
                    secure=_get_secure_cookies(request),
                    samesite="lax",
                    max_age=AUTH_SESSION_SECONDS,
                )

                if db:
                    await deps.db.create_audit_log(
                        username=username,
                        role="viewer",
                        action="login_success",
                        endpoint="/api/login",
                        ip_address=client_ip,
                        user_agent=user_agent,
                    )
                return response

    # 3. Fall back to env var credentials -> super_admin role
    viewer_only = request.headers.get("x-viewer-only", "").lower() == "true"
    if _SA_USERNAME and _SA_PASSWORD and secrets.compare_digest(username, _SA_USERNAME) and secrets.compare_digest(password, _SA_PASSWORD):
        if viewer_only:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        token = await _create_session(username, "super_admin", None)
        response = JSONResponse({"success": True, "role": "super_admin", "username": username})
        response.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=token,
            httponly=True,
            secure=_get_secure_cookies(request),
            samesite="lax",
            max_age=AUTH_SESSION_SECONDS,
        )

        if db:
            await deps.db.create_audit_log(
                username=username,
                role="super_admin",
                action="login_success",
                endpoint="/api/login",
                ip_address=client_ip,
                user_agent=user_agent,
            )
        return response

    # Failed login
    if db:
        await deps.db.create_audit_log(
            username=username or "(empty)",
            role="unknown",
            action="login_failed",
            endpoint="/api/login",
            ip_address=client_ip,
            user_agent=user_agent,
        )
    raise HTTPException(status_code=401, detail="Invalid credentials")


@router.post("/api/logout")
async def logout(
    request: Request,
    auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
):
    """Invalidate current session and clear cookie."""
    if auth_cookie:
        session = _sessions.pop(auth_cookie, None)
        if db:
            try:
                if not session:
                    row = await deps.db.get_session(auth_cookie)
                    if row:
                        session = SessionData(username=row["username"], role=row["role"])
                await deps.db.delete_session(auth_cookie)
            except Exception:
                pass
            if session:
                await deps.db.create_audit_log(
                    username=session.username,
                    role=session.role,
                    action="logout",
                    endpoint="/api/logout",
                    ip_address=request.client.host if request.client else None,
                )

    response = JSONResponse({"success": True})
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


@router.post("/auth/token")
async def auth_via_token(request: Request):
    """Authenticate using a share token. Creates a session scoped to the token's allowed chats."""
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")

    direct_ip = request.client.host if request.client else "unknown"
    _trusted = direct_ip.startswith(("172.", "10.", "192.168.", "127.")) or direct_ip in ("::1", "localhost")
    if _trusted:
        client_ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or request.headers.get("x-real-ip", "")
            or direct_ip
        )
    else:
        client_ip = direct_ip

    if not _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    try:
        data = await request.json()
        plaintext_token = data.get("token", "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request")

    if not plaintext_token:
        raise HTTPException(status_code=400, detail="Token required")

    _record_login_attempt(client_ip)

    token_record = await deps.db.verify_viewer_token(plaintext_token)
    if not token_record:
        await deps.db.create_audit_log(
            username="(token)",
            role="token",
            action="token_auth_failed",
            endpoint="/auth/token",
            ip_address=client_ip,
        )
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    allowed = None
    if token_record["allowed_chat_ids"]:
        try:
            allowed = set(json.loads(token_record["allowed_chat_ids"]))
        except (json.JSONDecodeError, TypeError):
            allowed = None

    token_no_download = bool(token_record.get("no_download", 0))
    token_label = token_record.get("label") or f"token:{token_record['id']}"
    session_token = await _create_session(
        username=f"token:{token_label}",
        role="token",
        allowed_chat_ids=allowed,
        no_download=token_no_download,
        source_token_id=token_record["id"],
    )

    response = JSONResponse(
        {
            "success": True,
            "role": "token",
            "username": f"token:{token_label}",
            "no_download": token_no_download,
        }
    )
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=_get_secure_cookies(request),
        samesite="lax",
        max_age=AUTH_SESSION_SECONDS,
    )

    await deps.db.create_audit_log(
        username=f"token:{token_label}",
        role="token",
        action="token_auth_success",
        endpoint="/auth/token",
        ip_address=client_ip,
    )

    return response
