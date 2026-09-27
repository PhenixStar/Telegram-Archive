"""Tag search (#hashtag/$cashtag click-through) and the what-changed feed.

Both routes are read-only discovery surfaces layered on data the archive
already keeps: message text (tag search) and ``messages.is_deleted`` /
``message_versions`` (the change feed). Neither widens what a viewer can see —
both filter through ``get_user_chat_ids``, the same entitlement every other
chat-scoped route in this viewer uses.
"""

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from . import dependencies as deps
from .dependencies import UserContext, get_user_chat_ids, logger, require_auth

router = APIRouter()

# A tag is a #hashtag (word chars, at least one non-digit — Telegram excludes
# pure numbers) or a $CASHTAG (1-8 uppercase latin letters, the official
# shape used by Telegram's own clients).
_TAG_PATTERN = re.compile(r"^#(?!\d+$)\w{1,64}$|^\$[A-Z]{1,8}$")


@router.get("/api/tags/{tag}")
async def search_tag(
    tag: str,
    user: UserContext = Depends(require_auth),
    scope: str = Query("all", pattern="^(chat|mine|all)$"),
    chat_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Messages using a #hashtag or $cashtag, newest first — the tag view.

    Mirrors the official Telegram clients' tag tabs, mapped onto an archive:
    ``scope=chat`` is This Chat (requires ``chat_id``, entitlement-checked the
    same way every other ``/api/chats/{chat_id}/...`` route is), ``scope=mine``
    is My Messages (the archive's outgoing side), ``scope=all`` is the whole
    entitled archive. A restricted viewer's search is filtered in SQL via
    ``get_user_chat_ids``, so this route can never widen what a viewer sees.
    """
    if not _TAG_PATTERN.match(tag):
        raise HTTPException(status_code=400, detail="Not a recognizable #hashtag or $cashtag")

    allowed_chat_ids = get_user_chat_ids(user)
    kwargs: dict = {"allowed_chat_ids": allowed_chat_ids, "limit": limit, "offset": offset}
    if scope == "chat":
        if chat_id is None:
            raise HTTPException(status_code=400, detail="scope=chat requires chat_id")
        if allowed_chat_ids is not None and chat_id not in allowed_chat_ids:
            raise HTTPException(status_code=403, detail="Access denied")
        kwargs["chat_id"] = chat_id
    elif scope == "mine":
        kwargs["outgoing_only"] = True

    try:
        payload = await deps.db.search_messages_by_tag(tag, **kwargs)
    except Exception as e:
        # Type name only: the exception text can echo statement parameters —
        # the tag itself and the viewer's chat-scope ids.
        logger.error(f"Error searching tag: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Internal server error")
    payload["tag"] = tag
    return payload


def _parse_changes_bound(value: str, param: str) -> datetime:
    """ISO bound to naive UTC. A tz-aware value converts, never relabels."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {param} date. Use ISO 8601.") from None
    if parsed.tzinfo:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


@router.get("/api/changes")
async def get_recent_changes(
    user: UserContext = Depends(require_auth),
    since: str | None = Query(None),
    before: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """What changed: deletions and edits the archive captured, newest first.

    ``since`` bounds the window's start (inclusive); ``before`` is the keyset
    cursor — pass the last row's ``date`` back to page older. Entitlements are
    the same ``get_user_chat_ids`` scope every chat-scoped route uses, so a
    restricted viewer's feed only ever lists changes from their own chats.
    """
    parsed_since = _parse_changes_bound(since, "since") if since else None
    parsed_before = _parse_changes_bound(before, "before") if before else None
    try:
        changes = await deps.db.get_recent_changes(
            since=parsed_since,
            before=parsed_before,
            limit=limit,
            allowed_chat_ids=get_user_chat_ids(user),
        )
        next_cursor = changes[-1]["date"] if len(changes) == limit else None
        return {"changes": changes, "next_before": next_cursor}
    except HTTPException:
        raise
    except Exception as e:
        # Counts/type only — feed rows carry chat titles and message text.
        logger.error(f"Error building the changes feed: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Internal server error")
