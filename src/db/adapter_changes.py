"""Tag search and the what-changed feed.

Two small discovery features ported from upstream (semantic ports, not
cherry-picks — upstream carries a multi-account ChatScope/ref layer this
archive does not have), sharing one module since neither is large enough to
justify its own file yet:

- ``TagSearchMixin.search_messages_by_tag`` serves the #hashtag/$cashtag
  click-through: tap a tag in a rendered message to see every other message
  using it.
- ``ChangeFeedMixin.get_recent_changes`` serves the what-changed feed: the
  edits and deletions the archive kept, read from ``message_versions`` and
  ``messages.is_deleted``/``deleted_at``.

This archive holds one Telegram account per database, so there is no
account_id/ChatScope/chat-ref indirection to ride the way upstream's does.
Both queries take ``allowed_chat_ids`` straight from the caller
(``get_user_chat_ids``) and filter with a plain ``chat_id IN (...)`` in SQL —
that set IS the whole entitlement surface here, so a restricted viewer's
query can never touch a chat outside it.
"""

import re
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select, tuple_

from .models import Chat, Message, MessageVersion


class TagSearchMixin:
    """Messages carrying a given #hashtag or $CASHTAG, newest first."""

    async def search_messages_by_tag(
        self,
        tag: str,
        *,
        allowed_chat_ids: set[int] | None,
        chat_id: int | None = None,
        outgoing_only: bool = False,
        limit: int = 50,
        offset: int = 0,
        scan_cap: int = 3000,
    ) -> dict[str, Any]:
        """Messages carrying ``tag`` as a whole token, newest first.

        The tag view's data source: SQL prefilters with an escaped ILIKE
        (works identically on SQLite and PostgreSQL, same pattern
        ``get_messages_paginated`` already uses for its text search) and a
        word-boundary regex post-filter drops substring hits ('#tag' inside
        '#taglonger'). ``chat_id`` narrows to one chat (the This Chat tab);
        ``outgoing_only`` is My Messages (the archive owner's side of every
        conversation). ``allowed_chat_ids`` is the caller's entitlement
        (``get_user_chat_ids``); when it is a set, every query below adds
        ``Message.chat_id.in_(allowed_chat_ids)`` so a restricted viewer's tag
        search can only ever touch entitled chats.

        Offset paging re-scans from the top by design — tag result sets are
        small, and each request bounds its own scan (``scan_cap`` prefilter
        rows) via a keyset cursor walked in bounded chunks, so no single call
        can walk the whole messages table. When the cap truncates the scan,
        ``has_more`` stays False — pages past the cap are unreachable through
        an offset API, and advertising them would loop the client forever —
        and ``truncated`` turns True so the UI can say the search was cut
        short.

        Returns ``{"results": [...], "has_more": bool, "truncated": bool}``;
        rows carry message id/date/text/is_outgoing/sender_name plus
        chat_id/chat_title/chat_type so the viewer can jump straight to the
        message.
        """
        # Hashtags search case-insensitively (official Telegram behavior);
        # cashtags are uppercase-only entities, so '$TSLA' must not match
        # '$tsla' in text (that is not the same entity).
        flags = re.IGNORECASE if tag.startswith("#") else 0
        boundary = re.compile(rf"(?<![\w#$]){re.escape(tag)}(?!\w)", flags)
        escaped = tag.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        needed = offset + limit + 1  # one extra row proves has_more
        matched: list[dict[str, Any]] = []
        scanned = 0
        cursor: tuple[Any, ...] | None = None
        chunk = max(limit * 3, 60)
        exhausted = False

        async with self.db_manager.async_session_factory() as session:
            while len(matched) < needed and scanned < scan_cap:
                stmt = (
                    select(
                        Message.id,
                        Message.date,
                        Message.text,
                        Message.is_outgoing,
                        Message.sender_name,
                        Message.chat_id,
                        Chat.title,
                        Chat.first_name,
                        Chat.last_name,
                        Chat.username,
                        Chat.type.label("chat_type"),
                    )
                    .join(Chat, Chat.id == Message.chat_id)
                    .where(Message.text.isnot(None))
                    .where(Message.text.ilike(f"%{escaped}%", escape="\\"))
                )
                if chat_id is not None:
                    stmt = stmt.where(Message.chat_id == chat_id)
                if outgoing_only:
                    stmt = stmt.where(Message.is_outgoing == 1)
                if allowed_chat_ids is not None:
                    stmt = stmt.where(Message.chat_id.in_(allowed_chat_ids))
                order_cols = (Message.date, Message.chat_id, Message.id)
                if cursor is not None:
                    stmt = stmt.where(tuple_(*order_cols) < cursor)
                stmt = stmt.order_by(*(col.desc() for col in order_cols)).limit(chunk)

                rows = (await session.execute(stmt)).mappings().all()
                scanned += len(rows)
                for row in rows:
                    if not boundary.search(row["text"] or ""):
                        continue
                    title = (
                        row["title"]
                        or " ".join(part for part in (row["first_name"], row["last_name"]) if part)
                        or row["username"]
                        or "Unknown"
                    )
                    matched.append(
                        {
                            "id": row["id"],
                            "date": row["date"].isoformat() if hasattr(row["date"], "isoformat") else row["date"],
                            "text": row["text"],
                            "is_outgoing": bool(row["is_outgoing"]),
                            "sender_name": row["sender_name"],
                            "chat_id": row["chat_id"],
                            "chat_title": title,
                            "chat_type": row["chat_type"],
                        }
                    )
                    if len(matched) >= needed:
                        break
                if len(rows) < chunk:
                    exhausted = True
                    break
                cursor = tuple(rows[-1][key] for key in ("date", "chat_id", "id"))

        truncated = not exhausted and scanned >= scan_cap and len(matched) < needed
        return {
            "results": matched[offset : offset + limit],
            "has_more": len(matched) > offset + limit,
            "truncated": truncated,
        }


