"""Media and reaction database operations mixin.

Handles insert, query, update, delete for media files and message reactions.
"""

import logging
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, update

from .adapter import retry_on_locked
from .models import Media, Message, Reaction, User

logger = logging.getLogger(__name__)


class MediaMixin:
    """Mixin providing media and reaction operations.

    Assumes ``self.db_manager`` and ``self._is_sqlite`` are set by the host class.
    """

    async def insert_media(self, media_data: dict[str, Any]) -> None:
        """Insert a media file record."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        async with self.db_manager.async_session_factory() as session:
            values = {
                "id": media_data["id"],
                "message_id": media_data.get("message_id"),
                "chat_id": media_data.get("chat_id"),
                "type": media_data["type"],
                "file_name": media_data.get("file_name"),
                "file_path": media_data.get("file_path"),
                "file_size": media_data.get("file_size"),
                "mime_type": media_data.get("mime_type"),
                "width": media_data.get("width"),
                "height": media_data.get("height"),
                "duration": media_data.get("duration"),
                "downloaded": 1 if media_data.get("downloaded") else 0,
                "download_date": media_data.get("download_date"),
            }

            if self._is_sqlite:
                stmt = sqlite_insert(Media).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=values)
            else:
                stmt = pg_insert(Media).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=values)

            await session.execute(stmt)
            await session.commit()

    async def get_media_for_chat(self, chat_id: int) -> list[dict[str, Any]]:
        """
        Get all media records for a specific chat.

        Args:
            chat_id: Chat identifier

        Returns:
            List of media records with file paths and metadata
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Media).where(Media.chat_id == chat_id)
            result = await session.execute(stmt)
            media_records = result.scalars().all()

            return [
                {
                    "id": m.id,
                    "message_id": m.message_id,
                    "chat_id": m.chat_id,
                    "type": m.type,
                    "file_path": m.file_path,
                    "file_size": m.file_size,
                    "downloaded": m.downloaded,
                }
                for m in media_records
            ]

    async def delete_media_for_chat(self, chat_id: int) -> int:
        """
        Delete all media records for a specific chat.
        Does not delete message records or the chat itself.

        Args:
            chat_id: Chat identifier

        Returns:
            Number of media records deleted
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = delete(Media).where(Media.chat_id == chat_id)
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount

    async def get_media_for_verification(self) -> list[dict[str, Any]]:
        """
        Get all media records that should have files on disk.
        Used by VERIFY_MEDIA to check for missing/corrupted files.

        Returns media where downloaded=1 OR file_path is not null.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Media)
                .where(or_(Media.downloaded == 1, Media.file_path.isnot(None)))
                .order_by(Media.chat_id, Media.message_id)
            )
            result = await session.execute(stmt)
            return [
                {
                    "id": m.id,
                    "message_id": m.message_id,
                    "chat_id": m.chat_id,
                    "type": m.type,
                    "file_path": m.file_path,
                    "file_name": m.file_name,
                    "file_size": m.file_size,
                    "downloaded": m.downloaded,
                }
                for m in result.scalars()
            ]

    async def iter_media_paths_for_repair(self, batch_size: int = 500):
        """Yield ``(id, file_path, file_name)`` batches for the #175 repair pass.

        Keyset-paginated on the primary key and projecting only the three columns
        the repair needs, so memory stays bounded regardless of table size. The
        full-table materialization in ``get_media_for_verification`` OOM-killed
        the 256m backup container on large archives; this streams instead.
        """
        last_id: str | None = None
        while True:
            async with self.db_manager.async_session_factory() as session:
                stmt = (
                    select(Media.id, Media.file_path, Media.file_name)
                    .where(or_(Media.downloaded == 1, Media.file_path.isnot(None)))
                    .order_by(Media.id)
                    .limit(batch_size)
                )
                if last_id is not None:
                    stmt = stmt.where(Media.id > last_id)
                rows = (await session.execute(stmt)).all()
            if not rows:
                return
            yield [{"id": r[0], "file_path": r[1], "file_name": r[2]} for r in rows]
            last_id = rows[-1][0]
            if len(rows) < batch_size:
                return

    async def update_media_file_path(self, media_id: str, file_path: str) -> None:
        """Update the stored file_path for a single media record."""
        async with self.db_manager.async_session_factory() as session:
            stmt = update(Media).where(Media.id == media_id).values(file_path=file_path)
            await session.execute(stmt)
            await session.commit()

    async def mark_media_for_redownload(self, media_id: str) -> None:
        """Mark a media record as needing re-download."""
        async with self.db_manager.async_session_factory() as session:
            stmt = update(Media).where(Media.id == media_id).values(downloaded=0, file_path=None, download_date=None)
            await session.execute(stmt)
            await session.commit()

    # ========== Reaction Operations ==========

    @retry_on_locked()
    async def insert_reactions(self, message_id: int, chat_id: int, reactions: list[dict[str, Any]]) -> None:
        """Insert reactions for a message using upsert to avoid sequence issues."""
        if not reactions:
            return

        async with self.db_manager.async_session_factory() as session:
            # Delete existing reactions first
            await session.execute(
                delete(Reaction).where(and_(Reaction.message_id == message_id, Reaction.chat_id == chat_id))
            )
            await session.commit()

        # Insert in a separate transaction to avoid sequence conflicts
        async with self.db_manager.async_session_factory() as session:
            for reaction in reactions:
                try:
                    r = Reaction(
                        message_id=message_id,
                        chat_id=chat_id,
                        emoji=reaction["emoji"],
                        user_id=reaction.get("user_id"),
                        count=reaction.get("count", 1),
                    )
                    session.add(r)
                    await session.flush()  # Flush each to catch errors early
                except Exception as e:
                    if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
                        # Sequence out of sync - reset and retry
                        logger.warning("Reactions sequence out of sync, resetting...")
                        await session.rollback()
                        await self._reset_reactions_sequence()
                        # Retry the insert
                        async with self.db_manager.async_session_factory() as retry_session:
                            r = Reaction(
                                message_id=message_id,
                                chat_id=chat_id,
                                emoji=reaction["emoji"],
                                user_id=reaction.get("user_id"),
                                count=reaction.get("count", 1),
                            )
                            retry_session.add(r)
                            await retry_session.commit()
                        return  # Exit after recovery
                    raise

            await session.commit()

    async def _reset_reactions_sequence(self) -> None:
        """Reset the reactions table sequence to max(id) + 1."""
        async with self.db_manager.async_session_factory() as session:
            if self.db_manager.db_type == "postgresql":
                await session.execute(
                    text("SELECT setval('reactions_id_seq', COALESCE((SELECT MAX(id) FROM reactions), 0) + 1, false)")
                )
                await session.commit()
                logger.info("Reset reactions_id_seq sequence")

    async def get_reactions(self, message_id: int, chat_id: int) -> list[dict[str, Any]]:
        """Get all reactions for a message."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Reaction)
                .where(and_(Reaction.message_id == message_id, Reaction.chat_id == chat_id))
                .order_by(Reaction.emoji)
            )
            result = await session.execute(stmt)
            return [{"emoji": r.emoji, "user_id": r.user_id, "count": r.count} for r in result.scalars()]

    async def get_media_messages(
        self,
        chat_id: int,
        media_type: str | None = None,
        limit: int = 50,
        before: int | None = None,
    ) -> dict[str, Any]:
        """Get messages that have media for a chat, with optional type filter.

        Args:
            chat_id: Chat ID
            media_type: Optional filter (photo, video, voice, document, animation)
            limit: Max results (caller clamps to 1..200)
            before: Message-ID cursor for pagination (return messages with id < before)

        Returns:
            Dict with "messages" list and "has_more" bool
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
                .join(Media, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
                .outerjoin(User, Message.sender_id == User.id)
                .where(Message.chat_id == chat_id)
            )

            if media_type:
                stmt = stmt.where(Media.type == media_type)

            if before is not None:
                stmt = stmt.where(Message.id < before)

            # Fetch limit+1 to determine has_more
            stmt = stmt.order_by(Message.id.desc()).limit(limit + 1)

            result = await session.execute(stmt)
            rows = result.all()
            has_more = len(rows) > limit
            rows = rows[:limit]

            messages = []
            for row in rows:
                msg = self._message_to_dict(row.Message)
                msg["first_name"] = row.first_name
                msg["last_name"] = row.last_name
                msg["username"] = row.username
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
                messages.append(msg)

            return {"messages": messages, "has_more": has_more}

    async def get_media_for_message(self, chat_id: int, message_id: int) -> dict[str, Any] | None:
        """Get the first media record for a specific message."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Media).where(
                and_(Media.chat_id == chat_id, Media.message_id == message_id)
            ).limit(1)
            result = await session.execute(stmt)
            m = result.scalar_one_or_none()
            if not m:
                return None
            return {
                "id": m.id, "message_id": m.message_id, "chat_id": m.chat_id,
                "type": m.type, "file_path": m.file_path, "downloaded": m.downloaded,
            }

    # ========== Media Gallery (cursor-paginated, per-media-item) ==========

    async def get_media_paginated(
        self,
        chat_id: int,
        media_types: list[str] | None = None,
        limit: int = 50,
        before_id: str | None = None,
    ) -> dict[str, Any]:
        """Get paginated downloaded media records for a chat's media gallery.

        Unlike ``get_media_messages`` (which keys results by message), this returns
        one entry per media item, enabling album-aware galleries.

        Uses a composite cursor (Message.date, Media.id) for deterministic ordering
        so albums (multiple media sharing one message_id) paginate without overlap.

        Args:
            chat_id: Chat identifier
            media_types: Optional list of media types to include (e.g. ["photo", "video"])
            limit: Max items to return (caller clamps to 1..200)
            before_id: Media-ID cursor; return items strictly older than this item

        Returns:
            Dict with "items" (list of media dicts) and "has_more" (bool)
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Media, Message.date, User.first_name, User.last_name)
                .join(
                    Message,
                    and_(
                        Media.message_id == Message.id,
                        Media.chat_id == Message.chat_id,
                    ),
                )
                .outerjoin(User, Message.sender_id == User.id)
            )
            stmt = stmt.where(and_(Media.chat_id == chat_id, Media.downloaded == 1))

            if media_types:
                stmt = stmt.where(Media.type.in_(media_types))

            if before_id:
                cursor_stmt = (
                    select(Media.id, Message.date)
                    .join(
                        Message,
                        and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id),
                    )
                    .where(Media.id == before_id)
                )
                cursor_result = await session.execute(cursor_stmt)
                cursor_row = cursor_result.one_or_none()
                if cursor_row is None:
                    return {"items": [], "has_more": False}
                cursor_media_id, cursor_date = cursor_row
                stmt = stmt.where(
                    or_(
                        Message.date < cursor_date,
                        and_(Message.date == cursor_date, Media.id < cursor_media_id),
                    )
                )

            stmt = stmt.order_by(Message.date.desc(), Media.id.desc())
            stmt = stmt.limit(limit + 1)
            result = await session.execute(stmt)
            rows = result.all()

            has_more = len(rows) > limit
            items = [
                {
                    "id": media.id,
                    "message_id": media.message_id,
                    "chat_id": media.chat_id,
                    "type": media.type,
                    "file_path": media.file_path,
                    "file_name": media.file_name,
                    "file_size": media.file_size,
                    "mime_type": media.mime_type,
                    "width": media.width,
                    "height": media.height,
                    "duration": media.duration,
                    "message_date": msg_date.isoformat() if msg_date else None,
                    "sender_name": f"{first_name or ''} {last_name or ''}".strip() or None,
                }
                for media, msg_date, first_name, last_name in rows[:limit]
            ]

            return {"items": items, "has_more": has_more}

    async def get_media_counts(self, chat_id: int) -> dict[str, int]:
        """Get count of downloaded media grouped by type for a chat.

        Args:
            chat_id: Chat identifier

        Returns:
            Dict mapping media type to count (only types with count > 0)
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Media.type, func.count())
                .where(and_(Media.chat_id == chat_id, Media.downloaded == 1))
                .group_by(Media.type)
            )
            result = await session.execute(stmt)
            return {row[0]: row[1] for row in result.all()}
