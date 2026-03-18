"""Message-related database operations mixin.

Handles insert, query, update, delete for messages and related helpers.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, update

from .adapter import _strip_tz, retry_on_locked
from .models import Media, Message, Reaction, SyncStatus, User

logger = logging.getLogger(__name__)


class MessageMixin:
    """Mixin providing message CRUD and query operations.

    Assumes ``self.db_manager`` and ``self._is_sqlite`` are set by the host class.
    """

    async def insert_message(self, message_data: dict[str, Any]) -> None:
        """Insert a message record.

        v6.0.0: media_type, media_id, media_path removed - use insert_media() separately.
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        async with self.db_manager.async_session_factory() as session:
            values = {
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
            }

            if self._is_sqlite:
                stmt = sqlite_insert(Message).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id", "chat_id"], set_=values)
            else:
                stmt = pg_insert(Message).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id", "chat_id"], set_=values)

            await session.execute(stmt)
            await session.commit()

    @retry_on_locked()
    async def insert_messages_batch(self, messages_data: list[dict[str, Any]]) -> None:
        """Insert multiple message records in a single transaction.

        v6.0.0: media_type, media_id, media_path removed - use insert_media() separately.
        """
        if not messages_data:
            return

        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        async with self.db_manager.async_session_factory() as session:
            for m in messages_data:
                values = {
                    "id": m["id"],
                    "chat_id": m["chat_id"],
                    "sender_id": m.get("sender_id"),
                    "date": _strip_tz(m["date"]),
                    "text": m.get("text"),
                    "reply_to_msg_id": m.get("reply_to_msg_id"),
                    "reply_to_top_id": m.get("reply_to_top_id"),
                    "reply_to_text": m.get("reply_to_text"),
                    "forward_from_id": m.get("forward_from_id"),
                    "edit_date": _strip_tz(m.get("edit_date")),
                    "raw_data": self._serialize_raw_data(m.get("raw_data", {})),
                    "is_outgoing": m.get("is_outgoing", 0),
                    "is_pinned": m.get("is_pinned", 0),
                }

                if self._is_sqlite:
                    stmt = sqlite_insert(Message).values(**values)
                    stmt = stmt.on_conflict_do_update(index_elements=["id", "chat_id"], set_=values)
                else:
                    stmt = pg_insert(Message).values(**values)
                    stmt = stmt.on_conflict_do_update(index_elements=["id", "chat_id"], set_=values)

                await session.execute(stmt)

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

            # v6.2.0: Filter by forum topic
            if topic_id is not None:
                stmt = stmt.where(Message.reply_to_top_id == topic_id)

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

            # Get reply texts and reactions for each message
            for msg in messages:
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

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        """Delete a specific message and its media."""
        async with self.db_manager.async_session_factory() as session:
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

    async def update_message_text(
        self, chat_id: int, message_id: int, new_text: str, edit_date: datetime | None
    ) -> None:
        """Update a message's text and edit_date."""
        async with self.db_manager.async_session_factory() as session:
            await session.execute(
                update(Message)
                .where(and_(Message.chat_id == chat_id, Message.id == message_id))
                .values(text=new_text, edit_date=_strip_tz(edit_date))
            )
            await session.commit()
            logger.debug(f"Updated message {message_id} in chat {chat_id}")

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
            "ai_comment": message.ai_comment,
            "ocr_text": message.ocr_text,
        }

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
            stmt = select(Message.id, Message.edit_date).where(Message.chat_id == chat_id)
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