class ChangeFeedMixin:
    """The what-changed feed: deletions and edits the archive captured."""

    async def get_recent_changes(
        self,
        *,
        since: datetime | None = None,
        before: datetime | None = None,
        limit: int = 50,
        allowed_chat_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """The what-changed feed: deletions and edits the archive captured.

        The archive's differentiator is that it KEEPS what disappeared; this
        is the query that finally lists it. Two streams share one shape:

        * ``deleted`` — soft-deleted messages (``is_deleted=1``), dated by
          ``deleted_at``, carrying the text the archive kept.
        * ``edited`` — ``message_versions`` rows, dated by ``captured_at``
          (when the archive observed the supersession), carrying the old text
          plus the message's CURRENT text.

        Newest first. ``before`` is an exclusive keyset cursor over the
        per-row date: pass the last row's ``date`` back to page. Rows sharing
        that exact microsecond with the cursor are skipped — this is a review
        feed, not an export, and the export path is the lossless one.
        ``allowed_chat_ids`` (``get_user_chat_ids``) filters both streams in
        SQL exactly like every other chat-scoped query, so a restricted
        viewer's feed touches only their own chats' rows. Hard deletions
        cannot appear here: their content no longer exists
        (DELETION_MODE=soft is what feeds this).
        """
        per_stream = max(1, min(int(limit), 200))

        def _chat_fields(row) -> dict[str, Any]:
            name = row.title or " ".join(p for p in (row.first_name, row.last_name) if p) or row.username or ""
            return {"chat_id": row.chat_id, "title": name, "type": row.chat_type}

        async with self.db_manager.async_session_factory() as session:
            deleted_stmt = (
                select(
                    Message.id.label("message_id"),
                    Message.chat_id,
                    Message.deleted_at.label("date"),
                    Message.text,
                    Message.sender_name,
                    Chat.title,
                    Chat.first_name,
                    Chat.last_name,
                    Chat.username,
                    Chat.type.label("chat_type"),
                )
                .join(Chat, Chat.id == Message.chat_id)
                .where(Message.is_deleted == 1, Message.deleted_at.isnot(None))
            )
            edited_stmt = (
                select(
                    MessageVersion.message_id,
                    MessageVersion.chat_id,
                    MessageVersion.captured_at.label("date"),
                    MessageVersion.text.label("old_text"),
                    Message.text.label("new_text"),
                    Message.sender_name,
                    Chat.title,
                    Chat.first_name,
                    Chat.last_name,
                    Chat.username,
                    Chat.type.label("chat_type"),
                )
                .join(
                    Message,
                    and_(Message.chat_id == MessageVersion.chat_id, Message.id == MessageVersion.message_id),
                )
                .join(Chat, Chat.id == MessageVersion.chat_id)
            )
            if since is not None:
                deleted_stmt = deleted_stmt.where(Message.deleted_at >= since)
                edited_stmt = edited_stmt.where(MessageVersion.captured_at >= since)
            if before is not None:
                deleted_stmt = deleted_stmt.where(Message.deleted_at < before)
                edited_stmt = edited_stmt.where(MessageVersion.captured_at < before)
            if allowed_chat_ids is not None:
                deleted_stmt = deleted_stmt.where(Message.chat_id.in_(allowed_chat_ids))
                edited_stmt = edited_stmt.where(MessageVersion.chat_id.in_(allowed_chat_ids))
            deleted_stmt = deleted_stmt.order_by(Message.deleted_at.desc()).limit(per_stream)
            edited_stmt = edited_stmt.order_by(MessageVersion.captured_at.desc()).limit(per_stream)

            changes: list[dict[str, Any]] = []
            for row in (await session.execute(deleted_stmt)).all():
                changes.append(
                    {
                        "kind": "deleted",
                        "date": row.date.isoformat() if row.date else None,
                        "chat": _chat_fields(row),
                        "message_id": row.message_id,
                        "sender_name": row.sender_name,
                        "text": row.text,
                    }
                )
            for row in (await session.execute(edited_stmt)).all():
                changes.append(
                    {
                        "kind": "edited",
                        "date": row.date.isoformat() if row.date else None,
                        "chat": _chat_fields(row),
                        "message_id": row.message_id,
                        "sender_name": row.sender_name,
                        "old_text": row.old_text,
                        "new_text": row.new_text,
                    }
                )
            changes.sort(key=lambda c: c["date"] or "", reverse=True)
            return changes[:per_stream]
