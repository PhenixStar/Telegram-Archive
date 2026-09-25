"""Switch between Telegram accounts without logging in again.

Each Telegram account runs its own archive (own database and viewer) behind one
hostname; a reverse proxy routes by the ``tg_account`` cookie, which a
``?account=<key>`` navigation sets. Every viewer keeps its own session, so the
first visit to another account used to show its login card.

A hand-off lets the archive you are signed in to vouch for you to another one:

1. ``POST /api/auth/handoff`` (admin and above) mints a ticket for one target
   account: HMAC-SHA256 over ``VIEWER_HANDOFF_SECRET`` (shared by the viewers),
   valid 60 seconds, carrying the session's own role and account scope. The
   target account comes from the stored backup profile, never from the request.
2. The browser navigates to ``/?account=<key>#handoff=<ticket>``; the fragment
   never reaches a server or a log.
3. The target viewer's ``POST /auth/handoff`` accepts it only if the signature,
   expiry and target (``VIEWER_ACCOUNT_KEY``) match and it was not used before,
   then opens a session with exactly the carried role and scope, never more.

Unset ``VIEWER_HANDOFF_SECRET`` or ``VIEWER_ACCOUNT_KEY`` disables the feature.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import dependencies as deps
from .dependencies import (
    AUTH_COOKIE_NAME,
    AUTH_SESSION_SECONDS,
    ROLE_HIERARCHY,
    UserContext,
    _check_rate_limit,
    _create_session,
    _get_secure_cookies,
    _has_role,
    _record_login_attempt,
    logger,
    require_auth,
)

router = APIRouter()

HANDOFF_TTL_SECONDS = 60
HANDOFF_SECRET = os.getenv("VIEWER_HANDOFF_SECRET", "").strip()
ACCOUNT_KEY = os.getenv("VIEWER_ACCOUNT_KEY", "").strip()
ACCOUNT_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,32}")

# Tickets already redeemed on this viewer, pruned once they could not verify anyway.
_used_nonces: dict[str, float] = {}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(secret: str, body: str) -> str:
    return _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())


def mint_ticket(secret: str, claims: dict, now: float | None = None) -> str:
    """Signed, expiring, single-use ticket for ``claims`` (username, role, scope, account)."""
    payload = {**claims, "exp": int((now or time.time()) + HANDOFF_TTL_SECONDS), "nonce": secrets.token_urlsafe(16)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_signature(secret, body)}"


def verify_ticket(secret: str, ticket: str, account_key: str, now: float | None = None) -> dict | None:
    """Claims of a valid ticket addressed to ``account_key``, else None (fail closed)."""
    if not secret or not account_key or not isinstance(ticket, str) or ticket.count(".") != 1:
        return None
    body, signature = ticket.split(".")
    if not hmac.compare_digest(signature, _signature(secret, body)):
        return None
    try:
        claims = json.loads(_unb64(body))
    except ValueError, TypeError:
        return None
    now = now or time.time()
    if not isinstance(claims, dict) or not isinstance(claims.get("exp"), int) or claims["exp"] < now:
        return None
    if claims.get("account") != account_key or claims.get("role") not in ROLE_HIERARCHY:
        return None
    if not _has_role(claims["role"], "admin") or not isinstance(claims.get("username"), str):
        return None
    profiles = claims.get("allowed_profile_ids")
    if profiles is not None and not (isinstance(profiles, list) and all(isinstance(p, str) for p in profiles)):
        return None
    nonce = claims.get("nonce")
    for used, expiry in list(_used_nonces.items()):
        if expiry < now:
            _used_nonces.pop(used, None)
    if not isinstance(nonce, str) or nonce in _used_nonces:
        return None
    _used_nonces[nonce] = claims["exp"]
    return claims


def profile_account_key(profile: dict) -> str | None:
    """The ``?account=`` key a backup profile's URL switches to, if any."""
    query = urlsplit(profile.get("url") or "").query
    values = parse_qs(query).get("account")
    return values[0] if values else None


@router.post("/api/auth/handoff")
async def issue_handoff(request: Request, user: UserContext = Depends(require_auth)):
    """Mint a ticket that signs the current admin in to another account's archive."""
    if not HANDOFF_SECRET:
        raise HTTPException(status_code=403, detail="Account switching is not configured")
    if not _has_role(user.role, "admin"):
        raise HTTPException(status_code=403, detail="Only admins can switch accounts")
    try:
        data = await request.json()
        profile_id = str(data.get("profile_id", "")).strip()
        requested_account = str(data.get("account", "")).strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request")

    profiles = await deps.db.list_backup_profiles(active_only=True) if deps.db else []
    profile = next((p for p in profiles if p.get("id") == profile_id), None)
    account = profile_account_key(profile) if profile else None
    if user.role == "admin":
        # An admin's reach is the profiles assigned to it, so the target must be
        # resolved from a stored profile (the default account's archive holds them).
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        if user.allowed_profile_ids is not None and profile_id not in user.allowed_profile_ids:
            raise HTTPException(status_code=403, detail="Not allowed for this account")
    elif not account:
        # A super admin reaches every account, so the key may come from the
        # request where this archive stores no profiles; the target still only
        # accepts a ticket addressed to itself.
        if not ACCOUNT_KEY_PATTERN.fullmatch(requested_account):
            raise HTTPException(status_code=404, detail="Account not found")
        account = requested_account

    ticket = mint_ticket(
        HANDOFF_SECRET,
        {
            "username": user.username,
            "role": user.role,
            "allowed_profile_ids": user.allowed_profile_ids,
            "account": account,
        },
    )
    if deps.db:
        await deps.db.create_audit_log(
            username=user.username,
            role=user.role,
            action="account_handoff_issued",
            endpoint=f"/api/auth/handoff:{account}",
        )
    return {"account": account, "ticket": ticket}


@router.post("/auth/handoff")
async def accept_handoff(request: Request):
    """Open a session on this archive from a hand-off ticket addressed to it."""
    client_ip = deps.client_ip(request)
    if not _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    try:
        ticket = (await request.json()).get("ticket", "")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request")

    claims = verify_ticket(HANDOFF_SECRET, ticket, ACCOUNT_KEY)
    if claims is None:
        _record_login_attempt(client_ip)
        logger.warning("Rejected an account hand-off ticket")
        raise HTTPException(status_code=401, detail="Invalid or expired hand-off")

    token = await _create_session(
        claims["username"], claims["role"], None, allowed_profile_ids=claims.get("allowed_profile_ids")
    )
    response = JSONResponse({"success": True, "role": claims["role"], "username": claims["username"]})
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=_get_secure_cookies(request),
        samesite="lax",
        max_age=AUTH_SESSION_SECONDS,
    )
    if deps.db:
        await deps.db.create_audit_log(
            username=claims["username"],
            role=claims["role"],
            action="account_handoff_accepted",
            endpoint="/auth/handoff",
            ip_address=client_ip,
            user_agent=request.headers.get("user-agent", "")[:500],
        )
    return response
