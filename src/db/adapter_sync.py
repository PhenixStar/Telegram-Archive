"""Sync, chat, user, folder, backup profile, and statistics operations mixin.

Handles chat/user upserts, sync status, gap detection, statistics,
pinned messages, forum topics, chat folders, backup profiles, chat members,
and message density.
"""

import glob
import logging
import os
import shutil
from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, update

from .adapter import _strip_tz, retry_on_locked
from .models import (
    BackupProfile,
    Chat,
    ChatFolder,
    ChatFolderMember,
    ForumTopic,
    Media,
    Message,
    Reaction,
    SyncStatus,
    User,
)

logger = logging.getLogger(__name__)


class SyncMixin:
    """Mixin providing sync, chat, folder, and statistics operations.

    Assumes ``self.db_manager`` and ``self._is_sqlite`` are set by the host class.
    """

    # ========== Chat Operations ==========

    @retry_on_locked()
    async def upsert_chat(self, chat_data: dict[str, Any]) -> int:
        """Insert or update a chat record.

        Only fields present in chat_data will be updated on conflict.
        This prevents the listener (which only provides basic fields)
        from overwriting is_forum/is_archived set by the backup.
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        async with self.db_manager.async_session_factory() as session:
            values = {
                "id": chat_data["id"],
                "type": chat_data.get("type", "unknown"),
                "title": chat_data.get("title"),
                "username": chat_data.get("username"),
                "first_name": chat_data.get("first_name"),
                "last_name": chat_data.get("last_name"),
                "phone": chat_data.get("phone"),
                "description": chat_data.get("description"),
                "participants_count": chat_data.get("participants_count"),
                "is_forum": chat_data.get("is_forum", 0),
                "is_archived": chat_data.get("is_archived", 0),
                "updated_at": datetime.utcnow(),
            }

            # Build update set from only the fields explicitly provided in chat_data.
            # This prevents partial upserts (e.g. from the listener) from resetting
            # is_forum/is_archived to their defaults.
            update_set = {
                "updated_at": datetime.utcnow(),
            }
            # Always update these basic metadata fields
            for field in (
                "type",
                "title",
                "username",
                "first_name",
                "last_name",
                "phone",
                "description",
                "participants_count",
            ):
                if field in chat_data:
                    update_set[field] = values[field]
            # Only update is_forum/is_archived if explicitly provided
            if "is_forum" in chat_data:
                update_set["is_forum"] = values["is_forum"]
            if "is_archived" in chat_data:
                update_set["is_archived"] = values["is_archived"]

            if self._is_sqlite:
                stmt = sqlite_insert(Chat).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_set)
            else:
                stmt = pg_insert(Chat).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_set)

            await session.execute(stmt)
            await session.commit()
            return chat_data["id"]

    async def get_all_chats(
        self,
        limit: int = None,
        offset: int = 0,
        search: str = None,
        archived: bool | None = None,
        folder_id: int | None = None,
        folder_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Get chats with their last message date, with optional pagination and search.

        Args:
            limit: Maximum number of chats to return
            offset: Offset for pagination
            search: Optional search query (case-insensitive, matches title/first_name/last_name/username)
            archived: If True, only archived chats; if False, only non-archived; if None, all
            folder_id: If set, only chats in this folder
            folder_ids: If set, chats in any of these folders (union)
        """
        async with self.db_manager.async_session_factory() as session:
            # Subquery for last message date
            subq = (
                select(Message.chat_id, func.max(Message.date).label("last_message_date"))
                .group_by(Message.chat_id)
                .subquery()
            )

            # v8.1: Correlated scalar subqueries for last message preview
            # Uses idx_messages_chat_date_desc index — O(1) per chat
            last_msg_text = (
                select(Message.text)
                .where(Message.chat_id == Chat.id)
                .order_by(Message.date.desc())
                .limit(1)
                .correlate(Chat)
                .scalar_subquery()
                .label("last_message_text")
            )
            last_msg_sender = (
                select(User.first_name)
                .select_from(Message.__table__.join(User.__table__, Message.sender_id == User.id))
                .where(Message.chat_id == Chat.id)
                .order_by(Message.date.desc())
                .limit(1)
                .correlate(Chat)
                .scalar_subquery()
                .label("last_message_sender")
            )
            _latest_msg_id = (
                select(Message.id)
                .where(Message.chat_id == Chat.id)
                .order_by(Message.date.desc())
                .limit(1)
                .correlate(Chat)
                .scalar_subquery()
            )
            last_msg_id = _latest_msg_id.label("last_message_id")
            # v8.1: Media type of the latest message (for "Photo", "Video" preview fallback)
            last_msg_media_type = (
                select(Media.type)
                .where(and_(Media.chat_id == Chat.id, Media.message_id == _latest_msg_id))
                .limit(1)
                .correlate(Chat)
                .scalar_subquery()
                .label("last_message_media_type")
            )

            stmt = select(
                Chat, subq.c.last_message_date, last_msg_text, last_msg_sender, last_msg_id, last_msg_media_type
            ).outerjoin(
                subq, Chat.id == subq.c.chat_id
            )

            # Filter by folder membership
            if folder_ids:
                normalized_folder_ids = sorted({int(fid) for fid in folder_ids})
                member_subq = select(ChatFolderMember.chat_id).where(ChatFolderMember.folder_id.in_(normalized_folder_ids))
                stmt = stmt.where(Chat.id.in_(member_subq))
            elif folder_id is not None:
                stmt = stmt.join(
                    ChatFolderMember, and_(ChatFolderMember.chat_id == Chat.id, ChatFolderMember.folder_id == folder_id)
                )

            # Filter by archived status
            if archived is True:
                stmt = stmt.where(Chat.is_archived == 1)
            elif archived is False:
                stmt = stmt.where(or_(Chat.is_archived == 0, Chat.is_archived.is_(None)))

            # Apply search filter if provided
            if search:
                search_pattern = f"%{search}%"
                stmt = stmt.where(
                    or_(
                        Chat.title.ilike(search_pattern),
                        Chat.first_name.ilike(search_pattern),
                        Chat.last_name.ilike(search_pattern),
                        Chat.username.ilike(search_pattern),
                    )
                )

            # Order by last message date
            stmt = stmt.order_by(subq.c.last_message_date.is_(None), subq.c.last_message_date.desc())

            # Apply pagination if limit is specified
            if limit is not None:
                stmt = stmt.limit(limit).offset(offset)

            result = await session.execute(stmt)
            chats = []
            for row in result:
                chat_dict = {
                    "id": row.Chat.id,
                    "type": row.Chat.type,
                    "title": row.Chat.title,
                    "username": row.Chat.username,
                    "first_name": row.Chat.first_name,
                    "last_name": row.Chat.last_name,
                    "phone": row.Chat.phone,
                    "description": row.Chat.description,
                    "participants_count": row.Chat.participants_count,
                    "is_forum": row.Chat.is_forum,
                    "is_archived": row.Chat.is_archived,
                    "last_synced_message_id": row.Chat.last_synced_message_id,
                    "created_at": row.Chat.created_at,
                    "updated_at": row.Chat.updated_at,
                    "last_message_date": row.last_message_date,
                    "last_message_text": row.last_message_text,
                    "last_message_sender": row.last_message_sender,
                    "last_message_id": row.last_message_id,
                    "last_message_media_type": row.last_message_media_type,
                }
                chats.append(chat_dict)
            return chats

    async def get_chat_count(
        self,
        search: str = None,
        archived: bool | None = None,
        folder_id: int | None = None,
        folder_ids: list[int] | None = None,
    ) -> int:
        """Get total number of chats (fast count for pagination).

        Args:
            search: Optional search query to filter count
            archived: If True, only archived chats; if False, only non-archived; if None, all
            folder_id: If set, only chats in this folder
            folder_ids: If set, chats in any of these folders (union)
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(func.count(Chat.id))

            if folder_ids:
                normalized_folder_ids = sorted({int(fid) for fid in folder_ids})
                member_subq = select(ChatFolderMember.chat_id).where(ChatFolderMember.folder_id.in_(normalized_folder_ids))
                stmt = stmt.where(Chat.id.in_(member_subq))
            elif folder_id is not None:
                stmt = stmt.join(
                    ChatFolderMember, and_(ChatFolderMember.chat_id == Chat.id, ChatFolderMember.folder_id == folder_id)
                )

            if archived is True:
                stmt = stmt.where(Chat.is_archived == 1)
            elif archived is False:
                stmt = stmt.where(or_(Chat.is_archived == 0, Chat.is_archived.is_(None)))

            if search:
                search_pattern = f"%{search}%"
                stmt = stmt.where(
                    or_(
                        Chat.title.ilike(search_pattern),
                        Chat.first_name.ilike(search_pattern),
                        Chat.last_name.ilike(search_pattern),
                        Chat.username.ilike(search_pattern),
                    )
                )

            result = await session.execute(stmt)
            return result.scalar() or 0

    async def get_chat_by_id(self, chat_id: int) -> dict[str, Any] | None:
        """Get a single chat by ID."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(Chat).where(Chat.id == chat_id))
            chat = result.scalar_one_or_none()
            if not chat:
                return None
            return {
                "id": chat.id,
                "type": chat.type,
                "title": chat.title,
                "username": chat.username,
                "first_name": chat.first_name,
                "last_name": chat.last_name,
                "phone": chat.phone,
                "description": chat.description,
                "participants_count": chat.participants_count,
                "is_forum": chat.is_forum,
                "is_archived": chat.is_archived,
            }

    # ========== User Operations ==========

    async def upsert_user(self, user_data: dict[str, Any]) -> None:
        """Insert or update a user record."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        async with self.db_manager.async_session_factory() as session:
            values = {
                "id": user_data["id"],
                "username": user_data.get("username"),
                "first_name": user_data.get("first_name"),
                "last_name": user_data.get("last_name"),
                "phone": user_data.get("phone"),
                "is_bot": 1 if user_data.get("is_bot") else 0,
                "updated_at": datetime.utcnow(),
            }

            if self._is_sqlite:
                stmt = sqlite_insert(User).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "username": stmt.excluded.username,
                        "first_name": stmt.excluded.first_name,
                        "last_name": stmt.excluded.last_name,
                        "phone": stmt.excluded.phone,
                        "is_bot": stmt.excluded.is_bot,
                        "updated_at": datetime.utcnow(),
                    },
                )
            else:
                stmt = pg_insert(User).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "username": stmt.excluded.username,
                        "first_name": stmt.excluded.first_name,
                        "last_name": stmt.excluded.last_name,
                        "phone": stmt.excluded.phone,
                        "is_bot": stmt.excluded.is_bot,
                        "updated_at": datetime.utcnow(),
                    },
                )

            await session.execute(stmt)
            await session.commit()

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        """Get a user by ID."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                return None
            return {
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "phone": user.phone,
                "is_bot": user.is_bot,
            }

    # ========== Sync Status Operations ==========

    @retry_on_locked()
    async def update_sync_status(self, chat_id: int, last_message_id: int, message_count: int) -> None:
        """Update sync status for a chat using atomic upsert."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        async with self.db_manager.async_session_factory() as session:
            now = datetime.utcnow()
            values = {
                "chat_id": chat_id,
                "last_message_id": last_message_id,
                "last_sync_date": now,
                "message_count": message_count,
            }

            if self._is_sqlite:
                stmt = sqlite_insert(SyncStatus).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["chat_id"],
                    set_={
                        "last_message_id": stmt.excluded.last_message_id,
                        "last_sync_date": stmt.excluded.last_sync_date,
                        "message_count": SyncStatus.message_count + stmt.excluded.message_count,
                    },
                )
            else:
                stmt = pg_insert(SyncStatus).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["chat_id"],
                    set_={
                        "last_message_id": stmt.excluded.last_message_id,
                        "last_sync_date": stmt.excluded.last_sync_date,
                        "message_count": SyncStatus.message_count + stmt.excluded.message_count,
                    },
                )

            await session.execute(stmt)
            await session.commit()

    # ========== Gap Detection ==========

    async def detect_message_gaps(self, chat_id: int, threshold: int = 50) -> list[tuple[int, int, int]]:
        """Detect gaps in message ID sequences for a chat.

        Finds consecutive message IDs where the jump between them exceeds
        the threshold. Small gaps are normal (deleted/service messages) —
        large gaps indicate backup failures.

        Returns list of (gap_start_id, gap_end_id, gap_size) tuples.
        """
        async with self.db_manager.async_session_factory() as session:
            query = text("""
                WITH ordered AS (
                    SELECT id, LAG(id) OVER (ORDER BY id) AS prev_id
                    FROM messages
                    WHERE chat_id = :chat_id
                )
                SELECT prev_id AS gap_start,
                       id AS gap_end,
                       id - prev_id AS gap_size
                FROM ordered
                WHERE prev_id IS NOT NULL
                  AND id - prev_id > :threshold
                ORDER BY gap_start
            """)
            result = await session.execute(query, {"chat_id": chat_id, "threshold": threshold})
            return [(row.gap_start, row.gap_end, row.gap_size) for row in result]

    async def get_chats_with_messages(self) -> list[int]:
        """Get all chat IDs that have at least one message."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Message.chat_id).distinct()
            result = await session.execute(stmt)
            return [row[0] for row in result]

    # ========== Statistics ==========

    async def get_statistics(self) -> dict[str, Any]:
        """Get statistics - alias for get_cached_statistics for backwards compatibility."""
        return await self.get_cached_statistics()

    async def get_cached_statistics(self) -> dict[str, Any]:
        """Get cached statistics (fast, no expensive queries)."""
        # Get cached stats from metadata
        cached_stats = await self.get_metadata("cached_stats")
        stats_calculated_at = await self.get_metadata("stats_calculated_at")
        last_backup_time = await self.get_metadata("last_backup_time")

        result = {
            "chats": 0,
            "messages": 0,
            "media_files": 0,
            "total_size_mb": 0,
            "stats_calculated_at": stats_calculated_at,
        }

        if cached_stats:
            import json

            try:
                result.update(json.loads(cached_stats))
            except (json.JSONDecodeError, TypeError):
                pass

        if last_backup_time:
            result["last_backup_time"] = last_backup_time
            result["last_backup_time_source"] = "metadata"

        return result

    async def calculate_and_store_statistics(self) -> dict[str, Any]:
        """Calculate statistics and store in metadata (expensive, run daily)."""
        import json
        from datetime import datetime

        async with self.db_manager.async_session_factory() as session:
            logger.info("Calculating statistics (this may take a while)...")

            # Chat count
            chat_count = await session.execute(select(func.count(Chat.id)))
            chat_count = chat_count.scalar() or 0

            # Message count
            msg_count = await session.execute(select(func.count()).select_from(Message))
            msg_count = msg_count.scalar() or 0

            # Media count
            media_count = await session.execute(select(func.count(Media.id)).where(Media.downloaded == 1))
            media_count = media_count.scalar() or 0

            # Total media size
            total_size = await session.execute(select(func.sum(Media.file_size)).where(Media.downloaded == 1))
            total_size = total_size.scalar() or 0

            # Per-chat statistics
            chat_stats_query = select(Message.chat_id, func.count(Message.id).label("message_count")).group_by(
                Message.chat_id
            )
            chat_stats_result = await session.execute(chat_stats_query)
            per_chat_stats = {row.chat_id: row.message_count for row in chat_stats_result}

            stats = {
                "chats": int(chat_count),
                "messages": int(msg_count),
                "media_files": int(media_count),
                "total_size_mb": float(round(total_size / (1024 * 1024), 2)),
                "per_chat_message_counts": {int(k): int(v) for k, v in per_chat_stats.items()},
            }

            logger.info(f"Statistics calculated: {chat_count} chats, {msg_count} messages, {media_count} media files")

        # Store in metadata
        await self.set_metadata("cached_stats", json.dumps(stats))
        await self.set_metadata("stats_calculated_at", datetime.utcnow().isoformat())

        return stats

    # ========== Delete Operations ==========

    async def delete_chat_and_related_data(self, chat_id: int, media_base_path: str = None) -> None:
        """Delete a chat and all related data."""
        async with self.db_manager.async_session_factory() as session:
            # Delete media records
            await session.execute(delete(Media).where(Media.chat_id == chat_id))
            # Delete reactions
            await session.execute(delete(Reaction).where(Reaction.chat_id == chat_id))
            # Delete messages
            await session.execute(delete(Message).where(Message.chat_id == chat_id))
            # Delete sync status
            await session.execute(delete(SyncStatus).where(SyncStatus.chat_id == chat_id))
            # Delete chat
            await session.execute(delete(Chat).where(Chat.id == chat_id))

            await session.commit()
            logger.info(f"Deleted chat {chat_id} and all related data from database")

        # Delete physical files
        if media_base_path and os.path.exists(media_base_path):
            chat_media_dir = os.path.join(media_base_path, str(chat_id))
            if os.path.exists(chat_media_dir):
                try:
                    shutil.rmtree(chat_media_dir)
                    logger.info(f"Deleted media folder: {chat_media_dir}")
                except Exception as e:
                    logger.error(f"Failed to delete media folder {chat_media_dir}: {e}")

            for avatar_type in ["chats", "users"]:
                avatar_pattern = os.path.join(media_base_path, "avatars", avatar_type, f"{chat_id}_*.jpg")
                avatar_files = glob.glob(avatar_pattern)

                # Legacy fallback: remove old <chat_id>.jpg files as well
                legacy_avatar = os.path.join(media_base_path, "avatars", avatar_type, f"{chat_id}.jpg")
                if os.path.exists(legacy_avatar):
                    avatar_files.append(legacy_avatar)
                for avatar_file in avatar_files:
                    try:
                        os.remove(avatar_file)
                        logger.info(f"Deleted avatar file: {avatar_file}")
                    except Exception as e:
                        logger.error(f"Failed to delete avatar {avatar_file}: {e}")

    # ========== Pinned Messages ==========

    async def get_pinned_messages(self, chat_id: int) -> list[dict[str, Any]]:
        """Get all pinned messages for a chat, ordered by date descending (newest first).

        v6.0.0: Media is now returned as a nested object from the media table.
        """
        async with self.db_manager.async_session_factory() as session:
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
                .where(Message.is_pinned == 1)
                .order_by(Message.date.desc())
            )

            result = await session.execute(stmt)
            rows = result.all()

            messages = []
            for row in rows:
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
                        import json
                        msg["raw_data"] = json.loads(msg["raw_data"])
                    except:
                        msg["raw_data"] = {}

                messages.append(msg)

            return messages

    async def sync_pinned_messages(self, chat_id: int, pinned_message_ids: list[int]) -> None:
        """
        Sync pinned messages for a chat.

        Sets is_pinned=1 for messages in the list and is_pinned=0 for all others.
        This ensures the database reflects the current state of pinned messages.

        Args:
            chat_id: Chat ID
            pinned_message_ids: List of message IDs that are currently pinned
        """
        async with self.db_manager.async_session_factory() as session:
            # First, unpin all messages in this chat
            await session.execute(
                update(Message).where(Message.chat_id == chat_id).where(Message.is_pinned == 1).values(is_pinned=0)
            )

            # Then, pin the specified messages (if any exist in our database)
            if pinned_message_ids:
                await session.execute(
                    update(Message)
                    .where(Message.chat_id == chat_id)
                    .where(Message.id.in_(pinned_message_ids))
                    .values(is_pinned=1)
                )

            await session.commit()

    async def update_message_pinned(self, chat_id: int, message_id: int, is_pinned: bool) -> None:
        """
        Update the pinned status of a single message.

        Used by the real-time listener when pin/unpin events are received.

        Args:
            chat_id: Chat ID
            message_id: Message ID
            is_pinned: Whether the message is pinned
        """
        async with self.db_manager.async_session_factory() as session:
            await session.execute(
                update(Message)
                .where(Message.chat_id == chat_id)
                .where(Message.id == message_id)
                .values(is_pinned=1 if is_pinned else 0)
            )
            await session.commit()

    # ========== Forum Topic Operations (v6.2.0) ==========

    @retry_on_locked()
    async def upsert_forum_topic(self, topic_data: dict[str, Any]) -> None:
        """Insert or update a forum topic record."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        async with self.db_manager.async_session_factory() as session:
            values = {
                "id": topic_data["id"],
                "chat_id": topic_data["chat_id"],
                "title": topic_data["title"],
                "icon_color": topic_data.get("icon_color"),
                "icon_emoji_id": topic_data.get("icon_emoji_id"),
                "icon_emoji": topic_data.get("icon_emoji"),
                "is_closed": topic_data.get("is_closed", 0),
                "is_pinned": topic_data.get("is_pinned", 0),
                "is_hidden": topic_data.get("is_hidden", 0),
                "date": _strip_tz(topic_data.get("date")),
                "updated_at": datetime.utcnow(),
            }

            update_set = {
                "title": values["title"],
                "icon_color": values["icon_color"],
                "icon_emoji_id": values["icon_emoji_id"],
                "icon_emoji": values["icon_emoji"],
                "is_closed": values["is_closed"],
                "is_pinned": values["is_pinned"],
                "is_hidden": values["is_hidden"],
                "date": values["date"],
                "updated_at": datetime.utcnow(),
            }

            if self._is_sqlite:
                stmt = sqlite_insert(ForumTopic).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id", "chat_id"], set_=update_set)
            else:
                stmt = pg_insert(ForumTopic).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id", "chat_id"], set_=update_set)

            await session.execute(stmt)
            await session.commit()

    async def get_forum_topics(self, chat_id: int) -> list[dict[str, Any]]:
        """Get all forum topics for a chat, with message count per topic."""
        async with self.db_manager.async_session_factory() as session:
            # Subquery for message counts and last message date per topic.
            # Messages with reply_to_top_id=NULL are treated as General topic (id=1),
            # since pre-v6.2.0 messages and pre-forum messages lack topic assignment
            # and Telegram's client displays them under General.
            effective_topic_id = func.coalesce(Message.reply_to_top_id, 1).label("effective_topic_id")
            msg_subq = (
                select(
                    effective_topic_id,
                    func.count(Message.id).label("message_count"),
                    func.max(Message.date).label("last_message_date"),
                )
                .where(Message.chat_id == chat_id)
                .group_by(effective_topic_id)
                .subquery()
            )

            stmt = (
                select(ForumTopic, msg_subq.c.message_count, msg_subq.c.last_message_date)
                .outerjoin(msg_subq, ForumTopic.id == msg_subq.c.effective_topic_id)
                .where(ForumTopic.chat_id == chat_id)
                .order_by(ForumTopic.is_pinned.desc(), msg_subq.c.last_message_date.desc().nullslast())
            )

            result = await session.execute(stmt)
            topics = []
            for row in result:
                topic = row.ForumTopic
                topics.append(
                    {
                        "id": topic.id,
                        "chat_id": topic.chat_id,
                        "title": topic.title,
                        "icon_color": topic.icon_color,
                        "icon_emoji_id": topic.icon_emoji_id,
                        "icon_emoji": topic.icon_emoji,
                        "is_closed": topic.is_closed,
                        "is_pinned": topic.is_pinned,
                        "is_hidden": topic.is_hidden,
                        "date": topic.date,
                        "message_count": row.message_count or 0,
                        "last_message_date": row.last_message_date,
                    }
                )
            return topics

    # ========== Chat Folder Operations (v6.2.0) ==========

    @retry_on_locked()
    async def upsert_chat_folder(self, folder_data: dict[str, Any]) -> None:
        """Insert or update a chat folder."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        async with self.db_manager.async_session_factory() as session:
            values = {
                "id": folder_data["id"],
                "title": folder_data["title"],
                "emoticon": folder_data.get("emoticon"),
                "sort_order": folder_data.get("sort_order", 0),
                "updated_at": datetime.utcnow(),
            }

            update_set = {
                "title": values["title"],
                "emoticon": values["emoticon"],
                "sort_order": values["sort_order"],
                "updated_at": datetime.utcnow(),
            }

            if self._is_sqlite:
                stmt = sqlite_insert(ChatFolder).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_set)
            else:
                stmt = pg_insert(ChatFolder).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_set)

            await session.execute(stmt)
            await session.commit()

    @retry_on_locked()
    async def sync_folder_members(self, folder_id: int, chat_ids: list[int]) -> None:
        """Sync folder membership: replace all members for a folder."""
        async with self.db_manager.async_session_factory() as session:
            # Delete existing members
            await session.execute(delete(ChatFolderMember).where(ChatFolderMember.folder_id == folder_id))

            # Insert new members (only for chats that exist in our DB)
            if chat_ids:
                # Verify which chat_ids actually exist
                existing = await session.execute(select(Chat.id).where(Chat.id.in_(chat_ids)))
                existing_ids = {row[0] for row in existing}

                for cid in chat_ids:
                    if cid in existing_ids:
                        session.add(ChatFolderMember(folder_id=folder_id, chat_id=cid))

            await session.commit()

    async def get_all_folders(self, chat_ids: set[int] | list[int] | None = None) -> list[dict[str, Any]]:
        """Get chat folders with counts, optionally scoped to chat IDs."""
        async with self.db_manager.async_session_factory() as session:
            if chat_ids is not None:
                normalized_chat_ids = sorted({int(cid) for cid in chat_ids})
                if not normalized_chat_ids:
                    return []
                count_subq = (
                    select(ChatFolderMember.folder_id, func.count(ChatFolderMember.chat_id).label("chat_count"))
                    .where(ChatFolderMember.chat_id.in_(normalized_chat_ids))
                    .group_by(ChatFolderMember.folder_id)
                    .subquery()
                )
                stmt = (
                    select(ChatFolder, count_subq.c.chat_count)
                    .join(count_subq, ChatFolder.id == count_subq.c.folder_id)
                    .order_by(ChatFolder.sort_order, ChatFolder.title)
                )
            else:
                count_subq = (
                    select(ChatFolderMember.folder_id, func.count(ChatFolderMember.chat_id).label("chat_count"))
                    .group_by(ChatFolderMember.folder_id)
                    .subquery()
                )
                stmt = (
                    select(ChatFolder, count_subq.c.chat_count)
                    .outerjoin(count_subq, ChatFolder.id == count_subq.c.folder_id)
                    .order_by(ChatFolder.sort_order, ChatFolder.title)
                )

            result = await session.execute(stmt)
            folders = []
            for row in result:
                folder = row.ChatFolder
                folders.append(
                    {
                        "id": folder.id,
                        "title": folder.title,
                        "emoticon": folder.emoticon,
                        "sort_order": folder.sort_order,
                        "chat_count": row.chat_count or 0,
                    }
                )
            return folders

    @retry_on_locked()
    async def cleanup_stale_folders(self, active_folder_ids: list[int]) -> None:
        """Remove folders that no longer exist in Telegram."""
        async with self.db_manager.async_session_factory() as session:
            if active_folder_ids:
                await session.execute(delete(ChatFolder).where(ChatFolder.id.notin_(active_folder_ids)))
            else:
                await session.execute(delete(ChatFolder))
            await session.commit()

    async def get_archived_chat_count(self) -> int:
        """Get the count of archived chats."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(func.count(Chat.id)).where(Chat.is_archived == 1))
            return result.scalar() or 0

    # ========== Backup Profile Management (v11.0.0) ==========

    @retry_on_locked()
    async def create_backup_profile(self, **kwargs) -> dict[str, Any]:
        """Create a new backup profile. Returns the created profile dict."""
        async with self.db_manager.async_session_factory() as session:
            profile = BackupProfile(**kwargs)
            session.add(profile)
            await session.commit()
            await session.refresh(profile)
            return self._backup_profile_to_dict(profile)

    async def list_backup_profiles(self, active_only: bool = False) -> list[dict[str, Any]]:
        """List all backup profiles, optionally filtered to active only."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(BackupProfile).order_by(BackupProfile.sort_order, BackupProfile.created_at)
            if active_only:
                stmt = stmt.where(BackupProfile.is_active == 1)
            result = await session.execute(stmt)
            return [self._backup_profile_to_dict(p) for p in result.scalars().all()]

    async def get_backup_profile(self, profile_id: str) -> dict[str, Any] | None:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(BackupProfile).where(BackupProfile.id == profile_id))
            profile = result.scalar_one_or_none()
            return self._backup_profile_to_dict(profile) if profile else None

    @retry_on_locked()
    async def update_backup_profile(self, profile_id: str, **kwargs) -> dict[str, Any] | None:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(BackupProfile).where(BackupProfile.id == profile_id))
            profile = result.scalar_one_or_none()
            if not profile:
                return None
            for key, value in kwargs.items():
                if hasattr(profile, key):
                    setattr(profile, key, value)
            await session.commit()
            await session.refresh(profile)
            return self._backup_profile_to_dict(profile)

    @retry_on_locked()
    async def delete_backup_profile(self, profile_id: str) -> bool:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(BackupProfile).where(BackupProfile.id == profile_id))
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    def _backup_profile_to_dict(profile: BackupProfile) -> dict[str, Any]:
        return {
            "id": profile.id,
            "name": profile.name,
            "description": profile.description,
            "icon": profile.icon or "database",
            "color": profile.color or "#8774e1",
            "url": profile.url,
            "is_active": bool(profile.is_active),
            "sort_order": profile.sort_order or 0,
            "created_by": profile.created_by,
            "created_at": profile.created_at.isoformat() if profile.created_at else None,
            "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
        }

    # ========== Chat Members & Message Density ==========

    async def get_chat_members(
        self,
        chat_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get unique senders in a chat with message counts.

        Args:
            chat_id: Chat ID
            limit: Max results
            offset: Pagination offset

        Returns:
            List of dicts with sender_id, name, username, message_count
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(
                    Message.sender_id,
                    func.count(Message.id).label("message_count"),
                    User.first_name,
                    User.last_name,
                    User.username,
                )
                .outerjoin(User, Message.sender_id == User.id)
                .where(and_(Message.chat_id == chat_id, Message.sender_id.isnot(None)))
                .group_by(Message.sender_id, User.first_name, User.last_name, User.username)
                .order_by(func.count(Message.id).desc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            return [
                {
                    "sender_id": row.sender_id,
                    "name": f"{row.first_name or ''} {row.last_name or ''}".strip() or None,
                    "username": row.username,
                    "message_count": row.message_count,
                }
                for row in result
            ]

    async def get_message_density(
        self,
        chat_id: int,
        granularity: str = "week",
        timezone: str = "UTC",
    ) -> list[dict[str, Any]]:
        """Get message counts grouped by time period for timeline/heatmap.

        Args:
            chat_id: Chat ID
            granularity: "day", "week", or "month"
            timezone: IANA timezone string (used for SQLite strftime offset)

        Returns:
            List of dicts with "date" and "count"
        """
        async with self.db_manager.async_session_factory() as session:
            if self._is_sqlite:
                # SQLite: use strftime for grouping
                fmt_map = {
                    "day": "%Y-%m-%d",
                    "week": "%Y-W%W",
                    "month": "%Y-%m",
                }
                fmt = fmt_map.get(granularity, "%Y-W%W")
                date_label = func.strftime(fmt, Message.date).label("period")
            else:
                # PostgreSQL: use date_trunc
                trunc_map = {
                    "day": "day",
                    "week": "week",
                    "month": "month",
                }
                trunc = trunc_map.get(granularity, "week")
                date_label = func.date_trunc(trunc, Message.date).label("period")

            stmt = (
                select(date_label, func.count(Message.id).label("count"))
                .where(Message.chat_id == chat_id)
                .group_by("period")
                .order_by(text("period ASC"))
            )
            result = await session.execute(stmt)
            return [
                {"date": str(row.period), "count": row.count}
                for row in result
            ]
