"""Admin routes: viewer/token/profile/admin CRUD, settings, backup config, listener status."""

import hashlib
import json
import os
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import dependencies as deps
from .dependencies import (
    UserContext,
    _hash_password,
    _invalidate_token_sessions,
    _invalidate_user_sessions,
    _verify_password,
    get_user_chat_ids,
    logger,
    require_auth,
    require_master,
    require_super_admin,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Listener Status
# ---------------------------------------------------------------------------


@router.get("/api/admin/listener-status")
async def get_listener_status(user: UserContext = Depends(require_master)):
    """Return current Telegram listener mode, status, and viewer count."""
    status = deps.listener_mgr.status
    if not deps.listener_mgr._listener_available:
        status = "viewer-only"
    return {
        "mode": deps.config.listener_mode,
        "status": status,
        "grace_period": deps.config.listener_grace_period,
        "viewer_count": len(deps.manager.active_connections),
        "listener_available": deps.listener_mgr._listener_available,
    }


# ---------------------------------------------------------------------------
# Viewer Account CRUD
# ---------------------------------------------------------------------------


@router.get("/api/admin/viewers")
async def list_viewers(user: UserContext = Depends(require_master)):
    """List all viewer accounts."""
    viewers = await deps.db.get_all_viewer_accounts()
    safe = []
    for v in viewers:
        safe.append(
            {
                "id": v["id"],
                "username": v["username"],
                "allowed_chat_ids": json.loads(v["allowed_chat_ids"]) if v["allowed_chat_ids"] else None,
                "is_active": v["is_active"],
                "no_download": v.get("no_download", 0),
                "created_by": v["created_by"],
                "created_at": v["created_at"],
                "updated_at": v["updated_at"],
            }
        )
    return {"viewers": safe}


@router.post("/api/admin/viewers")
async def create_viewer(request: Request, user: UserContext = Depends(require_master)):
    """Create a new viewer account."""
    from .dependencies import AUTH_ENABLED, VIEWER_USERNAME

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    allowed_chat_ids = data.get("allowed_chat_ids")
    is_active = 1 if data.get("is_active", 1) else 0
    viewer_no_download = 1 if data.get("no_download", 0) else 0

    if not username or len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
    if not password or len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if AUTH_ENABLED and VIEWER_USERNAME and username.lower() == VIEWER_USERNAME.lower():
        raise HTTPException(status_code=409, detail="Username conflicts with master account")

    existing = await deps.db.get_viewer_by_username(username)
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")

    salt = secrets.token_hex(32)
    password_hash = _hash_password(password, salt)

    chat_ids_json = None
    if allowed_chat_ids is not None:
        try:
            chat_ids_json = json.dumps([int(cid) for cid in allowed_chat_ids])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid chat ID format")

    account = await deps.db.create_viewer_account(
        username=username,
        password_hash=password_hash,
        salt=salt,
        allowed_chat_ids=chat_ids_json,
        created_by=user.username,
        is_active=is_active,
        no_download=viewer_no_download,
    )

    await deps.db.create_audit_log(
        username=user.username,
        role="master",
        action="viewer_created",
        endpoint="/api/admin/viewers",
        ip_address=request.client.host if request.client else None,
    )

    return {
        "id": account["id"],
        "username": account["username"],
        "allowed_chat_ids": json.loads(chat_ids_json) if chat_ids_json else None,
        "is_active": account["is_active"],
        "no_download": account["no_download"],
    }


@router.put("/api/admin/viewers/{viewer_id}")
async def update_viewer(viewer_id: int, request: Request, user: UserContext = Depends(require_master)):
    """Update a viewer account. Invalidates their existing sessions."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    existing = await deps.db.get_viewer_account(viewer_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Viewer not found")

    updates = {}
    if "password" in data and data["password"]:
        pwd = data["password"].strip()
        if len(pwd) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        salt = secrets.token_hex(32)
        updates["password_hash"] = _hash_password(pwd, salt)
        updates["salt"] = salt

    if "allowed_chat_ids" in data:
        allowed = data["allowed_chat_ids"]
        if allowed is None:
            updates["allowed_chat_ids"] = None
        else:
            try:
                updates["allowed_chat_ids"] = json.dumps([int(cid) for cid in allowed])
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="Invalid chat ID format")

    if "is_active" in data:
        updates["is_active"] = 1 if data["is_active"] else 0

    if "no_download" in data:
        updates["no_download"] = 1 if data["no_download"] else 0

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    account = await deps.db.update_viewer_account(viewer_id, **updates)
    await _invalidate_user_sessions(existing["username"])

    await deps.db.create_audit_log(
        username=user.username,
        role="master",
        action=f"viewer_updated:{existing['username']}",
        endpoint=f"/api/admin/viewers/{viewer_id}",
        ip_address=request.client.host if request.client else None,
    )

    return {
        "id": account["id"],
        "username": account["username"],
        "allowed_chat_ids": json.loads(account["allowed_chat_ids"]) if account["allowed_chat_ids"] else None,
        "is_active": account["is_active"],
    }


@router.delete("/api/admin/viewers/{viewer_id}")
async def delete_viewer(viewer_id: int, request: Request, user: UserContext = Depends(require_master)):
    """Delete a viewer account and invalidate their sessions."""
    existing = await deps.db.get_viewer_account(viewer_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Viewer not found")

    await _invalidate_user_sessions(existing["username"])
    await deps.db.delete_viewer_account(viewer_id)

    await deps.db.create_audit_log(
        username=user.username,
        role="master",
        action=f"viewer_deleted:{existing['username']}",
        endpoint=f"/api/admin/viewers/{viewer_id}",
        ip_address=request.client.host if request.client else None,
    )

    return {"success": True}


@router.get("/api/admin/chats")
async def admin_list_chats(user: UserContext = Depends(require_master)):
    """List all chats for the admin chat picker."""
    chats = await deps.db.get_all_chats()
    result = []
    for c in chats:
        title = c.get("title")
        if not title:
            parts = [c.get("first_name", ""), c.get("last_name", "")]
            title = " ".join(p for p in parts if p) or c.get("username") or str(c["id"])
        result.append(
            {
                "id": c["id"],
                "title": title,
                "type": c.get("type"),
                "username": c.get("username"),
                "first_name": c.get("first_name"),
                "last_name": c.get("last_name"),
            }
        )
    return {"chats": result}


@router.get("/api/admin/audit")
async def get_audit_log(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    username: str | None = Query(None),
    action: str | None = Query(None),
    user: UserContext = Depends(require_master),
):
    """Get paginated audit log entries."""
    logs = await deps.db.get_audit_logs(limit=limit, offset=offset, username=username, action=action)
    return {"logs": logs, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# Share Token CRUD
# ---------------------------------------------------------------------------


@router.get("/api/admin/tokens")
async def list_tokens(user: UserContext = Depends(require_master)):
    """List all share tokens."""
    tokens = await deps.db.get_all_viewer_tokens()
    safe = []
    for t in tokens:
        safe.append(
            {
                "id": t["id"],
                "label": t["label"],
                "created_by": t["created_by"],
                "allowed_chat_ids": json.loads(t["allowed_chat_ids"]) if t["allowed_chat_ids"] else None,
                "is_revoked": t["is_revoked"],
                "no_download": t["no_download"],
                "expires_at": t["expires_at"],
                "last_used_at": t["last_used_at"],
                "use_count": t["use_count"],
                "created_at": t["created_at"],
            }
        )
    return {"tokens": safe}


@router.post("/api/admin/tokens")
async def create_token(request: Request, user: UserContext = Depends(require_master)):
    """Create a new share token. Returns the plaintext token only once."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    label = (data.get("label") or "").strip() or None
    allowed_chat_ids = data.get("allowed_chat_ids")
    no_download = 1 if data.get("no_download") else 0
    expires_at = None
    if data.get("expires_at"):
        try:
            expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid expires_at format. Use ISO 8601.")

    if not allowed_chat_ids or not isinstance(allowed_chat_ids, list):
        raise HTTPException(status_code=400, detail="allowed_chat_ids is required (list of chat IDs)")

    try:
        chat_ids_json = json.dumps([int(cid) for cid in allowed_chat_ids])
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid chat ID format")

    plaintext_token = secrets.token_hex(32)
    salt = secrets.token_hex(32)
    token_hash = hashlib.pbkdf2_hmac("sha256", plaintext_token.encode(), bytes.fromhex(salt), 600_000).hex()

    token_record = await deps.db.create_viewer_token(
        label=label,
        token_hash=token_hash,
        token_salt=salt,
        created_by=user.username,
        allowed_chat_ids=chat_ids_json,
        no_download=no_download,
        expires_at=expires_at,
    )

    await deps.db.create_audit_log(
        username=user.username,
        role="master",
        action=f"token_created:{token_record['id']}",
        endpoint="/api/admin/tokens",
        ip_address=request.client.host if request.client else None,
    )

    return {
        "id": token_record["id"],
        "label": token_record["label"],
        "token": plaintext_token,
        "allowed_chat_ids": json.loads(chat_ids_json),
        "no_download": token_record["no_download"],
        "expires_at": token_record["expires_at"],
        "created_at": token_record["created_at"],
    }


@router.put("/api/admin/tokens/{token_id}")
async def update_token(token_id: int, request: Request, user: UserContext = Depends(require_master)):
    """Update a share token."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    updates = {}
    if "label" in data:
        updates["label"] = (data["label"] or "").strip() or None
    if "allowed_chat_ids" in data:
        allowed = data["allowed_chat_ids"]
        if allowed is None or not isinstance(allowed, list):
            raise HTTPException(status_code=400, detail="allowed_chat_ids must be a list")
        try:
            updates["allowed_chat_ids"] = json.dumps([int(cid) for cid in allowed])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid chat ID format")
    if "is_revoked" in data:
        updates["is_revoked"] = 1 if data["is_revoked"] else 0
    if "no_download" in data:
        updates["no_download"] = 1 if data["no_download"] else 0

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    updated = await deps.db.update_viewer_token(token_id, **updates)
    if not updated:
        raise HTTPException(status_code=404, detail="Token not found")

    scope_changed = any(k in updates for k in ("is_revoked", "allowed_chat_ids", "no_download"))
    if scope_changed:
        await _invalidate_token_sessions(token_id)

    await deps.db.create_audit_log(
        username=user.username,
        role="master",
        action=f"token_updated:{token_id}",
        endpoint=f"/api/admin/tokens/{token_id}",
        ip_address=request.client.host if request.client else None,
    )

    return {
        "id": updated["id"],
        "label": updated["label"],
        "allowed_chat_ids": json.loads(updated["allowed_chat_ids"]) if updated["allowed_chat_ids"] else None,
        "is_revoked": updated["is_revoked"],
        "no_download": updated["no_download"],
        "expires_at": updated["expires_at"],
    }


@router.delete("/api/admin/tokens/{token_id}")
async def delete_token(token_id: int, request: Request, user: UserContext = Depends(require_master)):
    """Delete a share token permanently."""
    await _invalidate_token_sessions(token_id)
    deleted = await deps.db.delete_viewer_token(token_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Token not found")

    await deps.db.create_audit_log(
        username=user.username,
        role="master",
        action=f"token_deleted:{token_id}",
        endpoint=f"/api/admin/tokens/{token_id}",
        ip_address=request.client.host if request.client else None,
    )

    return {"success": True}


# ---------------------------------------------------------------------------
# Super Admin: Profile + Admin Account CRUD
# ---------------------------------------------------------------------------


@router.get("/api/admin/profiles")
async def admin_list_profiles(user: UserContext = Depends(require_master)):
    """List backup profiles."""
    profiles = await deps.db.list_backup_profiles() if db else []
    if user.role == "admin" and user.allowed_profile_ids is not None:
        profiles = [p for p in profiles if p["id"] in user.allowed_profile_ids]
    return {"profiles": profiles}


@router.post("/api/admin/profiles")
async def admin_create_profile(request: Request, user: UserContext = Depends(require_super_admin)):
    """Create a backup profile. Super admin only."""
    data = await request.json()
    profile_id = data.get("id") or data.get("name", "").lower().replace(" ", "-")[:64]
    if not profile_id:
        raise HTTPException(400, "Profile ID or name required")
    profile = await deps.db.create_backup_profile(
        id=profile_id,
        name=data.get("name", profile_id),
        description=data.get("description"),
        icon=data.get("icon", "database"),
        color=data.get("color", "#8774e1"),
        url=data.get("url"),
        created_by=user.username,
    )
    return {"success": True, "profile": profile}


@router.put("/api/admin/profiles/{profile_id}")
async def admin_update_profile(profile_id: str, request: Request, user: UserContext = Depends(require_master)):
    """Update a backup profile."""
    data = await request.json()
    if user.role == "admin":
        if user.allowed_profile_ids is not None and profile_id not in user.allowed_profile_ids:
            raise HTTPException(403, "Not assigned to this profile")
        data = {k: v for k, v in data.items() if k in ("name", "description")}
    updated = await deps.db.update_backup_profile(profile_id, **data)
    if not updated:
        raise HTTPException(404, "Profile not found")
    return {"success": True, "profile": updated}


@router.delete("/api/admin/profiles/{profile_id}")
async def admin_delete_profile(profile_id: str, user: UserContext = Depends(require_super_admin)):
    """Delete a backup profile. Super admin only."""
    deleted = await deps.db.delete_backup_profile(profile_id)
    if not deleted:
        raise HTTPException(404, "Profile not found")
    return {"success": True}


@router.get("/api/admin/admins")
async def admin_list_admins(user: UserContext = Depends(require_super_admin)):
    """List all admin/super_admin user accounts."""
    accounts = await deps.db.list_user_accounts() if db else []
    profiles = await deps.db.list_backup_profiles() if db else []
    profile_map = {p["id"]: p["name"] for p in profiles}
    safe_accounts = []
    for acct in accounts:
        raw_pids = acct.get("allowed_profile_ids")
        pids = None
        if raw_pids and isinstance(raw_pids, str):
            try:
                pids = json.loads(raw_pids)
            except (json.JSONDecodeError, TypeError):
                pids = None
        elif isinstance(raw_pids, list):
            pids = raw_pids
        safe = {k: v for k, v in acct.items() if k not in ("password_hash", "salt")}
        safe["allowed_profile_ids"] = pids
        safe["profile_names"] = ", ".join(profile_map.get(pid, pid) for pid in pids) if pids else "All"
        safe_accounts.append(safe)
    return {"admins": safe_accounts}


@router.post("/api/admin/admins")
async def admin_create_admin(request: Request, user: UserContext = Depends(require_super_admin)):
    """Create an admin or super_admin user account."""
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        raise HTTPException(400, "Username and password required")
    if len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    existing = await deps.db.get_user_by_username(username) if db else None
    if existing:
        raise HTTPException(409, "Username already exists")
    existing_viewer = await deps.db.get_viewer_by_username(username) if db else None
    if existing_viewer:
        raise HTTPException(409, "Username already exists as viewer")

    salt = secrets.token_hex(32)
    password_hash = _hash_password(password, salt)
    role = data.get("role", "admin")
    if role not in ("admin", "super_admin"):
        raise HTTPException(400, "Role must be 'admin' or 'super_admin'")

    profile_ids = data.get("allowed_profile_ids")
    account = await deps.db.create_user_account(
        username=username,
        password_hash=password_hash,
        salt=salt,
        role=role,
        email=data.get("email"),
        display_name=data.get("display_name"),
        allowed_profile_ids=json.dumps(profile_ids) if profile_ids else None,
        created_by=user.username,
    )
    safe = {k: v for k, v in account.items() if k not in ("password_hash", "salt")}
    raw = safe.get("allowed_profile_ids")
    if raw and isinstance(raw, str):
        try:
            safe["allowed_profile_ids"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return {"success": True, "admin": safe}


@router.put("/api/admin/admins/{account_id}")
async def admin_update_admin(account_id: int, request: Request, user: UserContext = Depends(require_super_admin)):
    """Update an admin account."""
    data = await request.json()
    update_kwargs = {}
    for field in ("display_name", "email", "role", "is_active"):
        if field in data:
            update_kwargs[field] = data[field]
    if "allowed_profile_ids" in data:
        pids = data["allowed_profile_ids"]
        update_kwargs["allowed_profile_ids"] = json.dumps(pids) if pids else None
    if "password" in data and data["password"]:
        salt = secrets.token_hex(32)
        update_kwargs["salt"] = salt
        update_kwargs["password_hash"] = _hash_password(data["password"], salt)
    updated = await deps.db.update_user_account(account_id, **update_kwargs)
    if not updated:
        raise HTTPException(404, "Account not found")
    safe = {k: v for k, v in updated.items() if k not in ("password_hash", "salt")}
    raw = safe.get("allowed_profile_ids")
    if raw and isinstance(raw, str):
        try:
            safe["allowed_profile_ids"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return {"success": True, "admin": safe}


@router.delete("/api/admin/admins/{account_id}")
async def admin_delete_admin(account_id: int, user: UserContext = Depends(require_super_admin)):
    """Delete an admin account."""
    deleted = await deps.db.delete_user_account(account_id)
    if not deleted:
        raise HTTPException(404, "Account not found")
    return {"success": True}


# ---------------------------------------------------------------------------
# App Settings
# ---------------------------------------------------------------------------


@router.get("/api/admin/settings")
async def get_settings(user: UserContext = Depends(require_master)):
    """Get all app settings."""
    settings = await deps.db.get_all_settings()
    return {"settings": settings}


@router.put("/api/admin/settings/{key}")
async def set_setting(key: str, request: Request, user: UserContext = Depends(require_master)):
    """Set an app setting value."""
    if not key or len(key) > 255:
        raise HTTPException(status_code=400, detail="Invalid key")

    try:
        data = await request.json()
        value = data.get("value")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if value is None:
        raise HTTPException(status_code=400, detail="value is required")

    await deps.db.set_setting(key, str(value))

    await deps.db.create_audit_log(
        username=user.username,
        role="master",
        action=f"setting_updated:{key}",
        endpoint=f"/api/admin/settings/{key}",
        ip_address=request.client.host if request.client else None,
    )

    return {"key": key, "value": str(value)}


# ---------------------------------------------------------------------------
# Backup Schedule
# ---------------------------------------------------------------------------


@router.get("/api/admin/backup-config")
async def get_backup_config(user: UserContext = Depends(require_master)):
    """Get current backup schedule configuration."""
    schedule = await deps.db.get_setting("backup.schedule") or deps.config.schedule
    active_boost = (await deps.db.get_setting("backup.active_boost") or "false").lower() == "true"
    heartbeat = await deps.db.get_setting("backup.viewer_heartbeat")
    return {
        "schedule": schedule,
        "default_schedule": deps.config.schedule,
        "active_boost": active_boost,
        "viewer_heartbeat": heartbeat,
    }


@router.put("/api/admin/backup-config")
async def set_backup_config(request: Request, user: UserContext = Depends(require_master)):
    """Update backup schedule configuration."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    updated = {}

    if "schedule" in data:
        cron = data["schedule"].strip()
        parts = cron.split()
        if len(parts) != 5:
            raise HTTPException(status_code=400, detail="Invalid cron format (need 5 fields: min hour day month dow)")
        await deps.db.set_setting("backup.schedule", cron)
        updated["schedule"] = cron

    if "active_boost" in data:
        val = "true" if data["active_boost"] else "false"
        await deps.db.set_setting("backup.active_boost", val)
        updated["active_boost"] = data["active_boost"]

    await deps.db.create_audit_log(
        username=user.username, role="master",
        action=f"backup_config_updated:{','.join(updated.keys())}",
        endpoint="/api/admin/backup-config",
        ip_address=request.client.host if request.client else None,
    )

    return {"updated": updated}


@router.post("/api/admin/backup-heartbeat")
async def backup_heartbeat(user: UserContext = Depends(require_auth)):
    """Record viewer activity heartbeat."""
    from datetime import datetime, timezone
    await deps.db.set_setting("backup.viewer_heartbeat", datetime.now(timezone.utc).isoformat())
    return {"ok": True}


# ---------------------------------------------------------------------------
# OCR / Transcription admin toggles
# ---------------------------------------------------------------------------


@router.put("/api/admin/chats/{chat_id}/ocr")
async def toggle_chat_ocr(chat_id: int, request: Request, user: UserContext = Depends(require_master)):
    """Toggle OCR processing on/off for a chat."""
    body = await request.json()
    enabled = body.get("enabled", False)
    await deps.db.set_setting(f"ocr_enabled:{chat_id}", "true" if enabled else "false")
    if enabled:
        logger.info(f"OCR enabled for chat {chat_id}")
    return {"chat_id": chat_id, "ocr_enabled": enabled}


@router.get("/api/admin/chats/{chat_id}/ocr/status")
async def get_chat_ocr_status(chat_id: int, user: UserContext = Depends(require_master)):
    """Get OCR status and progress for a chat."""
    enabled_val = await deps.db.get_setting(f"ocr_enabled:{chat_id}")
    visible_val = await deps.db.get_setting(f"ocr_visible:{chat_id}")
    progress = await deps.db.get_ocr_progress(chat_id)
    return {
        "chat_id": chat_id,
        "enabled": enabled_val == "true",
        "visible": visible_val == "true" if visible_val else True,
        **progress,
    }


@router.put("/api/admin/chats/{chat_id}/ocr/visibility")
async def toggle_chat_ocr_visibility(chat_id: int, request: Request, user: UserContext = Depends(require_master)):
    """Toggle OCR text visibility for a chat."""
    body = await request.json()
    visible = body.get("visible", True)
    await deps.db.set_setting(f"ocr_visible:{chat_id}", "true" if visible else "false")
    return {"chat_id": chat_id, "ocr_visible": visible}


@router.get("/api/admin/transcription/status")
async def get_transcription_status(user: UserContext = Depends(require_master)):
    """Get global voice transcription progress."""
    progress = await deps.db.get_transcription_progress()
    enabled_val = await deps.db.get_setting("ai.transcription.enabled")
    return {
        "enabled": enabled_val == "true" if enabled_val else True,
        **progress,
    }


# ---------------------------------------------------------------------------
# Vault endpoints
# ---------------------------------------------------------------------------


@router.get("/api/admin/vault/profiles")
async def get_vault_profiles(user: UserContext = Depends(require_master)):
    """Proxy to Vault API to list voice profiles."""
    import httpx
    vault_url = (await deps.db.get_setting("ai.vault.api_url") or "").rstrip("/")
    vault_token = await deps.db.get_setting("ai.vault.api_token") or ""
    if not vault_url:
        return {"profiles": [], "error": "Vault API URL not configured"}
    headers = {}
    if vault_token:
        headers["Authorization"] = f"Bearer {vault_token}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{vault_url}/profiles", headers=headers)
            resp.raise_for_status()
            return {"profiles": resp.json()}
    except Exception as e:
        return {"profiles": [], "error": str(e)[:200]}


