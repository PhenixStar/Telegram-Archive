"""Media serving, thumbnail, LQIP, root page, and permalink routes."""

import os
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

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
)

router = APIRouter()

templates_dir = Path(__file__).parent / "templates"
static_dir = Path(__file__).parent / "static"


@router.get("/sw.js")
async def serve_service_worker():
    """Serve the service worker from root path with proper headers."""
    sw_path = static_dir / "sw.js"
    if not sw_path.exists():
        raise HTTPException(status_code=404, detail="Service worker not found")

    return FileResponse(sw_path, media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})


@router.get("/media/thumb/{size}/{folder:path}/{filename}")
async def serve_thumbnail(
    size: int, folder: str, filename: str,
    request: Request,
    user: UserContext = Depends(require_auth),
):
    """Serve on-demand generated thumbnails with auth and path traversal protection."""
    if not deps._media_root:
        raise HTTPException(status_code=404, detail="Media directory not configured")

    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None:
        try:
            media_chat_id = int(folder.split("/")[0])
            if media_chat_id not in user_chat_ids:
                raise HTTPException(status_code=403, detail="Access denied")
        except ValueError:
            pass

    from .thumbnails import ensure_thumbnail, ensure_video_thumbnail, _is_video

    thumb_path = await ensure_thumbnail(deps._media_root, size, folder, filename)
    if not thumb_path and _is_video(filename):
        thumb_path = await ensure_video_thumbnail(deps._media_root, size, folder, filename)

    if not thumb_path:
        raise HTTPException(status_code=404, detail="Thumbnail not available")

    return FileResponse(
        thumb_path,
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/api/lqip/{folder:path}/{filename}")
async def serve_lqip(folder: str, filename: str, user: UserContext = Depends(require_auth)):
    """Return a tiny base64 blur placeholder for progressive image loading."""
    if not deps._media_root:
        return JSONResponse({"blur": None})

    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None:
        try:
            media_chat_id = int(folder.split("/")[0])
            if media_chat_id not in user_chat_ids:
                raise HTTPException(status_code=403, detail="Access denied")
        except ValueError:
            pass

    from .thumbnails import generate_lqip_base64

    try:
        blur = await generate_lqip_base64(deps._media_root, folder, filename)
    except Exception:
        blur = None

    return JSONResponse(
        {"blur": blur},
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/media/{path:path}")
async def serve_media(path: str, download: int = Query(0), user: UserContext = Depends(require_auth)):
    """Serve media files with authentication, path traversal protection, and no_download enforcement."""
    if not deps._media_root:
        raise HTTPException(status_code=404, detail="Media directory not configured")

    if user.no_download and download:
        raise HTTPException(status_code=403, detail="Downloads disabled for this account")

    if ".." in path.split("/") or path.startswith("/"):
        raise HTTPException(status_code=403, detail="Access denied")

    candidate = deps._media_root / path
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="File not found")
    if not resolved.is_relative_to(deps._media_root):
        raise HTTPException(status_code=403, detail="Access denied")

    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None:
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] != "avatars":
            try:
                media_chat_id = int(parts[0])
                if media_chat_id not in user_chat_ids:
                    raise HTTPException(status_code=403, detail="Access denied")
            except ValueError:
                pass

    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(resolved)


@router.get("/", response_class=HTMLResponse)
async def read_root():
    """Serve the main application page."""
    return FileResponse(
        templates_dir / "index.html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@router.get("/chat/{chat_id}", response_class=HTMLResponse)
async def permalink_page(
    chat_id: int,
    request: Request,
    auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
):
    """Serve viewer page for permalink URLs. Auth + chat access check."""
    if AUTH_ENABLED:
        if not auth_cookie:
            redirect = f"/chat/{chat_id}"
            msg = request.query_params.get("msg", "")
            if msg:
                redirect += f"?msg={msg}"
            return HTMLResponse(
                status_code=302,
                headers={"Location": f"/?redirect={quote(redirect)}"},
            )
        session = await _resolve_session(auth_cookie)
        if not session or time.time() - session.created_at > AUTH_SESSION_SECONDS:
            redirect = f"/chat/{chat_id}"
            msg = request.query_params.get("msg", "")
            if msg:
                redirect += f"?msg={msg}"
            return HTMLResponse(
                status_code=302,
                headers={"Location": f"/?redirect={quote(redirect)}"},
            )
        user_chat_ids = get_user_chat_ids(
            UserContext(
                role=session.role,
                username=session.username,
                allowed_chat_ids=session.allowed_chat_ids,
                no_download=session.no_download,
            )
        )
        if user_chat_ids is not None and chat_id not in user_chat_ids:
            raise HTTPException(status_code=403, detail="Access denied")
    return FileResponse(
        templates_dir / "index.html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )
