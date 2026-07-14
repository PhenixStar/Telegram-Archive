"""Sync, chat, user, and statistics operations mixin.

Handles chat/user upserts, sync status, gap detection, statistics,
and chat deletion. Organizational operations (pinned messages, forum topics,
chat folders, backup profiles, members, density) are in adapter_organize.py.
"""

import glob
import logging
import os
import shutil
from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text

from .adapter import retry_on_locked
from .models import (
    Chat,
    ChatFolderMember,
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

    @staticmethod
    def _apply_folder_filter(stmt, folder_id=None, folder_ids=None):
        """Apply folder membership filter to a query statement (DRY helper)."""
        if folder_ids:
            normalized = sorted({int(fid) for fid in folder_ids})
            member_subq = select(ChatFolderMember.chat_id).where(
                ChatFolderMember.folder_id.in_(normalized)
            )
            return stmt.where(Chat.id.in_(member_subq))
        elif folder_id is not None:
            return stmt.join(
                ChatFolderMember,
                and_(ChatFolderMember.chat_id == Chat.id, ChatFolderMember.folder_id == folder_id),
            )
        return stmt

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
            stmt = self._apply_folder_filter(stmt, folder_id, folder_ids)

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

            stmt = self._apply_folder_filter(stmt, folder_id, folder_ids)

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

    async def calculate_and_store_statistics(self, storage_path: str | None = None) -> dict[str, Any]:
        """Calculate statistics and store in metadata (expensive, run daily).

        When ``storage_path`` is given, total media size reflects actual on-disk
        usage (``du`` semantics) via ``compute_directory_size`` so the figure
        tracks real disk consumption. The filesystem walk is a blocking scan, so
        it runs off the event loop (``asyncio.to_thread``) and outside the DB
        session. If the path is missing/unmounted (``du`` is 0 while media rows
        exist), or no path is given, it falls back to the DB snapshot
        ``SUM(media.file_size WHERE downloaded=1)``.
        """
        import asyncio
        import json
        from datetime import datetime

        from ..message_utils import compute_directory_size

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

            # DB snapshot of downloaded media sizes — the fallback when on-disk
            # usage is unavailable (e.g. the backup volume is not mounted yet).
            db_total_size = (
                await session.execute(select(func.sum(Media.file_size)).where(Media.downloaded == 1))
            ).scalar() or 0

            # Per-chat statistics
            chat_stats_query = select(Message.chat_id, func.count(Message.id).label("message_count")).group_by(
                Message.chat_id
            )
            chat_stats_result = await session.execute(chat_stats_query)
            per_chat_stats = {row.chat_id: row.message_count for row in chat_stats_result}

        # Total media size: prefer actual on-disk usage. Run the blocking walk off
        # the event loop and after the session is closed so it never stalls other
        # requests or pins a DB connection.
        if storage_path is not None:
            total_size = await asyncio.to_thread(compute_directory_size, storage_path)
            if total_size == 0 and media_count > 0:
                # Path missing/unmounted: don't cache a spurious 0 over the last good value.
                logger.warning("On-disk storage size is 0 while media exists; using DB snapshot for storage stat")
                total_size = db_total_size
        else:
            total_size = db_total_size

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

