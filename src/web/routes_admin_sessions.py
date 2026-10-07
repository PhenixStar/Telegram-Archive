"""End every viewer session at once, for example after a master password change.

The viewer never re-checks a session against ``VIEWER_PASSWORD`` or an account's
password, so a changed password leaves every old session valid until
``AUTH_SESSION_DAYS`` runs out. ``POST /api/admin/sessions/end-all`` deletes
every row of ``viewer_sessions`` (all roles, viewer accounts and share-token
sessions), empties this process's cache and closes the live sockets of the ended
sessions. Another viewer process on the same database drops its cached copies
within ``_SESSION_REVALIDATE_SECONDS`` (see ``dependencies._revalidate_cached_session``).

Account hand-off tickets are not revoked. They are stateless HMAC tickets that
live 60 seconds, are single use, and are redeemed by a *different* archive with
its own database and sessions, which an end-all here cannot reach anyway: each
archive needs its own end-all. Minting one needs a live session here, so after
an end-all no new ticket can be minted from an ended session. The residual
window is a ticket minted in the 60 seconds before the end-all; rotating
``VIEWER_HANDOFF_SECRET`` is the server-side kill for those.

Push subscriptions are left as they are, matching logout, which also keeps them.
"""

import json

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import dependencies as deps
from .dependencies import (
    AUTH_COOKIE_NAME,
    UserContext,
    _drop_cached_sessions,
    _has_role,
    logger,
    require_master,
)

router = APIRouter()


def require_session_admin(user: UserContext = Depends(require_master)) -> UserContext:
    """Master and above only: the env login (super_admin) or a proxy admin (master).

    A profile-scoped ``admin`` account must not be able to log out the people
    above it, so ``require_master`` (which admits ``admin``) is narrowed here.
    ``require_master`` also refuses requests carrying ``X-Viewer-Only: true``.
    """
    if not _has_role(user.role, "master"):
        raise HTTPException(status_code=403, detail="Master access required")
    return user


async def _keep_current_from_body(request: Request) -> bool:
    """Read ``keep_current`` from an optional JSON body. No body means False."""
    raw = await request.body()
    if not raw.strip():
        return False
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    keep_current = body.get("keep_current", False)
    if not isinstance(keep_current, bool):
        raise HTTPException(status_code=400, detail="keep_current must be true or false")
    return keep_current


def _caller_session_token(user: UserContext, auth_cookie: str | None) -> str | None:
    """The session key the caller is logged in with, or None (proxy identity, no cookie)."""
    session = deps._sessions.get(auth_cookie) if auth_cookie else None
    if session is None or session.username != user.username:
        return None
    return auth_cookie


@router.post("/api/admin/sessions/end-all")
async def end_all_sessions(
    request: Request,
    keep_current: bool | None = None,
    user: UserContext = Depends(require_session_admin),
    auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
):
    """End every viewer session; the caller's own too unless ``keep_current`` is true.

    ``keep_current`` comes from the query string or a JSON body.
    """
    if not deps.db:
        raise HTTPException(status_code=503, detail="Database not available")
    if keep_current is None:
        keep_current = await _keep_current_from_body(request)
    caller_token = _caller_session_token(user, auth_cookie)
    keep_token = caller_token if keep_current else None

    deleted = await deps.db.delete_all_sessions(keep_token=keep_token)
    # Read the cache after the delete: a session cached during that await has no
    # row left either, unless it was created after the DELETE, in which case its
    # next request reads it back from the database and it survives correctly.
    ended = {token: s.username for token, s in deps._sessions.items() if token != keep_token}
    ended.update(deleted)
    await _drop_cached_sessions(ended)

    # Best effort: the sessions are already gone, so a failed audit write must not
    # turn this into an error that leaves the caller's cookie in place.
    try:
        await deps.db.create_audit_log(
            username=user.username,
            role=user.role,
            action="sessions_ended_all:kept_current" if keep_token else "sessions_ended_all",
            endpoint="/api/admin/sessions/end-all",
            ip_address=deps.client_ip(request),
        )
    except Exception as e:
        logger.warning(f"Failed to write audit log ({type(e).__name__})")
    logger.info(f"Ended {len(ended)} viewer sessions by admin action")

    current_session_ended = caller_token is not None and keep_token is None
    response = JSONResponse({"success": True, "ended": len(ended), "current_session_ended": current_session_ended})
    if current_session_ended:
        response.delete_cookie(AUTH_COOKIE_NAME)
    return response
