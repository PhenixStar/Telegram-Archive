"""Admin routes: Vault voice profile proxy endpoints."""

import os

from fastapi import APIRouter, Depends, Request

from . import dependencies as deps
from .dependencies import (
    UserContext,
    require_master,
)

router = APIRouter()


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
