"""AI assistant routes: chat, OCR, annotation, embedding, semantic search, config."""

import asyncio
import base64
import os
import time

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
from sqlalchemy import and_, select

from ..db import Media, Message
from . import dependencies as deps
from .dependencies import (
    AUTH_COOKIE_NAME,
    AUTH_ENABLED,
    AUTH_SESSION_SECONDS,
    UserContext,
    _resolve_session,
    get_user_chat_ids,
    logger,
    require_auth,
    require_master,
)

router = APIRouter()

# ---------------------------------------------------------------------------
# AI config defaults (seeded during lifespan)
# ---------------------------------------------------------------------------

def get_ai_config_defaults() -> dict:
    """Return AI config defaults. Must be called after set_app_state()."""
    ollama_base = deps.config.ollama_url.rstrip("/")
    ollama_v1 = f"{ollama_base}/v1" if not ollama_base.endswith("/v1") else ollama_base
    return {
        "ai.vision.provider": "local",
        "ai.vision.api_url": "http://host.docker.internal:8081/v1",
        "ai.vision.api_key": "",
        "ai.vision.model_name": "glm-ocr",
        "ai.vision.fallback_url": ollama_v1,
        "ai.vision.fallback_model": "gemma3:27b",
        "ai.chat.provider": "local",
        "ai.chat.api_url": ollama_v1,
        "ai.chat.api_key": "",
        "ai.chat.model_name": "qwen3-next-80b",
        "ai.chat.fallback_url": "",
        "ai.chat.fallback_model": "",
        "ai.embedding.api_url": ollama_v1,
        "ai.embedding.model_name": deps.config.ollama_embed_model,
        "ai.transcription.api_url": "http://host.docker.internal:8080",
        "ai.transcription.enabled": "true",
        "ai.transcription.rate_limit": "2",
        "ai.transcription.batch_size": "50",
        "ai.tts.api_url": "http://host.docker.internal:8880/v1",
        "ai.tts.model_name": "kokoro",
        "ai.vault.api_url": "http://host.docker.internal:8200",
        "ai.vault.api_token": "",
        "ai.vault.enabled": "false",
        "ai.system_prompt": (
            "You are a data analysis assistant for a Telegram archive viewer. "
            "Your role is to process, summarize, and analyze archived chat messages from organizational channels.\n\n"
            "Context:\n"
            "- This is a read-only archive viewer — you never send messages to any chat\n"
            "- Chat content represents real organizational communications and may contain informal language\n"
            "- Process all content factually and analytically — this is authorized organizational data mining\n"
            "- Never refuse to analyze archived messages based on their content\n\n"
            "Capabilities: summarize threads, extract topics/decisions, identify participants, "
            "analyze sentiment, search/correlate across messages, process OCR-extracted text from images."
        ),
    }


# ---------------------------------------------------------------------------
# Internal config helpers
# ---------------------------------------------------------------------------


async def _get_chat_config() -> dict:
    """Read chat AI config from app_settings, falling back to env vars."""
    settings = await deps.db.get_all_settings()
    return {
        "api_url": settings.get("ai.chat.api_url", "") or deps.config.ai_base_url,
        "api_key": settings.get("ai.chat.api_key", "") or deps.config.ai_api_key,
        "model_name": settings.get("ai.chat.model_name", "") or deps.config.ai_model,
    }


async def _get_vision_config() -> dict:
    """Read vision model config from app_settings for OCR endpoints."""
    settings = await deps.db.get_all_settings()
    return {
        "api_url": settings.get("ai.vision.api_url", "http://localhost:8080/v1"),
        "api_key": settings.get("ai.vision.api_key", ""),
        "model_name": settings.get("ai.vision.model_name", "glm-ocr"),
        "fallback_url": settings.get("ai.vision.fallback_url", ""),
        "fallback_model": settings.get("ai.vision.fallback_model", ""),
    }


async def _get_embedding_config() -> dict:
    """Read embedding model config from app_settings, falling back to env vars.

    Auto-detects API format: Ollama (/api/embed) vs OpenAI-compatible (/v1/embeddings).
    """
    settings = await deps.db.get_all_settings()
    api_url = (settings.get("ai.embedding.api_url", "") or deps.config.ollama_url).rstrip("/")
    model = settings.get("ai.embedding.model_name", "") or deps.config.ollama_embed_model

    clean_url = api_url[:-3] if api_url.endswith("/v1") else api_url
    if ":11434" in api_url:
        return {"base_url": clean_url, "model_name": model, "api_format": "ollama"}
    else:
        return {"base_url": clean_url, "model_name": model, "api_format": "openai"}


