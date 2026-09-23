"""Media serving, thumbnail, LQIP, root page, and permalink routes."""

import mimetypes
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


def _checked_media_request_path(path: str) -> str:
    """Reject a request path with a ".." segment or a leading "/", before anything else runs.

    The ASGI server percent-decodes the URL before routing, so ``%2e%2e``
    reaches a ``{folder:path}`` route as a real ``..`` segment. The ACL below
    keys off the path's first component, and the eventual file read
    (``_validate_traversal``) only checks that the RESOLVED path stays inside
    the media root, not which chat folder it lands in — so an encoded ``..``
    used to let the ACL see one (often unparseable, so silently skipped)
    folder while the resolve()-based containment check happily served a
    different, real chat's folder that was never authorized. Checking first,
    on the exact string both the ACL and the file lookup then use unchanged,
    closes that gap.
    """
    if path.startswith("/") or ".." in path.split("/"):
        raise HTTPException(status_code=403, detail="Access denied")
    return path


def _enforce_restricted_chat_acl(path: str, user: UserContext) -> None:
    """Apply the per-chat ACL to the path's own first component.

    Avatars stay available for UI chrome for every account. For a RESTRICTED
    account (``get_user_chat_ids`` returns a set, i.e. a share-token or
    chat-limited viewer), any other folder that fails to parse as a chat id —
    including ``_shared/...``, the dedup store that holds blobs pooled across
    every chat — is now DENIED outright instead of silently passed through:
    only a real chat id can ever be proven to be on the allow-list, so a
    folder that isn't one can never be proven safe for a restricted account.
    """
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is None:
        return  # master, or a viewer with no chat restriction

    folder = path.split("/", 1)[0]
    if folder == "avatars":
        return
    try:
        media_chat_id = int(folder)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    if media_chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")


@router.get("/media/thumb/{size}/{folder:path}/{filename}")
async def serve_thumbnail(
    size: int, folder: str, filename: str,
    request: Request,
    user: UserContext = Depends(require_auth),
):
    """Serve on-demand generated thumbnails with auth and path traversal protection."""
    if not deps._media_root:
        raise HTTPException(status_code=404, detail="Media directory not configured")

    # Traversal check FIRST, then the ACL on the checked path's resolved first
    # component — the ACL must authorize the exact string the file lookup
    # below uses, or the two can disagree (see _checked_media_request_path).
    requested = _checked_media_request_path(f"{folder}/{filename}")
    _enforce_restricted_chat_acl(requested, user)

    from .thumbnails import _is_video, ensure_thumbnail, ensure_video_thumbnail

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

    # Same order as the thumbnail route: traversal check first, then the ACL on
    # the checked path, so the authorized string is the one that is read.
    requested = _checked_media_request_path(f"{folder}/{filename}")
    _enforce_restricted_chat_acl(requested, user)

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

    path = _checked_media_request_path(path)
    _enforce_restricted_chat_acl(path, user)

    candidate = deps._media_root / path
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="File not found")
    if not resolved.is_relative_to(deps._media_root):
        raise HTTPException(status_code=403, detail="Access denied")

    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Archived files come from any Telegram contact. Rendering an .html or .svg
    # inline would run its scripts on the viewer's own origin, so only media the
    # browser cannot execute is served inline; everything else downloads.
    content_type, _ = mimetypes.guess_type(resolved.name)
    if not _is_inline_safe(content_type):
        return FileResponse(resolved, filename=resolved.name, content_disposition_type="attachment")
    return FileResponse(resolved)


def _is_inline_safe(content_type: str | None) -> bool:
    """True for media types a browser displays without executing script."""
    if not content_type:
        return False
    if content_type == "image/svg+xml":
        return False
    return content_type.startswith(("image/", "video/", "audio/")) or content_type == "application/pdf"


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
