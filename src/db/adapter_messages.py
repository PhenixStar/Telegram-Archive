"""Message-related database operations mixin.

Handles insert, query, update, delete for messages and related helpers.
"""

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..message_utils import utcnow_naive
from .adapter import _strip_tz, retry_on_locked
from .models import Media, Message, MessageVersion, Reaction, SyncStatus, User


def _message_conflict_update_values(message_data: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Build update values for message upserts without undoing soft deletes."""
    update_values = dict(values)

    if not message_data.get("is_deleted"):
        update_values.pop("is_deleted", None)
        update_values.pop("deleted_at", None)
    elif "deleted_at" not in message_data:
        update_values.pop("deleted_at", None)
    if "is_pinned" not in message_data:
        update_values.pop("is_pinned", None)

    return update_values


def _datetime_hash_value(dt: datetime | None) -> str | None:
    dt = _strip_tz(dt)
    if dt is None:
        return None
    return dt.isoformat(timespec="microseconds")


def _message_version_hash(
    chat_id: int,
    message_id: int,
    text: str | None,
    date: datetime,
) -> str:
    # FROZEN CONTRACT: this exact encoding (key set, sort_keys, separators,
    # microsecond timespec) IS the dedup identity for message_versions rows via
    # the unique change_hash column. Changing any detail silently re-admits
    # duplicates of already-stored versions. Known accepted limit: repeated
    # no-edit_date edits that oscillate back to the same text reuse the same
    # fallback date and dedup into one row.
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "date": _datetime_hash_value(date),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _newer_edit_date(current: datetime | None, incoming: datetime | None) -> bool:
    current = _strip_tz(current)
    incoming = _strip_tz(incoming)
    if incoming is None:
        return False
    if current is None:
        return True
    return incoming > current

logger = logging.getLogger(__name__)


class MessageMixin:
    """Mixin providing message CRUD and query operations.

    Assumes ``self.db_manager`` and ``self._is_sqlite`` are set by the host class.
    """

    def _message_values(self, message_data: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": message_data["id"],
            "chat_id": message_data["chat_id"],
            "sender_id": message_data.get("sender_id"),
            "date": _strip_tz(message_data["date"]),
            "text": message_data.get("text"),
            "reply_to_msg_id": message_data.get("reply_to_msg_id"),
            "reply_to_top_id": message_data.get("reply_to_top_id"),
            "reply_to_text": message_data.get("reply_to_text"),
            "forward_from_id": message_data.get("forward_from_id"),
            "edit_date": _strip_tz(message_data.get("edit_date")),
            "raw_data": self._serialize_raw_data(message_data.get("raw_data", {})),
            "is_outgoing": message_data.get("is_outgoing", 0),
            "is_pinned": message_data.get("is_pinned", 0),
            "is_deleted": message_data.get("is_deleted", 0),
            "deleted_at": _strip_tz(message_data.get("deleted_at")),
        }

    def _insert_message_stmt(self, values: dict[str, Any]):
        if self._is_sqlite:
            return sqlite_insert(Message).values(**values).on_conflict_do_nothing(index_elements=["id", "chat_id"])
        return pg_insert(Message).values(**values).on_conflict_do_nothing(index_elements=["id", "chat_id"])

    def _insert_message_version_stmt(self, values: dict[str, Any]):
        if self._is_sqlite:
            return sqlite_insert(MessageVersion).values(**values).on_conflict_do_nothing(index_elements=["change_hash"])
        return pg_insert(MessageVersion).values(**values).on_conflict_do_nothing(index_elements=["change_hash"])

    async def _record_message_version(
        self,
        session,
        chat_id: int,
        message_id: int,
        text: str | None,
        date: datetime,
    ) -> bool:
        """Best-effort capture of a superseded text into message_versions.

        Versioning is plain text only — formatting/entity-only edits produce the
        same text and are intentionally not versioned. Runs inside a SAVEPOINT so
        an unexpected failure here can never poison the transaction or abort the
        message upsert/batch it belongs to (the expected duplicate case is already
        silenced by ON CONFLICT DO NOTHING on change_hash).
        """
        date = _strip_tz(date)
        if date is None:
            return False

        change_hash = _message_version_hash(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            date=date,
        )
        values = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "date": date,
            "change_hash": change_hash,
            "captured_at": utcnow_naive(),
        }
        try:
            async with session.begin_nested():
                result = await session.execute(self._insert_message_version_stmt(values))
        except Exception as e:
            logger.warning("Could not record a message version (%s); message update continues", type(e).__name__)
            return False
        return bool(result.rowcount)

    def _message_version_date(self, message: Message) -> datetime:
        return _strip_tz(message.edit_date) or _strip_tz(message.date)

    def _should_apply_upsert_text(self, existing: Message, values: dict[str, Any]) -> bool:
        """Decide whether a re-scanned/imported message may replace archived text.

        Truth table (upsert sources: backup re-scan, gap-fill, import):
        - same text            -> apply only to bump edit_date, and only when strictly
                                  newer (``>`` via _newer_edit_date) so identical
                                  replays are perfect no-ops (reaction-only edits).
        - empty -> non-empty   -> always fill (late hydration), even without an
                                  edit_date; caller preserves the existing edit_date.
        - differing text, no incoming edit_date -> refuse: an upsert source with no
                                  edit evidence must never clobber archived text.
        - differing text, incoming edit_date >= archived (or archived None) -> apply.
          ``>=`` (not ``>``) is deliberate: listener and backup can deliver the same
          edit with equal timestamps but the text seen later is the fresher fetch.
        """
        new_text = values.get("text")
        new_edit_date = _strip_tz(values.get("edit_date"))
        old_text = existing.text
        old_edit_date = _strip_tz(existing.edit_date)

        if old_text == new_text:
            return _newer_edit_date(old_edit_date, new_edit_date)
        if (old_text is None or old_text == "") and new_text not in (None, ""):
            return True
        if new_edit_date is None:
            return False
        if old_edit_date is None:
            return True
        if new_edit_date >= old_edit_date:
            return True
        return False

    def _should_apply_edit_text(self, existing: Message, new_text: str, edit_date: datetime | None) -> bool:
        """Decide whether a live edit event (listener/sync) may replace archived text.

        Differs from the upsert policy on the no-edit_date case: a live event with
        ``edit_date=None`` is applied only when the archived row was never edited —
        an already-edited row is never rolled over on date-less evidence (rare
        bot-API edits may hit this; conservative by design, covered by tests).
        """
        old_edit_date = _strip_tz(existing.edit_date)
        edit_date = _strip_tz(edit_date)

        if existing.text == new_text and old_edit_date == edit_date:
            return False
        if edit_date is None:
            return old_edit_date is None
        if old_edit_date is None:
            return True
        return edit_date >= old_edit_date

    async def _load_message_for_update(self, session, chat_id: int, message_id: int) -> Message | None:
        if self._is_sqlite:
            # SQLite has no row-level SELECT FOR UPDATE. A no-op write acquires the
            # transaction's write lock before we re-read and decide whether to update.
            await session.execute(
                update(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id)).values(id=Message.id)
            )
            stmt = select(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id))
        else:
            stmt = select(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id)).with_for_update()

        result = await session.execute(stmt.execution_options(populate_existing=True))
        return result.scalar_one_or_none()

    async def _load_message_snapshot(self, session, chat_id: int, message_id: int) -> Message | None:
        """Plain lock-free read, used only for the fast-path no-change check."""
        stmt = select(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id))
        result = await session.execute(stmt.execution_options(populate_existing=True))
        return result.scalar_one_or_none()

    def _pending_update_values(
        self, existing: Message, message_data: dict[str, Any], values: dict[str, Any]
    ) -> dict[str, Any]:
        """Columns an upsert would actually change on ``existing`` (may be empty).

        Applies the text/edit_date gating policy, then drops every key whose value
        already matches the row, so re-scanning an unchanged message performs no
        write at all. Deliberate scope note: when text is withheld (older or
        no-evidence source), the remaining metadata (raw_data, reply_to_*, sender,
        …) still refreshes from the incoming payload — non-text fields stay
        last-writer-wins exactly as before versioning existed.
        """
        update_values = _message_conflict_update_values(message_data, values)
        if self._should_apply_upsert_text(existing, values):
            if values.get("edit_date") is None and existing.edit_date is not None:
                # Text change arrived without edit evidence (e.g. late hydration):
                # keep the existing edit_date rather than nulling it.
                update_values.pop("edit_date", None)
        else:
            update_values.pop("text", None)
            update_values.pop("edit_date", None)

        changed = {}
        for key, value in update_values.items():
            if key in ("id", "chat_id"):
                continue
            if getattr(existing, key) != value:
                changed[key] = value
        return changed

    async def _apply_existing_message_update(
        self,
        session,
        message_data: dict[str, Any],
        values: dict[str, Any],
    ) -> None:
        # Fast path: a lock-free read to detect the common re-scan case of a fully
        # unchanged message, so full re-backups don't pay the write lock + extra
        # statements per row. The definitive decision is re-made under the lock
        # below; skipping here is safe because a concurrent writer that changes the
        # row after our snapshot has, by definition, applied data at least as new
        # as ours.
        snapshot = await self._load_message_snapshot(session, values["chat_id"], values["id"])
        if snapshot is None:
            logger.debug("Upsert no-op: message row vanished during conflict resolution")
            return
        if not self._pending_update_values(snapshot, message_data, values):
            return

        existing = await self._load_message_for_update(session, values["chat_id"], values["id"])
        if existing is None:
            logger.debug("Upsert no-op: message row vanished during conflict resolution")
            return

        update_values = self._pending_update_values(existing, message_data, values)
        if not update_values:
            return
        if "text" in update_values:
            await self._record_message_version(
                session=session,
                chat_id=existing.chat_id,
                message_id=existing.id,
                text=existing.text,
                date=self._message_version_date(existing),
            )
        await session.execute(
            update(Message)
            .where(and_(Message.chat_id == values["chat_id"], Message.id == values["id"]))
            .values(**update_values)
        )

    async def _insert_or_update_message(self, session, message_data: dict[str, Any]) -> None:
        values = self._message_values(message_data)
        result = await session.execute(self._insert_message_stmt(values))
        if result.rowcount:
            return

        await self._apply_existing_message_update(session, message_data, values)

    @retry_on_locked()
    async def insert_message(self, message_data: dict[str, Any]) -> None:
        """Insert a message record.

        v6.0.0: media_type, media_id, media_path removed - use insert_media() separately.
        """
        async with self.db_manager.async_session_factory() as session:
            await self._insert_or_update_message(session, message_data)
            await session.commit()

    @retry_on_locked()
    async def insert_messages_batch(self, messages_data: list[dict[str, Any]]) -> None:
        """Insert multiple message records in a single transaction.

        v6.0.0: media_type, media_id, media_path removed - use insert_media() separately.
        """
        if not messages_data:
            return

        async with self.db_manager.async_session_factory() as session:
            for m in messages_data:
                await self._insert_or_update_message(session, m)

            await session.commit()

    async def get_messages_paginated(
        self,
        chat_id: int,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
        before_date: datetime | None = None,
        before_id: int | None = None,
        after_date: datetime | None = None,
        after_id: int | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        topic_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get messages with user info and media info for web viewer.

        v6.0.0: Media is now returned as a nested object from the media table.
        v6.2.0: Added topic_id filter for forum topic messages.

        Supports three pagination modes:
        1. Offset-based (legacy): Uses offset parameter - slower for large offsets
        2. Cursor backward: Uses before_date/before_id - O(1), older messages
        3. Cursor forward: Uses after_date/after_id - O(1), newer messages

        Args:
            chat_id: Chat ID
            limit: Maximum messages to return
            offset: Pagination offset (used only if no cursor provided)
            search: Optional text search filter
            before_date: Cursor - get messages before this date (backward)
            before_id: Cursor - tiebreaker for same-date messages (backward)
            after_date: Cursor - get messages after this date (forward)
            after_id: Cursor - tiebreaker for same-date messages (forward)
            date_from: Filter - only messages on or after this date
            date_to: Filter - only messages on or before this date
            topic_id: Optional forum topic ID to filter messages by thread

        Returns:
            List of message dictionaries with user and media info
        """
        async with self.db_manager.async_session_factory() as session:
            # Build query with joins - v6.0.0: join on composite key
            stmt = (
                select(
                    Message,
                    User.first_name,
                    User.last_name,
                    User.username,
                    Media.id.label("media_id"),
                    Media.type.label("media_type"),
                    Media.file_path.label("media_file_path"),
                    Media.file_name.label("media_file_name"),
                    Media.file_size.label("media_file_size"),
                    Media.mime_type.label("media_mime_type"),
                    Media.width.label("media_width"),
                    Media.height.label("media_height"),
                    Media.duration.label("media_duration"),
                )
                .outerjoin(User, Message.sender_id == User.id)
                .outerjoin(Media, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
                .where(Message.chat_id == chat_id)
            )

            # v6.2.0: Filter by forum topic.
            # General topic (topic_id=1) must include pre-forum messages that have
            # NULL reply_to_top_id as well as messages with an explicit value of 1.
            # coalesce(reply_to_top_id, 1) == topic_id keeps non-General topics
            # strict (NULL coalesces to 1, which only matches topic_id=1).
            if topic_id is not None:
                stmt = stmt.where(func.coalesce(Message.reply_to_top_id, 1) == topic_id)

            if search:
                digits_only = re.sub(r'[^\d]', '', search)
                is_numeric = bool(digits_only) and len(digits_only) >= 3 and bool(re.match(r'^[\d,.\s]+$', search.strip()))
                if is_numeric:
                    normalized = func.replace(func.replace(func.replace(Message.text, ',', ''), '.', ''), ' ', '')
                    stmt = stmt.where(normalized.contains(digits_only))
                else:
                    escaped = search.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
                    stmt = stmt.where(Message.text.ilike(f"%{escaped}%", escape="\\"))

            # Date range filtering (applies on top of any pagination mode)
            if date_from is not None:
                stmt = stmt.where(Message.date >= date_from)
            if date_to is not None:
                stmt = stmt.where(Message.date <= date_to)

            # Cursor-based pagination (preferred - O(1) performance)
            if before_date is not None:
                # Use composite cursor: (date, id) for deterministic ordering
                # Messages with same date are ordered by id DESC
                if before_id is not None:
                    stmt = stmt.where(
                        or_(Message.date < before_date, and_(Message.date == before_date, Message.id < before_id))
                    )
                else:
                    stmt = stmt.where(Message.date < before_date)
                stmt = stmt.order_by(Message.date.desc(), Message.id.desc()).limit(limit)
            elif after_date is not None:
                # Forward cursor: get messages NEWER than cursor
                if after_id is not None:
                    stmt = stmt.where(
                        or_(Message.date > after_date, and_(Message.date == after_date, Message.id > after_id))
                    )
                else:
                    stmt = stmt.where(Message.date > after_date)
                # Forward: oldest first so we get the NEXT messages chronologically
                stmt = stmt.order_by(Message.date.asc(), Message.id.asc()).limit(limit)
            else:
                # Offset-based pagination (legacy fallback)
                stmt = stmt.order_by(Message.date.desc()).limit(limit).offset(offset)

            result = await session.execute(stmt)
            messages = []

            for row in result:
                msg = self._message_to_dict(row.Message)
                msg["first_name"] = row.first_name
                msg["last_name"] = row.last_name
                msg["username"] = row.username

                # v6.0.0: Media as nested object
                if row.media_type:
                    msg["media"] = {
                        "id": row.media_id,
                        "type": row.media_type,
                        "file_path": row.media_file_path,
                        "file_name": row.media_file_name,
                        "file_size": row.media_file_size,
                        "mime_type": row.media_mime_type,
                        "width": row.media_width,
                        "height": row.media_height,
                        "duration": row.media_duration,
                    }
                else:
                    msg["media"] = None

                # Parse raw_data JSON
                if msg.get("raw_data"):
                    try:
                        msg["raw_data"] = json.loads(msg["raw_data"])
                    except:
                        msg["raw_data"] = {}

                messages.append(msg)

            version_counts = {msg["id"]: 0 for msg in messages}
            version_count_message_ids = [msg["id"] for msg in messages]
            if version_count_message_ids:
                count_stmt = (
                    select(MessageVersion.message_id, func.count(MessageVersion.id).label("version_count"))
                    .where(
                        and_(
                            MessageVersion.chat_id == chat_id,
                            MessageVersion.message_id.in_(version_count_message_ids),
                        )
                    )
                    .group_by(MessageVersion.message_id)
                )
                count_result = await session.execute(count_stmt)
                version_counts.update({row.message_id: int(row.version_count or 0) for row in count_result})

            # Get reply texts and reactions for each message
            for msg in messages:
                msg["version_count"] = version_counts.get(msg["id"], 0)

                if msg.get("reply_to_msg_id") and not msg.get("reply_to_text"):
                    reply_result = await session.execute(
                        select(Message.text).where(
                            and_(Message.chat_id == chat_id, Message.id == msg["reply_to_msg_id"])
                        )
                    )
                    reply_text = reply_result.scalar_one_or_none()
                    if reply_text:
                        msg["reply_to_text"] = reply_text[:100]

                # Get reactions
                reactions = await self.get_reactions(msg["id"], chat_id)
                reactions_by_emoji = {}
                for reaction in reactions:
                    emoji = reaction["emoji"]
                    if emoji not in reactions_by_emoji:
                        reactions_by_emoji[emoji] = {"emoji": emoji, "count": 0, "user_ids": []}
                    reactions_by_emoji[emoji]["count"] += reaction.get("count", 1)
                    if reaction.get("user_id"):
                        reactions_by_emoji[emoji]["user_ids"].append(reaction["user_id"])
                msg["reactions"] = list(reactions_by_emoji.values())

            return messages

    async def get_messages_around(
        self,
        chat_id: int,
        msg_id: int,
        count: int = 50,
    ) -> dict[str, Any]:
        """
        Get messages around a target message for permalink navigation.

        Fetches count/2 messages before and after the target message's date,
        returning them with boundary flags for bidirectional loading.

        Args:
            chat_id: Chat ID
            msg_id: Target message ID
            count: Total number of messages to return (split evenly before/after)

        Returns:
            Dict with messages list and boundary flags, or empty dict if not found
        """
        half = count // 2
        async with self.db_manager.async_session_factory() as session:
            # Step 1: Find target message date
            target_result = await session.execute(
                select(Message.date).where(
                    and_(Message.chat_id == chat_id, Message.id == msg_id)
                )
            )
            target_date = target_result.scalar_one_or_none()
            if target_date is None:
                return {}

            # Base select with joins (same as get_messages_paginated)
            base_stmt = (
                select(
                    Message,
                    User.first_name,
                    User.last_name,
                    User.username,
                    Media.id.label("media_id"),
                    Media.type.label("media_type"),
                    Media.file_path.label("media_file_path"),
                    Media.file_name.label("media_file_name"),
                    Media.file_size.label("media_file_size"),
                    Media.mime_type.label("media_mime_type"),
                    Media.width.label("media_width"),
                    Media.height.label("media_height"),
                    Media.duration.label("media_duration"),
                )
                .outerjoin(User, Message.sender_id == User.id)
                .outerjoin(
                    Media,
                    and_(
                        Media.message_id == Message.id,
                        Media.chat_id == Message.chat_id,
                    ),
                )
                .where(Message.chat_id == chat_id)
            )

            # Step 2: Get messages before target (older), ordered newest-first
            before_stmt = (
                base_stmt.where(
                    or_(
                        Message.date < target_date,
                        and_(Message.date == target_date, Message.id < msg_id),
                    )
                )
                .order_by(Message.date.desc(), Message.id.desc())
                .limit(half)
            )

            # Step 3: Get target + messages after (newer), ordered oldest-first
            after_stmt = (
                base_stmt.where(
                    or_(
                        Message.date > target_date,
                        and_(Message.date == target_date, Message.id >= msg_id),
                    )
                )
                .order_by(Message.date.asc(), Message.id.asc())
                .limit(half)
            )

            before_result = await session.execute(before_stmt)
            after_result = await session.execute(after_stmt)

            # Combine: before (reversed to chronological) + after
            before_rows = list(before_result)
            after_rows = list(after_result)
            all_rows = list(reversed(before_rows)) + after_rows

            messages = []
            for row in all_rows:
                msg = self._message_to_dict(row.Message)
                msg["first_name"] = row.first_name
                msg["last_name"] = row.last_name
                msg["username"] = row.username

                if row.media_type:
                    msg["media"] = {
                        "id": row.media_id,
                        "type": row.media_type,
                        "file_path": row.media_file_path,
                        "file_name": row.media_file_name,
                        "file_size": row.media_file_size,
                        "mime_type": row.media_mime_type,
                        "width": row.media_width,
                        "height": row.media_height,
                        "duration": row.media_duration,
                    }
                else:
                    msg["media"] = None

                if msg.get("raw_data"):
                    try:
                        msg["raw_data"] = json.loads(msg["raw_data"])
                    except Exception:
                        msg["raw_data"] = {}

                messages.append(msg)

            # Enrich with reply texts and reactions
            for msg in messages:
                if msg.get("reply_to_msg_id") and not msg.get("reply_to_text"):
                    reply_result = await session.execute(
                        select(Message.text).where(
                            and_(
                                Message.chat_id == chat_id,
                                Message.id == msg["reply_to_msg_id"],
                            )
                        )
                    )
                    reply_text = reply_result.scalar_one_or_none()
                    if reply_text:
                        msg["reply_to_text"] = reply_text[:100]

                reactions = await self.get_reactions(msg["id"], chat_id)
                reactions_by_emoji: dict[str, dict] = {}
                for reaction in reactions:
                    emoji = reaction["emoji"]
                    if emoji not in reactions_by_emoji:
                        reactions_by_emoji[emoji] = {
                            "emoji": emoji,
                            "count": 0,
                            "user_ids": [],
                        }
                    reactions_by_emoji[emoji]["count"] += reaction.get("count", 1)
                    if reaction.get("user_id"):
                        reactions_by_emoji[emoji]["user_ids"].append(
                            reaction["user_id"]
                        )
                msg["reactions"] = list(reactions_by_emoji.values())

            return {
                "messages": messages,
                "has_more_older": len(before_rows) == half,
                "has_more_newer": len(after_rows) == half,
                "target_msg_id": msg_id,
            }

    async def get_messages_by_date_range(
        self, chat_id: int | None = None, start_date: datetime | None = None, end_date: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Get messages within a date range."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Message)

            conditions = []
            if chat_id:
                conditions.append(Message.chat_id == chat_id)
            if start_date:
                conditions.append(Message.date >= start_date)
            if end_date:
                conditions.append(Message.date <= end_date)

            if conditions:
                stmt = stmt.where(and_(*conditions))

            stmt = stmt.order_by(Message.date.asc())

            result = await session.execute(stmt)
            return [self._message_to_dict(m) for m in result.scalars()]

    async def find_message_by_date(self, chat_id: int, target_date: datetime) -> dict[str, Any] | None:
        """Find the first message on or after a specific date."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Message)
                .where(and_(Message.chat_id == chat_id, Message.date >= target_date))
                .order_by(Message.date.asc())
                .limit(1)
            )
            result = await session.execute(stmt)
            message = result.scalar_one_or_none()
            return self._message_to_dict(message) if message else None

    async def find_message_by_date_with_joins(self, chat_id: int, target_date: datetime) -> dict[str, Any] | None:
        """
        Find message by date with full user/media joins for web viewer.

        v6.0.0: Media is now returned as a nested object from the media table.

        Args:
            chat_id: Chat ID
            target_date: Target date to find message for

        Returns:
            Message dictionary with user and media info, or None
        """
        async with self.db_manager.async_session_factory() as session:
            base_stmt = (
                select(
                    Message,
                    User.first_name,
                    User.last_name,
                    User.username,
                    Media.id.label("media_id"),
                    Media.type.label("media_type"),
                    Media.file_path.label("media_file_path"),
                    Media.file_name.label("media_file_name"),
                    Media.file_size.label("media_file_size"),
                    Media.mime_type.label("media_mime_type"),
                    Media.width.label("media_width"),
                    Media.height.label("media_height"),
                    Media.duration.label("media_duration"),
                )
                .outerjoin(User, Message.sender_id == User.id)
                .outerjoin(Media, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
                .where(Message.chat_id == chat_id)
            )

            # Try on or after target date
            stmt = base_stmt.where(Message.date >= target_date).order_by(Message.date.asc()).limit(1)
            result = await session.execute(stmt)
            row = result.first()

            if not row:
                # Try before target date
                stmt = base_stmt.where(Message.date < target_date).order_by(Message.date.desc()).limit(1)
                result = await session.execute(stmt)
                row = result.first()

            if not row:
                # Try first message in chat
                stmt = base_stmt.order_by(Message.date.asc()).limit(1)
                result = await session.execute(stmt)
                row = result.first()

            if not row:
                return None

            msg = self._message_to_dict(row.Message)
            msg["first_name"] = row.first_name
            msg["last_name"] = row.last_name
            msg["username"] = row.username

            # v6.0.0: Media as nested object
            if row.media_type:
                msg["media"] = {
                    "id": row.media_id,
                    "type": row.media_type,
                    "file_path": row.media_file_path,
                    "file_name": row.media_file_name,
                    "file_size": row.media_file_size,
                    "mime_type": row.media_mime_type,
                    "width": row.media_width,
                    "height": row.media_height,
                    "duration": row.media_duration,
                }
            else:
                msg["media"] = None

            # Parse raw_data
            if msg.get("raw_data"):
                try:
                    msg["raw_data"] = json.loads(msg["raw_data"])
                except:
                    msg["raw_data"] = {}

            # Get reply text
            if msg.get("reply_to_msg_id") and not msg.get("reply_to_text"):
                reply_result = await session.execute(
                    select(Message.text).where(and_(Message.chat_id == chat_id, Message.id == msg["reply_to_msg_id"]))
                )
                reply_text = reply_result.scalar_one_or_none()
                if reply_text:
                    msg["reply_to_text"] = reply_text[:100]

            # Get reactions
            reactions = await self.get_reactions(msg["id"], chat_id)
            reactions_by_emoji = {}
            for reaction in reactions:
                emoji = reaction["emoji"]
                if emoji not in reactions_by_emoji:
                    reactions_by_emoji[emoji] = {"emoji": emoji, "count": 0, "user_ids": []}
                reactions_by_emoji[emoji]["count"] += reaction.get("count", 1)
                if reaction.get("user_id"):
                    reactions_by_emoji[emoji]["user_ids"].append(reaction["user_id"])
            msg["reactions"] = list(reactions_by_emoji.values())

            return msg

    @retry_on_locked()
    async def delete_message(self, chat_id: int, message_id: int) -> None:
        """Delete a specific message and its media."""
        async with self.db_manager.async_session_factory() as session:
            # Delete previous versions
            await session.execute(
                delete(MessageVersion).where(
                    and_(MessageVersion.chat_id == chat_id, MessageVersion.message_id == message_id)
                )
            )
            # Delete associated media
            await session.execute(delete(Media).where(and_(Media.chat_id == chat_id, Media.message_id == message_id)))
            # Delete reactions
            await session.execute(
                delete(Reaction).where(and_(Reaction.chat_id == chat_id, Reaction.message_id == message_id))
            )
            # Delete the message
            await session.execute(delete(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id)))
            await session.commit()
            logger.debug(f"Deleted message {message_id} from chat {chat_id}")

    @retry_on_locked()
    async def mark_message_deleted(self, chat_id: int, message_id: int, deleted_at: datetime | None = None) -> None:
        """Mark a message as deleted on Telegram while keeping archive content."""
        deleted_at = _strip_tz(deleted_at) or utcnow_naive()
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                update(Message)
                .where(and_(Message.chat_id == chat_id, Message.id == message_id))
                .values(
                    is_deleted=1,
                    deleted_at=func.coalesce(Message.deleted_at, deleted_at),
                )
            )
            await session.commit()
            if result.rowcount:
                logger.debug(f"Marked message {message_id} as deleted")
            else:
                logger.debug(f"Soft-delete no-op: message {message_id} not in archive")

    async def delete_message_by_id_any_chat(self, message_id: int) -> bool:
        """
        Delete a message by ID when chat is unknown.

        This is used by the real-time listener when Telegram sends deletion
        events without specifying the chat (can happen in some edge cases).

        Args:
            message_id: The message ID to delete

        Returns:
            True if a message was deleted, False otherwise
        """
        async with self.db_manager.async_session_factory() as session:
            # First, find which chat(s) have this message
            result = await session.execute(select(Message.chat_id).where(Message.id == message_id))
            chat_ids = [row[0] for row in result.fetchall()]

            if not chat_ids:
                return False

            # Delete from all matching chats (usually just one)
            for chat_id in chat_ids:
                # Delete associated media
                await session.execute(
                    delete(Media).where(and_(Media.chat_id == chat_id, Media.message_id == message_id))
                )
                # Delete reactions
                await session.execute(
                    delete(Reaction).where(and_(Reaction.chat_id == chat_id, Reaction.message_id == message_id))
                )
                # Delete the message
                await session.execute(delete(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id)))

            await session.commit()
            logger.debug(f"Deleted message {message_id} from {len(chat_ids)} chat(s)")
            return True

    @retry_on_locked()
    async def update_message_text(
        self, chat_id: int, message_id: int, new_text: str, edit_date: datetime | None
    ) -> str:
        """Update a message's text and edit_date.

        Returns the outcome so callers can keep honest counters and only
        broadcast edits that actually changed the archive:
        ``"applied"`` | ``"noop"`` (already current / older evidence) |
        ``"not_found"`` (message not archived).
        """
        edit_date = _strip_tz(edit_date)
        async with self.db_manager.async_session_factory() as session:
            message = await self._load_message_for_update(session, chat_id, message_id)
            if message is None:
                logger.debug("Edit no-op: message not found in archive")
                return "not_found"

            if not self._should_apply_edit_text(message, new_text, edit_date):
                logger.debug("Edit no-op: message already current")
                return "noop"

            if message.text != new_text:
                await self._record_message_version(
                    session=session,
                    chat_id=chat_id,
                    message_id=message_id,
                    text=message.text,
                    date=self._message_version_date(message),
                )
            await session.execute(
                update(Message)
                .where(and_(Message.chat_id == chat_id, Message.id == message_id))
                .values(text=new_text, edit_date=edit_date)
            )
            await session.commit()
            logger.debug("Updated archived message text")
            return "applied"

    async def backfill_is_outgoing(self, owner_id: int) -> None:
        """Backfill is_outgoing flag for messages sent by the owner."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                update(Message)
                .where(
                    and_(Message.sender_id == owner_id, or_(Message.is_outgoing == 0, Message.is_outgoing.is_(None)))
                )
                .values(is_outgoing=1)
            )
            await session.commit()
            if result.rowcount > 0:
                logger.info(f"Backfilled is_outgoing=1 for {result.rowcount} messages from owner {owner_id}")

    def _message_to_dict(self, message: Message) -> dict[str, Any]:
        """Convert Message model to dictionary.

        v6.0.0: media_type, media_id, media_path removed - use media_items relationship.
        """
        is_deleted = getattr(message, "is_deleted", 0)
        if not isinstance(is_deleted, int):
            is_deleted = 0
        deleted_at = getattr(message, "deleted_at", None)
        if not isinstance(deleted_at, datetime):
            deleted_at = None

        return {
            "id": message.id,
            "chat_id": message.chat_id,
            "sender_id": message.sender_id,
            "date": message.date,
            "text": message.text,
            "reply_to_msg_id": message.reply_to_msg_id,
            "reply_to_top_id": message.reply_to_top_id,
            "reply_to_text": message.reply_to_text,
            "forward_from_id": message.forward_from_id,
            "edit_date": message.edit_date,
            "raw_data": message.raw_data,
            "created_at": message.created_at,
            "is_outgoing": message.is_outgoing,
            "is_pinned": message.is_pinned,
            "is_deleted": int(is_deleted),
            "deleted_at": deleted_at,
            "ai_comment": message.ai_comment,
            "ocr_text": message.ocr_text,
        }

    def _message_version_to_dict(self, row: MessageVersion) -> dict[str, Any]:
        return {
            "chat_id": row.chat_id,
            "message_id": row.message_id,
            "text": row.text,
            "date": row.date,
        }

    async def get_message_versions(self, chat_id: int, message_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """Get preserved previous text versions for a message."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(MessageVersion)
                .where(and_(MessageVersion.chat_id == chat_id, MessageVersion.message_id == message_id))
                .order_by(MessageVersion.date.desc(), MessageVersion.id.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [self._message_version_to_dict(row) for row in result.scalars()]

    def _message_versions_query(
        self,
        chat_id: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ):
        # No join to messages: versions already carry (chat_id, message_id), and
        # referential integrity is owned by the explicit deletes in
        # delete_message / delete_chat_and_related_data.
        stmt = select(MessageVersion)

        conditions = []
        if chat_id is not None:
            conditions.append(MessageVersion.chat_id == chat_id)
        if start_date:
            conditions.append(MessageVersion.date >= start_date)
        if end_date:
            conditions.append(MessageVersion.date <= end_date)
        if conditions:
            stmt = stmt.where(and_(*conditions))

        return stmt.order_by(
            MessageVersion.chat_id.asc(),
            MessageVersion.message_id.asc(),
            MessageVersion.date.asc(),
            MessageVersion.id.asc(),
        )

    async def get_message_versions_by_date_range(
        self,
        chat_id: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Get previous message versions by version date/chat filter."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(self._message_versions_query(chat_id, start_date, end_date))
            return [self._message_version_to_dict(row) for row in result.scalars()]

    async def iter_message_versions_for_export(self, chat_id: int):
        """Stream a chat's message versions one by one (async generator).

        Mirrors get_messages_for_export so the export endpoint never
        materializes an entire edit history in memory.
        """
        async with self.db_manager.async_session_factory() as session:
            result = await session.stream(self._message_versions_query(chat_id))
            async for row in result.scalars():
                yield self._message_version_to_dict(row)

    async def get_messages_for_export(self, chat_id: int, include_media: bool = False):
        """
        Get messages for export with user info.
        Returns an async generator for streaming.

        v6.0.0: Media info now comes from the media table via JOIN.

        Args:
            chat_id: Chat ID to export
            include_media: If True, include media info from media table

        Yields:
            Message dictionaries with user info
        """
        async with self.db_manager.async_session_factory() as session:
            if include_media:
                stmt = (
                    select(
                        Message.id,
                        Message.date,
                        Message.text,
                        Message.is_outgoing,
                        Message.reply_to_msg_id,
                        Media.type.label("media_type"),
                        Media.file_path.label("media_file_path"),
                        User.first_name,
                        User.last_name,
                        User.username,
                    )
                    .outerjoin(User, Message.sender_id == User.id)
                    .outerjoin(Media, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
                    .where(Message.chat_id == chat_id)
                    .order_by(Message.date.asc())
                )
            else:
                stmt = (
                    select(
                        Message.id,
                        Message.date,
                        Message.text,
                        Message.is_outgoing,
                        Message.reply_to_msg_id,
                        User.first_name,
                        User.last_name,
                        User.username,
                    )
                    .outerjoin(User, Message.sender_id == User.id)
                    .where(Message.chat_id == chat_id)
                    .order_by(Message.date.asc())
                )

            result = await session.stream(stmt)
            async for row in result:
                msg = {
                    "id": row.id,
                    "date": row.date.isoformat() if hasattr(row.date, 'isoformat') else row.date,
                    "sender": {
                        "name": f"{row.first_name or ''} {row.last_name or ''}".strip() or row.username or "Unknown",
                        "username": row.username,
                    },
                    "text": row.text,
                    "is_outgoing": bool(row.is_outgoing),
                    "reply_to": row.reply_to_msg_id,
                }
                if include_media:
                    msg["media_type"] = row.media_type
                    msg["media_path"] = row.media_file_path
                yield msg

    async def get_boundary_message_id(self, chat_id: int, direction: str = "first") -> int | None:
        """Return the oldest or newest message ID for a chat.

        Args:
            chat_id: Chat ID to query
            direction: 'first' for oldest, 'last' for newest

        Returns:
            Message ID or None if chat has no messages
        """
        order = Message.date.asc() if direction == "first" else Message.date.desc()
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Message.id)
                .where(Message.chat_id == chat_id)
                .order_by(order)
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row

    async def get_last_message_id(self, chat_id: int) -> int:
        """Get the last synced message ID for a chat."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(SyncStatus.last_message_id).where(SyncStatus.chat_id == chat_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row if row else 0

    async def get_all_last_message_ids(self) -> dict[int, int]:
        """Bulk-load {chat_id: last_message_id} for all synced chats."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(SyncStatus.chat_id, SyncStatus.last_message_id)
            result = await session.execute(stmt)
            return {row.chat_id: row.last_message_id for row in result}

    async def get_chat_stats(self, chat_id: int) -> dict[str, Any]:
        """Get statistics for a specific chat (message count, media count, total size).

        Returns:
            Dict with keys: messages, media_files, total_size_bytes, first_message_date, last_message_date
        """
        async with self.db_manager.async_session_factory() as session:
            # Message count
            msg_result = await session.execute(select(func.count(Message.id)).where(Message.chat_id == chat_id))
            message_count = msg_result.scalar() or 0

            # Media count and total size
            media_result = await session.execute(
                select(func.count(Media.id), func.coalesce(func.sum(Media.file_size), 0)).where(
                    Media.chat_id == chat_id
                )
            )
            media_row = media_result.one()
            media_count = media_row[0] or 0
            total_size = media_row[1] or 0

            # First and last message dates
            date_result = await session.execute(
                select(func.min(Message.date), func.max(Message.date)).where(Message.chat_id == chat_id)
            )
            date_row = date_result.one()
            first_message = date_row[0]
            last_message = date_row[1]

            return {
                "chat_id": chat_id,
                "messages": int(message_count),
                "media_files": int(media_count),
                "total_size_bytes": int(total_size),
                "total_size_mb": round(total_size / (1024 * 1024), 2) if total_size else 0,
                "first_message_date": first_message.isoformat() if first_message else None,
                "last_message_date": last_message.isoformat() if last_message else None,
            }

    async def get_messages_sync_data(self, chat_id: int) -> dict[int, str | None]:
        """Get message IDs and their edit dates for sync checking."""
        async with self.db_manager.async_session_factory() as session:
            # Exclude soft-deleted rows so sync doesn't re-check them. The is_(None) arm is
            # defensive (is_deleted is NOT NULL with server_default 0) and mirrors is_archived.
            stmt = select(Message.id, Message.edit_date).where(
                and_(Message.chat_id == chat_id, or_(Message.is_deleted == 0, Message.is_deleted.is_(None)))
            )
            result = await session.execute(stmt)
            return {row.id: row.edit_date for row in result}

    async def get_chat_id_for_message(self, message_id: int) -> int | None:
        """
        Look up the chat_id for a message by its ID.

        Used when Telegram sends deletion events without chat_id.
        Note: Message IDs are only unique within a chat, so this may return
        multiple results. Returns the first match.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Message.chat_id).where(Message.id == message_id).limit(1)
            result = await session.execute(stmt)
            row = result.first()
            return row[0] if row else None
