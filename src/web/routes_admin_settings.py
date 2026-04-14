"""Admin routes: app settings, backup schedule, OCR/translation toggles."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from . import dependencies as deps
from .dependencies import (
    UserContext,
    logger,
    require_auth,
    require_master,
)

router = APIRouter()


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
    await deps.db.set_setting("backup.viewer_heartbeat", datetime.now(UTC).isoformat())
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
# Translation endpoints
# ---------------------------------------------------------------------------


@router.put("/api/admin/chats/{chat_id}/translation")
async def toggle_chat_translation(chat_id: int, request: Request, user: UserContext = Depends(require_master)):
    """Toggle translation on/off for a chat."""
    body = await request.json()
    enabled = body.get("enabled", False)
    await deps.db.set_setting(f"translation_enabled:{chat_id}", "true" if enabled else "false")
    if enabled:
        logger.info(f"Translation enabled for chat {chat_id}")
    return {"chat_id": chat_id, "translation_enabled": enabled}


@router.get("/api/admin/chats/{chat_id}/translation/status")
async def get_chat_translation_status(chat_id: int, user: UserContext = Depends(require_master)):
    """Get translation status and progress for a chat."""
    enabled_val = await deps.db.get_setting(f"translation_enabled:{chat_id}")
    progress = await deps.db.get_translation_progress(chat_id)
    return {
        "chat_id": chat_id,
        "enabled": enabled_val == "true",
        **progress,
    }


@router.post("/api/admin/translation/enable-all")
async def bulk_enable_translation(user: UserContext = Depends(require_master)):
    """Enable translation for all chats in the database."""
    all_chats = await deps.db.get_all_chats()
    count = 0
    for chat in all_chats:
        await deps.db.set_setting(f"translation_enabled:{chat['id']}", "true")
        count += 1
    logger.info(f"Translation bulk-enabled for {count} chats")
    return {"enabled_count": count}
