"""Organization-focused database mixin.

Handles pinned messages, forum topics, chat folders, backup profiles,
chat members, and message density calculations.
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, func, select, text, update

from .adapter import _strip_tz, retry_on_locked
from .models import (
    BackupProfile,
    Chat,
    ChatFolder,
    ChatFolderMember,
    ForumTopic,
    Media,
    Message,
    User,
)

logger = logging.getLogger(__name__)


class OrganizeMixin:
    """Mixin for organizational database operations.

    Assumes ``self.db_manager`` and ``self._is_sqlite`` are set by the host class.
    """

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

    # Flag-based folders can now resolve to very large member sets, so the
    # existence check is chunked to stay well under driver bind-parameter caps
    # (SQLite ~32766, PostgreSQL 65535).
    _FOLDER_MEMBER_CHUNK = 500

    @retry_on_locked()
    async def sync_folder_members(self, folder_id: int, chat_ids: list[int]) -> None:
        """Sync folder membership: replace all members for a folder."""
        async with self.db_manager.async_session_factory() as session:
            # Delete existing members
            await session.execute(delete(ChatFolderMember).where(ChatFolderMember.folder_id == folder_id))

            # Insert new members (only for chats that exist in our DB)
            if chat_ids:
                # Dedup while preserving order; verify existence in bounded chunks.
                unique_ids = list(dict.fromkeys(chat_ids))
                existing_ids: set[int] = set()
                for i in range(0, len(unique_ids), self._FOLDER_MEMBER_CHUNK):
                    chunk = unique_ids[i : i + self._FOLDER_MEMBER_CHUNK]
                    result = await session.execute(select(Chat.id).where(Chat.id.in_(chunk)))
                    existing_ids.update(row[0] for row in result)

                for cid in unique_ids:
                    if cid in existing_ids:
                        session.add(ChatFolderMember(folder_id=folder_id, chat_id=cid))

            await session.commit()

    async def get_chats_for_folder_resolution(self) -> list[dict[str, Any]]:
        """Return every archived chat with the facts needed to evaluate a folder's
        category flags: id, type, whether it is a bot, and archived state.

        Bot-ness is only meaningful for private chats and is read from the users
        table (chats store bots as type ``private``). The join is on ``User.id ==
        Chat.id`` — a private chat's id is the positive user id, while group and
        channel ids are negative/marked and can never collide with a user id, so
        they always resolve to ``is_bot = 0``.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(
                Chat.id,
                Chat.type,
                Chat.is_archived,
                func.coalesce(User.is_bot, 0).label("is_bot"),
            ).outerjoin(User, User.id == Chat.id)
            result = await session.execute(stmt)
            return [
                {
                    "id": row.id,
                    "type": row.type,
                    "is_bot": bool(row.is_bot),
                    "is_archived": bool(row.is_archived),
                }
                for row in result
            ]

    async def get_all_folders(self, chat_ids: set[int] | list[int] | None = None) -> list[dict[str, Any]]:
        """Get chat folders with counts, optionally scoped to chat IDs.

        Only folders that contain at least one backed-up (and, for restricted
        viewers, accessible) chat are returned. The viewer reflects the archive,
        not the full Telegram account: a folder whose chats were all excluded
        from backup — or that is empty on Telegram — would otherwise show as an
        empty filter tab that returns nothing when clicked (#208). Membership is
        already limited to chats present in our DB by sync_folder_members, so a
        zero count means "nothing archived here".
        """
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
                count = row.chat_count or 0
                # Hide folders with no backed-up chats (empty tabs help no one).
                # The restricted branch already excludes them via its inner join;
                # this also covers the unrestricted outer-join branch (#208).
                if count == 0:
                    continue
                folder = row.ChatFolder
                folders.append(
                    {
                        "id": folder.id,
                        "title": folder.title,
                        "emoticon": folder.emoticon,
                        "sort_order": folder.sort_order,
                        "chat_count": count,
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