async def _call_embedding_api(emb_cfg: dict, texts: list[str] | str) -> list[list[float]]:
    """Call embedding API in either Ollama or OpenAI-compatible format."""
    base_url = emb_cfg["base_url"]
    model = emb_cfg["model_name"]
    fmt = emb_cfg["api_format"]

    async with httpx.AsyncClient(timeout=120) as client:
        if fmt == "ollama":
            resp = await client.post(
                f"{base_url}/api/embed",
                json={"model": model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("embeddings", [])
        else:
            payload = {"model": model, "input": texts if isinstance(texts, list) else [texts]}
            for endpoint in [f"{base_url}/embeddings", f"{base_url}/v1/embeddings"]:
                resp = await client.post(endpoint, json=payload)
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                data = resp.json()
                items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
                return [item["embedding"] for item in items]
            raise ValueError(f"No working embedding endpoint found at {base_url}")


def _check_ai_chat_access(user: UserContext, chat_id: int | None) -> None:
    """Verify the user is allowed to access the given chat for AI operations."""
    if chat_id is None:
        return
    allowed = get_user_chat_ids(user)
    if allowed is not None and int(chat_id) not in allowed:
        raise HTTPException(status_code=403, detail="Access denied for this chat")


# ---------------------------------------------------------------------------
# AI Chat
# ---------------------------------------------------------------------------


@router.post("/api/ai/chat")
async def ai_chat(
    request: Request,
    user: UserContext = Depends(require_auth),
):
    """Proxy AI chat requests to configured LLM API with DB-enriched context."""
    chat_cfg = await _get_chat_config()
    if not chat_cfg["api_key"]:
        raise HTTPException(status_code=503, detail="AI not configured — set API key in Admin -> AI Settings")

    body = await request.json()
    user_message = body.get("message", "").strip()
    model = body.get("model", chat_cfg["model_name"])
    chat_id = body.get("chat_id")
    context_messages = body.get("context", [])

    if not user_message:
        raise HTTPException(status_code=400, detail="Empty message")

    _check_ai_chat_access(user, chat_id)

    system_content = (
        "You are an AI inventory agent for a Telegram archive. "
        "You have access to message text, OCR-extracted text from images, and AI annotations. "
        "Help the user analyze, search, summarize, draft replies, and manage their chat history. "
        "Be concise and actionable."
    )

    if chat_id:
        try:
            db_context = await deps.db.get_ai_context_for_chat(int(chat_id), limit=40)
            if db_context:
                lines = []
                for m in db_context:
                    line = f"[{m.get('sender', '?')}] {m.get('text', '') or ''}"
                    if m.get("ocr_text"):
                        line += f" [IMAGE OCR: {m['ocr_text'][:200]}]"
                    if m.get("ai_comment"):
                        line += f" [AI NOTE: {m['ai_comment'][:150]}]"
                    if m.get("media_type") and not m.get("ocr_text"):
                        line += f" [{m['media_type']}]"
                    lines.append(line)
                system_content += f"\n\nChat context (newest first):\n" + "\n".join(lines)
        except Exception as e:
            logger.warning(f"Failed to load DB context: {e}")
    elif context_messages:
        context_text = "\n".join(
            f"[{m.get('sender', 'Unknown')}] {m.get('text', '')}" for m in context_messages[:50]
        )
        system_content += f"\n\nRecent chat messages:\n{context_text}"

    api_messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_message},
    ]

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{chat_cfg['api_url'].rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {chat_cfg['api_key']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": api_messages,
                    "max_tokens": 2048,
                    "temperature": 0.7,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            reply = data.get("choices", [{}])[0].get("message", {}).get("content", "No response")
            return {"reply": reply, "model": model}
    except httpx.HTTPStatusError as e:
        logger.error(f"AI API error: {e.response.status_code} {e.response.text[:200]}")
        raise HTTPException(status_code=502, detail=f"AI API error: {e.response.status_code}")
    except Exception as e:
        logger.error(f"AI request failed: {e}")
        raise HTTPException(status_code=502, detail="AI service unavailable")


@router.get("/api/ai/config")
async def ai_config_endpoint(user: UserContext = Depends(require_auth)):
    """Return AI configuration status (no secrets exposed to non-admins)."""
    all_settings = await deps.db.get_all_settings()
    ai_keys = {k: v for k, v in all_settings.items() if k.startswith("ai.")}
    if getattr(user, "role", None) != "master":
        return {
            "enabled": bool(ai_keys.get("ai.chat.api_url") or deps.config.ai_api_key),
            "model": ai_keys.get("ai.chat.model_name", deps.config.ai_model),
        }

    def _section(prefix):
        return {
            "provider": ai_keys.get(f"{prefix}.provider", "local"),
            "api_url": ai_keys.get(f"{prefix}.api_url", ""),
            "api_key_set": bool(ai_keys.get(f"{prefix}.api_key", "")),
            "model_name": ai_keys.get(f"{prefix}.model_name", ""),
            "fallback_url": ai_keys.get(f"{prefix}.fallback_url", ""),
            "fallback_model": ai_keys.get(f"{prefix}.fallback_model", ""),
        }
    return {
        "vision": _section("ai.vision"),
        "chat": _section("ai.chat"),
        "embedding": {
            "api_url": ai_keys.get("ai.embedding.api_url", ""),
            "model_name": ai_keys.get("ai.embedding.model_name", ""),
        },
        "tts": {
            "api_url": ai_keys.get("ai.tts.api_url", ""),
            "model_name": ai_keys.get("ai.tts.model_name", ""),
        },
        "transcription": {
            "api_url": ai_keys.get("ai.transcription.api_url", ""),
            "enabled": ai_keys.get("ai.transcription.enabled", "true") == "true",
            "rate_limit": ai_keys.get("ai.transcription.rate_limit", "2"),
            "batch_size": ai_keys.get("ai.transcription.batch_size", "50"),
        },
        "vault": {
            "api_url": ai_keys.get("ai.vault.api_url", ""),
            "api_token_set": bool(ai_keys.get("ai.vault.api_token", "")),
            "enabled": ai_keys.get("ai.vault.enabled", "false") == "true",
        },
        "system_prompt": ai_keys.get("ai.system_prompt", ""),
    }


@router.put("/api/admin/ai-config")
async def update_ai_config(request: Request, user: UserContext = Depends(require_master)):
    """Bulk update AI configuration settings (admin only)."""
    body = await request.json()
    allowed_prefixes = ("ai.vision.", "ai.chat.", "ai.embedding.", "ai.tts.", "ai.transcription.", "ai.vault.", "ai.system_prompt")
    for key, value in body.items():
        if not any(key.startswith(p) or key == p for p in allowed_prefixes):
            continue
        await deps.db.set_setting(key, str(value))
    return {"status": "ok"}


@router.post("/api/admin/ai-config/test")
async def test_ai_connection(request: Request, user: UserContext = Depends(require_master)):
    """Test if an AI model endpoint is reachable."""
    body = await request.json()
    api_url = body.get("api_url", "").rstrip("/")
    api_key = body.get("api_key", "")
    test_type = body.get("test_type", "openai")
    if not api_url:
        return {"status": "error", "message": "No URL provided"}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if test_type == "whisper":
                resp = await client.get(f"{api_url}/health", headers=headers)
                if resp.status_code < 500:
                    return {"status": "ok", "message": f"Whisper connected ({resp.status_code})"}
                return {"status": "error", "message": f"Whisper unreachable ({resp.status_code})"}
            elif test_type == "vault":
                resp = await client.get(f"{api_url}/profiles", headers=headers)
                if resp.status_code < 500:
                    profiles = resp.json() if resp.status_code == 200 else []
                    count = len(profiles) if isinstance(profiles, list) else 0
                    return {"status": "ok", "message": f"Vault connected — {count} profiles"}
                return {"status": "error", "message": f"Vault unreachable ({resp.status_code})"}
            else:
                for path in [f"{api_url}/models", f"{api_url.rsplit('/v1', 1)[0]}/health"]:
                    try:
                        resp = await client.get(path, headers=headers)
                        if resp.status_code < 500:
                            return {"status": "ok", "message": f"Connected ({resp.status_code})"}
                    except Exception:
                        continue
                return {"status": "error", "message": "Endpoint unreachable"}
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}


# ---------------------------------------------------------------------------
# OCR endpoints
# ---------------------------------------------------------------------------


@router.post("/api/ai/ocr/{chat_id}/{message_id}")
async def ai_ocr_message(
    chat_id: int,
    message_id: int,
    user: UserContext = Depends(require_auth),
):
    """OCR a single message's image using vision model from app_settings."""
    vcfg = await _get_vision_config()
    if not vcfg["api_url"]:
        raise HTTPException(status_code=503, detail="Vision model not configured")

    _check_ai_chat_access(user, chat_id)

    async with deps.db.db_manager.async_session_factory() as sess:
        result = await sess.execute(
            select(Message.ocr_text, Media.file_path, Media.mime_type)
            .outerjoin(Media, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
            .where(and_(Message.chat_id == chat_id, Message.id == message_id))
            .limit(1)
        )
        row = result.first()

    if not row:
        raise HTTPException(status_code=404, detail="Message not found")

    if row[0]:
        return {"message_id": message_id, "ocr_text": row[0], "cached": True}

    file_path = row[1]
    if not file_path:
        raise HTTPException(status_code=404, detail="No media file for this message")

    abs_path = os.path.join(deps.config.backup_path, file_path) if not os.path.isabs(file_path) else file_path
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="Media file missing from disk")

    with open(abs_path, "rb") as f:
        img_data = base64.b64encode(f.read()).decode()

    mime = row[2] or "image/jpeg"
    if not mime.startswith("image"):
        mime = "image/jpeg"

    api_url = vcfg["api_url"].rstrip("/")
    headers = {"Content-Type": "application/json"}
    if vcfg["api_key"]:
        headers["Authorization"] = f"Bearer {vcfg['api_key']}"

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{api_url}/chat/completions",
                headers=headers,
                json={
                    "model": vcfg["model_name"],
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Extract ALL text from this image. Return only the extracted text, nothing else. If no text, describe the image briefly."},
                                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_data}"}},
                            ],
                        }
                    ],
                    "max_tokens": 2048,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            ocr_result = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        logger.error(f"OCR API error: {e}")
        raise HTTPException(status_code=502, detail="OCR service error")

    await deps.db.update_ocr_text(chat_id, message_id, ocr_result)
    return {"message_id": message_id, "ocr_text": ocr_result, "cached": False}


