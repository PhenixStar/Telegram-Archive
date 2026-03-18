"""WebSocket routes for real-time updates and broadcast helpers."""

import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import dependencies as deps
from .dependencies import (
    AUTH_COOKIE_NAME,
    AUTH_ENABLED,
    AUTH_SESSION_SECONDS,
    UserContext,
    _resolve_session,
    get_user_chat_ids,
    logger,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Broadcast helpers (called from realtime listener and external callers)
# ---------------------------------------------------------------------------


async def broadcast_new_message(chat_id: int, message: dict):
    """Broadcast a new message to subscribed clients."""
    await deps.manager.broadcast_to_chat(chat_id, {"type": "new_message", "chat_id": chat_id, "message": message})


async def broadcast_message_edit(chat_id: int, message_id: int, new_text: str, edit_date: str):
    """Broadcast a message edit to subscribed clients."""
    await deps.manager.broadcast_to_chat(
        chat_id,
        {"type": "edit", "chat_id": chat_id, "message_id": message_id, "new_text": new_text, "edit_date": edit_date},
    )


async def broadcast_message_delete(chat_id: int, message_id: int):
    """Broadcast a message deletion to subscribed clients."""
    await deps.manager.broadcast_to_chat(chat_id, {"type": "delete", "chat_id": chat_id, "message_id": message_id})


async def handle_realtime_notification(payload: dict):
    """Handle real-time notifications and broadcast to WebSocket clients + push notifications."""
    notification_type = payload.get("type")
    chat_id = payload.get("chat_id")
    data = payload.get("data", {})

    if deps.config.display_chat_ids and chat_id not in deps.config.display_chat_ids:
        return

    if notification_type == "new_message":
        await deps.manager.broadcast_to_chat(
            chat_id, {"type": "new_message", "chat_id": chat_id, "message": data.get("message")}
        )

        if push_manager and deps.push_manager.is_enabled:
            message = data.get("message", {})
            chat = await deps.db.get_chat_by_id(chat_id) if db else None
            chat_title = chat.get("title", "Telegram") if chat else "Telegram"

            sender_name = ""
            if message.get("sender_id"):
                sender = await deps.db.get_user_by_id(message.get("sender_id")) if db else None
                if sender:
                    sender_name = sender.get("first_name", "") or sender.get("username", "")

            await deps.push_manager.notify_new_message(
                chat_id=chat_id,
                chat_title=chat_title,
                sender_name=sender_name,
                message_text=message.get("text", "") or "[Media]",
                message_id=message.get("id", 0),
            )

    elif notification_type == "edit":
        await deps.manager.broadcast_to_chat(
            chat_id, {"type": "edit", "message_id": data.get("message_id"), "new_text": data.get("new_text")}
        )
    elif notification_type == "delete":
        await deps.manager.broadcast_to_chat(chat_id, {"type": "delete", "message_id": data.get("message_id")})


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@router.websocket("/ws/updates")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates.

    Auth is enforced via cookie sent during WebSocket upgrade.
    Per-user chat filtering is applied to subscriptions.
    """
    cookies = websocket.cookies
    auth_cookie = cookies.get(AUTH_COOKIE_NAME)
    ws_user_chat_ids: set[int] | None = None

    if AUTH_ENABLED:
        if not auth_cookie:
            await websocket.close(code=4001, reason="Unauthorized")
            return
        session = await _resolve_session(auth_cookie)
        if not session or time.time() - session.created_at > AUTH_SESSION_SECONDS:
            await websocket.close(code=4001, reason="Session expired")
            return
        user_ctx = UserContext(session.username, session.role, session.allowed_chat_ids)
        ws_user_chat_ids = get_user_chat_ids(user_ctx)

    await deps.manager.connect(websocket, allowed_chat_ids=ws_user_chat_ids)
    await deps.listener_mgr.on_viewer_connect(len(deps.manager.active_connections))

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "subscribe":
                chat_id = data.get("chat_id")
                if chat_id:
                    deps.manager.subscribe(websocket, chat_id)
                    await websocket.send_json({"type": "subscribed", "chat_id": chat_id})

            elif action == "unsubscribe":
                chat_id = data.get("chat_id")
                if chat_id:
                    deps.manager.unsubscribe(websocket, chat_id)
                    await websocket.send_json({"type": "unsubscribed", "chat_id": chat_id})

            elif action == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        deps.manager.disconnect(websocket)
        await deps.listener_mgr.on_viewer_disconnect(len(deps.manager.active_connections))
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        deps.manager.disconnect(websocket)
        await deps.listener_mgr.on_viewer_disconnect(len(deps.manager.active_connections))
