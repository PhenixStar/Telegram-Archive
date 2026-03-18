"""Chat, message, search, folder, topic, and density routes."""

import csv
import io
import json
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from . import dependencies as deps
from .dependencies import (
    UserContext,
    get_user_chat_ids,
    logger,
    require_auth,
    require_master,
)

router = APIRouter()

# ---------------------------------------------------------------------------
# Avatar helpers
# ---------------------------------------------------------------------------

import glob
import os

_avatar_cache: dict[int, str | None] = {}
_avatar_cache_time: datetime | None = None
AVATAR_CACHE_TTL_SECONDS = 300


def _find_avatar_path(chat_id: int, chat_type: str) -> str | None:
    """Find avatar file path for a chat."""
    avatar_folder = "users" if chat_type == "private" else "chats"
    avatar_dir = os.path.join(deps.config.media_path, "avatars", avatar_folder)

    if not os.path.exists(avatar_dir):
        return None

    pattern = os.path.join(avatar_dir, f"{chat_id}_*.jpg")
    matches = glob.glob(pattern)

    legacy_path = os.path.join(avatar_dir, f"{chat_id}.jpg")
    if os.path.exists(legacy_path):
        matches.append(legacy_path)

    if matches:
        newest_avatar = max(matches, key=os.path.getmtime)
        avatar_file = os.path.basename(newest_avatar)
        return f"avatars/{avatar_folder}/{avatar_file}"

    return None


def _get_cached_avatar_path(chat_id: int, chat_type: str) -> str | None:
    """Get avatar path with caching."""
    global _avatar_cache, _avatar_cache_time

    if _avatar_cache_time and (datetime.utcnow() - _avatar_cache_time).total_seconds() > AVATAR_CACHE_TTL_SECONDS:
        _avatar_cache.clear()
        _avatar_cache_time = None

    if chat_id in _avatar_cache:
        return _avatar_cache[chat_id]

    avatar_path = _find_avatar_path(chat_id, chat_type)
    _avatar_cache[chat_id] = avatar_path
    if _avatar_cache_time is None:
        _avatar_cache_time = datetime.utcnow()

    return avatar_path


# ---------------------------------------------------------------------------
# Chat list and info
# ---------------------------------------------------------------------------