@router.post("/api/admin/vault/submit-sample")
async def submit_vault_voice_sample(request: Request, user: UserContext = Depends(require_master)):
    """Submit a voice note as a voice sample to the Vault API."""
    import base64
    import httpx
    body = await request.json()
    chat_id = body.get("chat_id")
    message_id = body.get("message_id")
    profile_id = body.get("profile_id", "")
    reference_text = body.get("reference_text", "")
    if not all([chat_id, message_id, profile_id]):
        return {"status": "error", "message": "Missing chat_id, message_id, or profile_id"}
    media = await deps.db.get_media_for_message(chat_id, message_id)
    if not media or not media.get("file_path"):
        return {"status": "error", "message": "Voice note file not found"}
    file_path = media["file_path"]
    if not os.path.isabs(file_path):
        file_path = os.path.join(deps.config.backup_path, file_path)
    if not os.path.isabs(file_path) or not os.path.exists(file_path):
        raw = media["file_path"]
        if "/media/" in raw:
            rel = raw[raw.index("/media/") + 1:]
            file_path = os.path.join(deps.config.backup_path, rel)
    if not os.path.exists(file_path):
        return {"status": "error", "message": "Voice note file not found on disk"}
    with open(file_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode()
    vault_url = (await deps.db.get_setting("ai.vault.api_url") or "").rstrip("/")
    vault_token = await deps.db.get_setting("ai.vault.api_token") or ""
    if not vault_url:
        return {"status": "error", "message": "Vault API URL not configured"}
    headers = {"Content-Type": "application/json"}
    if vault_token:
        headers["Authorization"] = f"Bearer {vault_token}"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{vault_url}/samples", headers=headers, json={
                "profile_id": profile_id,
                "audio_base64": audio_b64,
                "reference_text": reference_text,
                "filename": os.path.basename(file_path),
            })
            resp.raise_for_status()
            return {"status": "ok", "sample": resp.json()}
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}