@router.post("/api/ai/ocr-batch/{chat_id}")
async def ai_ocr_batch(
    chat_id: int,
    request: Request,
    user: UserContext = Depends(require_auth),
):
    """Queue batch OCR for all un-processed images in a chat."""
    vcfg = await _get_vision_config()
    if not vcfg["api_url"]:
        raise HTTPException(status_code=503, detail="Vision model not configured")

    _check_ai_chat_access(user, chat_id)

    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    limit = min(body.get("limit", 20), 100)

    pending = await deps.db.get_messages_needing_ocr(chat_id, limit=limit)
    if not pending:
        return {"queued": 0, "message": "All images already processed"}

    api_url = vcfg["api_url"].rstrip("/")
    api_key = vcfg["api_key"]
    model_name = vcfg["model_name"]

    async def _run_batch():
        processed = 0
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                for item in pending:
                    file_path = item["file_path"]
                    abs_path = os.path.join(deps.config.backup_path, file_path) if not os.path.isabs(file_path) else file_path
                    if not os.path.exists(abs_path):
                        continue
                    mime = item.get("mime_type", "image/jpeg") or "image/jpeg"
                    if not mime.startswith("image"):
                        continue
                    try:
                        with open(abs_path, "rb") as f:
                            img_data = base64.b64encode(f.read()).decode()
                        resp = await client.post(
                            f"{api_url}/chat/completions",
                            headers=headers,
                            json={
                                "model": model_name,
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": [
                                            {"type": "text", "text": "Extract ALL text from this image. Return only the extracted text, nothing else. If no text, describe the image briefly."},
                                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_data}"}},
                                        ],
                                    }
                                ],
                                "max_tokens": 2048,
                            },
                        )
                        resp.raise_for_status()
                        data = resp.json()
                        ocr_result = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                        await deps.db.update_ocr_text(item["chat_id"], item["message_id"], ocr_result)
                        processed += 1
                    except Exception as e:
                        logger.warning(f"Batch OCR failed for msg {item['message_id']}: {e}")
        except Exception as e:
            logger.error(f"Batch OCR task error: {e}")
        logger.info(f"Batch OCR completed: {processed}/{len(pending)} images processed for chat {chat_id}")

    asyncio.create_task(_run_batch())
    return {"queued": len(pending), "message": f"Processing {len(pending)} images in background"}