@router.get("/api/chats")
async def get_chats(
    user: UserContext = Depends(require_auth),
    limit: int = Query(50, ge=1, le=1000, description="Number of chats to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    search: str = Query(None, description="Search query for chat names/usernames"),
    archived: bool | None = Query(None, description="Filter by archived status"),
    folder_id: int | None = Query(None, description="Filter by folder ID"),
):
    """Get chats with metadata, paginated. Returns most recent chats first."""
    try:
        user_chat_ids = get_user_chat_ids(user)
        if user_chat_ids is not None:
            chats = await deps.db.get_all_chats(search=search, archived=archived, folder_id=folder_id)
            chats = [c for c in chats if c["id"] in user_chat_ids]
            total = len(chats)
            chats = chats[offset : offset + limit]
        else:
            chats = await deps.db.get_all_chats(
                limit=limit, offset=offset, search=search, archived=archived, folder_id=folder_id
            )
            total = await deps.db.get_chat_count(search=search, archived=archived, folder_id=folder_id)

        for chat in chats:
            try:
                avatar_path = _get_cached_avatar_path(chat["id"], chat.get("type", "private"))
                if avatar_path:
                    chat["avatar_url"] = f"/media/{avatar_path}"
                else:
                    chat["avatar_url"] = None
            except Exception as e:
                logger.error(f"Error finding avatar for chat {chat.get('id')}: {e}")
                chat["avatar_url"] = None

        return {
            "chats": chats,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(chats) < total,
        }
    except Exception as e:
        logger.error(f"Error fetching chats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/chats/{chat_id}")
async def get_chat_info(
    chat_id: int,
    user: UserContext = Depends(require_auth),
):
    """Get a single chat by ID (for permalink navigation)."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=404, detail="Chat not found")
    chat = await deps.db.get_chat_by_id(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


@router.get("/api/chats/{chat_id}/messages")
async def get_messages(
    chat_id: int,
    user: UserContext = Depends(require_auth),
    limit: int = 50,
    offset: int = 0,
    search: str | None = None,
    before_date: str | None = None,
    before_id: int | None = None,
    after_date: str | None = None,
    after_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    topic_id: int | None = None,
):
    """Get messages for a specific chat with user and media info."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    if before_date and after_date:
        raise HTTPException(status_code=400, detail="Cannot use both before_date and after_date")

    def _parse_date(value: str | None, param_name: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo:
                parsed = parsed.replace(tzinfo=None)
            return parsed
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid {param_name} format. Use ISO 8601.")

    parsed_before_date = _parse_date(before_date, "before_date")
    parsed_after_date = _parse_date(after_date, "after_date")
    parsed_date_from = _parse_date(date_from, "date_from")
    parsed_date_to = _parse_date(date_to, "date_to")

    try:
        messages = await deps.db.get_messages_paginated(
            chat_id=chat_id,
            limit=limit,
            offset=offset,
            search=search,
            before_date=parsed_before_date,
            before_id=before_id,
            after_date=parsed_after_date,
            after_id=after_id,
            date_from=parsed_date_from,
            date_to=parsed_date_to,
            topic_id=topic_id,
        )
        return messages
    except Exception as e:
        logger.error(f"Error fetching messages: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/chats/{chat_id}/pinned")
async def get_pinned_messages(chat_id: int, user: UserContext = Depends(require_auth)):
    """Get all pinned messages for a chat, ordered by date descending."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        pinned_messages = await deps.db.get_pinned_messages(chat_id)
        return pinned_messages
    except Exception as e:
        logger.error(f"Error fetching pinned messages: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/chats/{chat_id}/messages/{msg_id}/context")
async def get_message_context(
    chat_id: int,
    msg_id: int,
    user: UserContext = Depends(require_auth),
):
    """Get messages around a target message for permalink navigation."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        result = await deps.db.get_messages_around(chat_id, msg_id, count=50)
        if not result:
            raise HTTPException(status_code=403, detail="Access denied")
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching message context: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/search")
async def search_messages_global(
    q: str,
    chat_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    user: UserContext = Depends(require_auth),
):
    """Cross-chat FTS5 search with access control. Falls back to ILIKE."""
    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    if len(q) > 200:
        raise HTTPException(status_code=400, detail="Query too long (max 200 chars)")
    if not q.strip():
        return {"results": [], "total": 0, "method": "none", "has_more": False}

    allowed_chat_ids = get_user_chat_ids(user)
    status = await deps.db.get_fts_status()

    if status == "ready":
        results = await deps.db.search_messages_fts(q, chat_id, allowed_chat_ids, limit, offset)
        total = await deps.db.count_fts_matches(q, chat_id, allowed_chat_ids)
        method = "fts"
    else:
        if chat_id is None:
            return {
                "results": [],
                "total": 0,
                "method": "ilike",
                "has_more": False,
                "fts_status": status or "not_initialized",
            }
        if allowed_chat_ids is not None and chat_id not in allowed_chat_ids:
            raise HTTPException(status_code=403, detail="Access denied")
        results = await deps.db.get_messages_paginated(chat_id, limit, offset, search=q)
        total = len(results)
        method = "ilike"

    return {
        "results": results,
        "total": total,
        "method": method,
        "has_more": len(results) == limit,
    }


@router.get("/api/fts/status")
async def get_fts_status(user: UserContext = Depends(require_auth)):
    """Get current FTS index build status."""
    status = await deps.db.get_fts_status()
    return {"status": status or "not_initialized"}


@router.get("/api/folders")
async def get_folders(user: UserContext = Depends(require_auth)):
    """Get all chat folders with their chat counts."""
    try:
        folders = await deps.db.get_all_folders()
        return {"folders": folders}
    except Exception as e:
        logger.error(f"Error fetching folders: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/chats/{chat_id}/topics")
async def get_chat_topics(chat_id: int, user: UserContext = Depends(require_auth)):
    """Get forum topics for a chat."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        topics = await deps.db.get_forum_topics(chat_id)
        return {"topics": topics}
    except Exception as e:
        logger.error(f"Error fetching topics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/archived/count")
async def get_archived_count(user: UserContext = Depends(require_auth)):
    """Get the number of archived chats."""
    try:
        user_chat_ids = get_user_chat_ids(user)
        if user_chat_ids is not None:
            all_archived = await deps.db.get_all_chats(archived=True)
            count = sum(1 for c in all_archived if c["id"] in user_chat_ids)
        else:
            count = await deps.db.get_archived_chat_count()
        return {"count": count}
    except Exception as e:
        logger.error(f"Error fetching archived count: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/stats")
async def get_stats(user: UserContext = Depends(require_auth)):
    """Get cached backup statistics (fast, calculated daily)."""
    try:
        stats = await deps.db.get_cached_statistics()
        stats["timezone"] = deps.config.viewer_timezone
        stats["stats_calculation_hour"] = deps.config.stats_calculation_hour
        stats["show_stats"] = deps.config.show_stats

        listener_active_since = await deps.db.get_metadata("listener_active_since")
        stats["listener_active"] = bool(listener_active_since)
        stats["listener_active_since"] = listener_active_since if listener_active_since else None

        stats["push_notifications"] = deps.config.push_notifications
        stats["push_enabled"] = push_manager is not None and deps.push_manager.is_enabled

        stats["enable_notifications"] = deps.config.enable_notifications or deps.config.push_notifications in ("basic", "full")

        return stats
    except Exception as e:
        logger.error(f"Error fetching stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/stats/refresh")
async def refresh_stats(user: UserContext = Depends(require_master)):
    """Manually trigger stats recalculation."""
    try:
        stats = await deps.db.calculate_and_store_statistics()
        stats["timezone"] = deps.config.viewer_timezone
        return stats
    except Exception as e:
        logger.error(f"Error calculating stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/chats/{chat_id}/stats")
async def get_chat_stats(chat_id: int, user: UserContext = Depends(require_auth)):
    """Get statistics for a specific chat."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        stats = await deps.db.get_chat_stats(chat_id)
        return stats
    except Exception as e:
        logger.error(f"Error getting chat stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/chats/{chat_id}/messages/by-date")
async def get_message_by_date(
    chat_id: int,
    user: UserContext = Depends(require_auth),
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    timezone: str = Query(None, description="Timezone for date interpretation (e.g., 'Europe/Madrid')"),
):
    """Find the first message on or after a specific date for navigation."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        tz_str = timezone or deps.config.viewer_timezone or "UTC"
        try:
            user_tz = ZoneInfo(tz_str)
        except Exception:
            logger.warning(f"Invalid timezone '{tz_str}', falling back to UTC")
            user_tz = ZoneInfo("UTC")

        naive_date = datetime.strptime(date, "%Y-%m-%d")
        local_start_of_day = naive_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=user_tz)
        target_date = local_start_of_day.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

        message = await deps.db.find_message_by_date_with_joins(chat_id, target_date)

        if not message:
            raise HTTPException(status_code=404, detail="No messages found for this date")

        return message
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error finding message by date: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/chats/{chat_id}/export")
async def export_chat(
    chat_id: int,
    format: str = Query("json", description="Export format: json or csv"),
    user: UserContext = Depends(require_auth),
):
    """Export chat history to JSON or CSV."""
    if user.no_download:
        raise HTTPException(status_code=403, detail="Downloads disabled for this account")
    if format not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="Invalid format. Use 'json' or 'csv'")
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        chat = await deps.db.get_chat_by_id(chat_id)
        if not chat:
            raise HTTPException(status_code=404, detail="Chat not found")

        chat_name = chat.get("title") or chat.get("username") or str(chat_id)
        safe_name = "".join(c for c in chat_name if c.isalnum() or c in (" ", "-", "_")).strip()

        if format == "csv":
            filename = f"{safe_name}_export.csv"
            include_media = True

            async def iter_csv():
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(["id", "date", "sender_name", "text", "media_type", "media_file"])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

                async for msg in deps.db.get_messages_for_export(chat_id, include_media=include_media):
                    writer.writerow([
                        msg.get("id", ""),
                        msg.get("date", ""),
                        msg.get("sender", {}).get("name", ""),
                        msg.get("text", "") or "",
                        msg.get("media_type", "") or "",
                        msg.get("media_path", "") or "",
                    ])
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate(0)

            encoded_filename = quote(filename)
            return StreamingResponse(
                iter_csv(),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
            )

        # Default: JSON export
        filename = f"{safe_name}_export.json"

        async def iter_json():
            yield "[\n"
            first = True
            async for msg in deps.db.get_messages_for_export(chat_id):
                if not first:
                    yield ",\n"
                first = False
                yield json.dumps(msg, ensure_ascii=False)
            yield "\n]"

        encoded_filename = quote(filename)
        return StreamingResponse(
            iter_json(),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting chat: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/chats/{chat_id}/boundary")
async def get_boundary_message(
    chat_id: int,
    direction: str = Query("first", description="Jump direction: 'first' (oldest) or 'last' (newest)"),
    user: UserContext = Depends(require_auth),
):
    """Return the first or last message ID in a chat for jump-to navigation."""
    if direction not in ("first", "last"):
        raise HTTPException(status_code=400, detail="direction must be 'first' or 'last'")
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        message_id = await deps.db.get_boundary_message_id(chat_id, direction)
        if message_id is None:
            raise HTTPException(status_code=404, detail="No messages found in this chat")
        return {"message_id": message_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching boundary message: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# Media / Members / Density
# ---------------------------------------------------------------------------


@router.get("/api/chats/{chat_id}/media")
async def get_chat_media(
    chat_id: int,
    user: UserContext = Depends(require_auth),
    media_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    before: int | None = Query(None),
):
    """Get media messages for a chat with optional type filter and cursor pagination."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    valid_types = {"photo", "video", "voice", "document", "animation"}
    if media_type and media_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Invalid media_type. Must be one of: {', '.join(sorted(valid_types))}")

    try:
        result = await deps.db.get_media_messages(chat_id, media_type=media_type, limit=limit, before=before)
        return result
    except Exception as e:
        logger.error(f"Error fetching media messages: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/chats/{chat_id}/members")
async def get_chat_members(
    chat_id: int,
    user: UserContext = Depends(require_auth),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Get unique senders in a chat with message counts."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        members = await deps.db.get_chat_members(chat_id, limit=limit, offset=offset)
        return {"members": members}
    except Exception as e:
        logger.error(f"Error fetching chat members: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/chats/{chat_id}/density")
async def get_chat_density(
    chat_id: int,
    user: UserContext = Depends(require_auth),
    granularity: str = Query("week"),
    timezone: str | None = Query(None),
):
    """Get message count per time period for timeline/heatmap visualisation."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    if granularity not in ("day", "week", "month"):
        raise HTTPException(status_code=400, detail="granularity must be day, week, or month")

    tz_str = timezone or deps.config.viewer_timezone or "UTC"

    try:
        density = await deps.db.get_message_density(chat_id, granularity=granularity, timezone=tz_str)
        return {"density": density, "granularity": granularity}
    except Exception as e:
        logger.error(f"Error fetching message density: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# Push notification endpoints
# ---------------------------------------------------------------------------


@router.get("/api/push/config")
async def get_push_config():
    """Get push notification configuration (public, no auth)."""
    result = {
        "mode": deps.config.push_notifications,
        "enabled": deps.config.push_notifications == "full" and push_manager is not None and deps.push_manager.is_enabled,
        "vapid_public_key": None,
    }

    if push_manager and deps.push_manager.is_enabled:
        result["vapid_public_key"] = deps.push_manager.public_key

    return result


@router.post("/api/push/subscribe")
async def push_subscribe(request: Request, user: UserContext = Depends(require_auth)):
    """Subscribe to push notifications."""
    if not push_manager or not deps.push_manager.is_enabled:
        raise HTTPException(status_code=400, detail="Push notifications not enabled. Set PUSH_NOTIFICATIONS=full")

    try:
        data = await request.json()

        endpoint = data.get("endpoint")
        keys = data.get("keys", {})
        p256dh = keys.get("p256dh")
        auth = keys.get("auth")
        chat_id = data.get("chat_id")

        if not endpoint or not p256dh or not auth:
            raise HTTPException(status_code=400, detail="Missing required subscription data")

        if chat_id:
            user_chat_ids = get_user_chat_ids(user)
            if user_chat_ids is not None and chat_id not in user_chat_ids:
                raise HTTPException(status_code=403, detail="Access denied to this chat")

        user_agent = request.headers.get("user-agent", "")[:500]
        user_chat_ids_list = get_user_chat_ids(user)

        success = await deps.push_manager.subscribe(
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            chat_id=chat_id,
            user_agent=user_agent,
            username=user.username,
            allowed_chat_ids=list(user_chat_ids_list) if user_chat_ids_list is not None else None,
        )

        if success:
            return {"status": "subscribed", "chat_id": chat_id}
        else:
            raise HTTPException(status_code=500, detail="Failed to store subscription")

    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Push subscribe error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request, user: UserContext = Depends(require_auth)):
    """Unsubscribe from push notifications."""
    if not deps.push_manager:
        raise HTTPException(status_code=400, detail="Push notifications not enabled")

    try:
        data = await request.json()
        endpoint = data.get("endpoint")

        if not endpoint:
            raise HTTPException(status_code=400, detail="Missing endpoint")

        success = await deps.push_manager.unsubscribe(endpoint)
        return {"status": "unsubscribed" if success else "not_found"}

    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Push unsubscribe error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/internal/push")
async def internal_push(request: Request):
    """Internal endpoint for SQLite real-time push notifications."""
    client_host = request.client.host if request.client else None

    allowed = False
    if client_host and (
        client_host in ("127.0.0.1", "localhost", "::1") or client_host.startswith(("172.", "10.", "192.168."))
    ):
        allowed = True

    if not allowed:
        logger.warning(f"Rejected /internal/push from non-private IP: {client_host}")
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        payload = await request.json()
        if deps.realtime_listener:
            await deps.realtime_listener.handle_http_push(payload)
        return {"status": "ok"}
    except Exception as e:
        logger.warning(f"Error handling internal push: {e}")
        return {"status": "error", "detail": "Internal push processing failed"}