@router.post("/api/admin/vault/create-profile")
async def create_vault_profile(request: Request, user: UserContext = Depends(require_master)):
    """Create a new voice profile in the Vault API."""
    import httpx
    body = await request.json()
    name = (body.get("name") or "").strip()
    language = body.get("language", "auto")
    if not name:
        return {"status": "error", "message": "Profile name is required"}
    vault_url = (await deps.db.get_setting("ai.vault.api_url") or "").rstrip("/")
    vault_token = await deps.db.get_setting("ai.vault.api_token") or ""
    if not vault_url:
        return {"status": "error", "message": "Vault API URL not configured"}
    headers = {"Content-Type": "application/json"}
    if vault_token:
        headers["Authorization"] = f"Bearer {vault_token}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{vault_url}/profiles", headers=headers, json={
                "name": name,
                "language": language,
            })
            resp.raise_for_status()
            profile = resp.json()
            return {"status": "ok", "profile": profile}
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}


@router.post("/api/admin/vault/transcribe-one")
async def transcribe_single_voice_note(request: Request, user: UserContext = Depends(require_master)):
    """Transcribe a single voice note on-demand via Voicebox."""
    import httpx
    body = await request.json()
    chat_id = body.get("chat_id")
    message_id = body.get("message_id")
    if not all([chat_id, message_id]):
        return {"status": "error", "message": "Missing chat_id or message_id"}
    media = await deps.db.get_media_for_message(chat_id, message_id)
    if not media or not media.get("file_path"):
        return {"status": "error", "message": "Voice note file not found"}
    file_path = media["file_path"]
    if not os.path.isabs(file_path):
        file_path = os.path.join(deps.config.backup_path, file_path)
    if not os.path.exists(file_path):
        raw = media["file_path"]
        if "/media/" in raw:
            rel = raw[raw.index("/media/") + 1:]
            file_path = os.path.join(deps.config.backup_path, rel)
    if not os.path.exists(file_path):
        return {"status": "error", "message": "Voice note file not found on disk"}
    api_url = (await deps.db.get_setting("ai.transcription.api_url") or "http://host.docker.internal:8080").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            with open(file_path, "rb") as f:
                files = {"file": (os.path.basename(file_path), f, "audio/ogg")}
                resp = await client.post(f"{api_url}/transcribe/file", files=files)
            resp.raise_for_status()
            data = resp.json()
            text = (data.get("text") or "").strip()
            if text:
                lang = data.get("language", "")
                duration = data.get("duration", 0)
                transcript = f"[Voice {duration:.0f}s, {lang}] {text}"
                await deps.db.update_ocr_text(chat_id, message_id, transcript)
                return {"status": "ok", "text": transcript}
            return {"status": "ok", "text": ""}
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}