@router.post("/api/ai/annotate/{chat_id}/{message_id}")
async def ai_annotate_message(
    chat_id: int,
    message_id: int,
    request: Request,
    user: UserContext = Depends(require_auth),
):
    """AI-annotate a single message (summarize, tag, categorize)."""
    chat_cfg = await _get_chat_config()
    if not chat_cfg["api_key"]:
        raise HTTPException(status_code=503, detail="AI not configured — set API key in Admin -> AI Settings")

    _check_ai_chat_access(user, chat_id)

    body = await request.json()
    instruction = body.get("instruction", "Summarize and tag this message concisely.")

    async with deps.db.db_manager.async_session_factory() as sess:
        result = await sess.execute(
            select(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id))
        )
        msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    msg_text = msg.text or ""
    if msg.ocr_text:
        msg_text += f"\n[OCR from image]: {msg.ocr_text}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{chat_cfg['api_url'].rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {chat_cfg['api_key']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": body.get("model", chat_cfg["model_name"]),
                    "messages": [
                        {"role": "system", "content": "You are an AI inventory agent annotating Telegram messages. Be concise."},
                        {"role": "user", "content": f"{instruction}\n\nMessage:\n{msg_text}"},
                    ],
                    "max_tokens": 512,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            comment = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        logger.error(f"Annotate API error: {e}")
        raise HTTPException(status_code=502, detail="AI service error")

    await deps.db.update_ai_comment(chat_id, message_id, comment)
    return {"message_id": message_id, "ai_comment": comment}


@router.get("/api/ai/context/{chat_id}")
async def ai_chat_context(
    chat_id: int,
    limit: int = Query(default=30, le=100),
    user: UserContext = Depends(require_auth),
):
    """Get AI-enriched context for a chat (messages + OCR + AI comments)."""
    _check_ai_chat_access(user, chat_id)

    context = await deps.db.get_ai_context_for_chat(chat_id, limit=limit)
    ocr_count = sum(1 for m in context if m.get("ocr_text"))
    annotated_count = sum(1 for m in context if m.get("ai_comment"))
    return {"messages": context, "ocr_count": ocr_count, "annotated_count": annotated_count}


# ---------------------------------------------------------------------------
# Semantic search / embedding
# ---------------------------------------------------------------------------


@router.get("/api/semantic/status")
async def semantic_status(
    chat_id: int = Query(..., description="Chat ID to check"),
    user: UserContext = Depends(require_auth),
):
    """Check embedding progress for a chat."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")
    counts = await deps.db.get_embedding_count(chat_id)
    return counts


@router.post("/api/semantic/embed")
async def trigger_embedding(
    chat_id: int = Query(..., description="Chat ID to embed"),
    limit: int = Query(50, description="Max messages to embed per batch"),
    user: UserContext = Depends(require_master),
):
    """Trigger embedding generation for a chat (master only)."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    emb_cfg = await _get_embedding_config()
    model = emb_cfg["model_name"]

    messages = await deps.db.get_unembedded_messages(chat_id, limit=limit)
    if not messages:
        counts = await deps.db.get_embedding_count(chat_id)
        return {"batch_stored": 0, "message": "All messages already embedded", **counts}

    texts = [m["text"][:2000] for m in messages]

    try:
        vectors = await _call_embedding_api(emb_cfg, texts)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Embedding failed: {e!s}")

    if len(vectors) != len(messages):
        raise HTTPException(status_code=502, detail="Embedding count mismatch")

    embeddings = [
        {"message_id": messages[i]["id"], "embedding": vectors[i]} for i in range(len(messages))
    ]
    stored = await deps.db.store_embeddings(chat_id, embeddings, model)
    counts = await deps.db.get_embedding_count(chat_id)
    return {"batch_stored": stored, **counts}


@router.get("/api/semantic/search")
async def semantic_search_endpoint(
    q: str = Query(..., min_length=2, description="Search query"),
    chat_id: int = Query(..., description="Chat ID to search"),
    limit: int = Query(20, ge=1, le=100),
    user: UserContext = Depends(require_auth),
):
    """Semantic search using embeddings."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    emb_cfg = await _get_embedding_config()

    try:
        vectors = await _call_embedding_api(emb_cfg, q)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Embedding query failed: {e!s}")

    query_embedding = vectors[0] if vectors else []
    if not query_embedding:
        raise HTTPException(status_code=502, detail="Empty embedding returned")

    results = await deps.db.semantic_search(chat_id, query_embedding, limit=limit)
    return {"results": results, "total": len(results), "method": "semantic"}


@router.get("/api/notifications/settings")
async def get_notification_settings(
    auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
):
    """Get notification settings for the viewer."""
    if AUTH_ENABLED:
        session = (await _resolve_session(auth_cookie)) if auth_cookie else None
        if not session or time.time() - session.created_at > AUTH_SESSION_SECONDS:
            return {"enabled": False, "reason": "Not authenticated"}

    notifications_active = deps.config.enable_notifications or deps.config.push_notifications in ("basic", "full")

    return {
        "enabled": notifications_active,
        "mode": deps.config.push_notifications,
        "websocket_url": "/ws/updates",
    }
