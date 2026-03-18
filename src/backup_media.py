"""Media download and processing mixin for TelegramBackup."""

import logging
import os
from datetime import datetime

from telethon.tl.types import (
    Message,
    MessageMediaContact,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaPhoto,
    MessageMediaPoll,
)

from .avatar_utils import get_avatar_paths

logger = logging.getLogger(__name__)


class BackupMediaMixin:
    """Media download, processing, cleanup, and profile photo methods."""

    async def _ensure_profile_photo(self, entity, marked_id: int = None) -> None:
        """
        Download the current profile photo for users and chats.

        Downloads the profile photo on every backup run to ensure avatars
        stay up-to-date. Files are named `<chat_id>_<photo_id>.jpg` so the
        viewer can pick the freshest version.

        Args:
            entity: Telegram entity (User, Chat, Channel)
            marked_id: The marked chat ID (negative for groups/channels) for consistent file naming
        """
        file_id = marked_id if marked_id is not None else self._get_marked_id(entity)
        avatar_path, _legacy_path = get_avatar_paths(self.config.media_path, entity, file_id)

        # Nothing to download (no avatar set)
        if avatar_path is None:
            logger.debug(f"No avatar available for {file_id}")
            return

        try:
            # Avoid redundant downloads when we already have the current photo
            needs_download = not os.path.exists(avatar_path) or os.path.getsize(avatar_path) == 0

            if not needs_download:
                return

            result = await self.client.download_profile_photo(
                entity,
                file=avatar_path,
                download_big=False,  # Small size is usually sufficient
            )
            if result:
                logger.info(f"📷 Avatar downloaded: {avatar_path}")
        except Exception as e:
            logger.warning(f"Failed to download avatar for {file_id}: {e}")

    async def _cleanup_existing_media(self, chat_id: int) -> None:
        """
        Delete existing media files and database records for a chat.
        Used when a chat is added to SKIP_MEDIA_CHAT_IDS to reclaim storage.

        Handles deduplicated media safely: symlinks are removed without
        affecting the shared original in _shared/. Only real files
        (non-symlinks) count toward freed storage.

        Args:
            chat_id: Chat identifier
        """
        try:
            media_records = await self.db.get_media_for_chat(chat_id)
            if not media_records:
                logger.debug(f"No existing media found for chat {chat_id}")
                return

            deleted_files = 0
            deleted_symlinks = 0
            deleted_records = 0
            freed_bytes = 0

            for record in media_records:
                file_path = record.get("file_path")
                if file_path and os.path.exists(file_path):
                    try:
                        if os.path.islink(file_path):
                            os.unlink(file_path)
                            deleted_symlinks += 1
                        else:
                            freed_bytes += os.path.getsize(file_path)
                            os.remove(file_path)
                            deleted_files += 1
                    except Exception as e:
                        logger.warning(f"Failed to delete media file {file_path}: {e}")

            # Delete all media records from database for this chat
            deleted_records = await self.db.delete_media_for_chat(chat_id)

            # Clean up empty chat media directory
            chat_media_dir = os.path.join(self.config.media_path, str(chat_id))
            if os.path.isdir(chat_media_dir):
                try:
                    remaining = os.listdir(chat_media_dir)
                    if not remaining:
                        os.rmdir(chat_media_dir)
                        logger.debug(f"Removed empty media directory for chat {chat_id}")
                except Exception as e:
                    logger.debug(f"Could not remove media directory for chat {chat_id}: {e}")

            if deleted_files > 0 or deleted_symlinks > 0 or deleted_records > 0:
                freed_mb = freed_bytes / (1024 * 1024)
                parts = []
                if deleted_files > 0:
                    parts.append(f"{deleted_files} files ({freed_mb:.1f} MB freed)")
                if deleted_symlinks > 0:
                    parts.append(f"{deleted_symlinks} symlinks removed")
                logger.info(
                    f"Cleaned up existing media for chat {chat_id}: "
                    f"{', '.join(parts)}, {deleted_records} DB records deleted"
                )

        except Exception as e:
            logger.error(f"Error cleaning up existing media for chat {chat_id}: {e}", exc_info=True)

    async def _process_media(self, message: Message, chat_id: int) -> dict | None:
        """
        Process and download media from a message.

        Args:
            message: Message object with media
            chat_id: Chat identifier

        Returns:
            Dictionary with media information, or None if skipped
        """
        media = message.media
        media_type = self._get_media_type(media)

        if not media_type:
            return None

        # Generate unique media ID
        media_id = f"{chat_id}_{message.id}_{media_type}"

        # Get Telegram's file unique ID for deduplication
        telegram_file_id = None
        if hasattr(media, "photo"):
            telegram_file_id = str(getattr(media.photo, "id", None))
        elif hasattr(media, "document"):
            telegram_file_id = str(getattr(media.document, "id", None))

        # Check file size (estimated)
        file_size = self._get_media_size(media)
        max_size = self.config.get_max_media_size_bytes()

        if file_size > max_size:
            logger.debug(f"Skipping large media file: {file_size / 1024 / 1024:.2f} MB")
            return {
                "id": media_id,
                "type": media_type,
                "message_id": message.id,
                "chat_id": chat_id,
                "file_size": file_size,
                "downloaded": False,
            }

        # Download media (with optional global deduplication)
        try:
            # Create chat-specific media directory
            chat_media_dir = os.path.join(self.config.media_path, str(chat_id))
            os.makedirs(chat_media_dir, exist_ok=True)

            # Generate filename using file_id for automatic deduplication
            file_name = self._get_media_filename(message, media_type, telegram_file_id)
            file_path = os.path.join(chat_media_dir, file_name)

            # Check if deduplication is enabled
            if getattr(self.config, "deduplicate_media", True):
                # Global deduplication: use _shared directory for actual files
                shared_dir = os.path.join(self.config.media_path, "_shared")
                os.makedirs(shared_dir, exist_ok=True)
                shared_file_path = os.path.join(shared_dir, file_name)

                # Check if file already exists (either directly or in shared)
                if not os.path.exists(file_path):
                    if os.path.exists(shared_file_path):
                        # File exists in shared - create symlink
                        try:
                            # Use relative symlink for portability
                            rel_path = os.path.relpath(shared_file_path, chat_media_dir)
                            os.symlink(rel_path, file_path)
                            logger.debug(f"Created symlink for deduplicated media: {file_name}")
                        except OSError as e:
                            # Symlink failed (e.g., Windows), copy reference instead
                            logger.warning(f"Symlink failed, downloading copy: {e}")
                            await self.client.download_media(message, file_path)
                    else:
                        # First time seeing this file - download to shared and create symlink
                        await self.client.download_media(message, shared_file_path)
                        logger.debug(f"Downloaded media to shared: {file_name}")

                        # Create symlink in chat directory
                        try:
                            rel_path = os.path.relpath(shared_file_path, chat_media_dir)
                            os.symlink(rel_path, file_path)
                        except OSError as e:
                            # Symlink failed - move file to chat dir instead
                            logger.warning(f"Symlink failed, using direct path: {e}")
                            import shutil

                            shutil.move(shared_file_path, file_path)

                # Update file_size with actual size from disk (follow symlinks)
                actual_path = shared_file_path if os.path.exists(shared_file_path) else file_path
                if os.path.exists(actual_path):
                    file_size = os.path.getsize(actual_path)
            else:
                # No deduplication - download directly to chat directory
                if not os.path.exists(file_path):
                    await self.client.download_media(message, file_path)
                    logger.debug(f"Downloaded media: {file_name}")

                # Update file_size with actual size from disk
                if os.path.exists(file_path):
                    file_size = os.path.getsize(file_path)

            # Extract media metadata
            media_data = {
                "id": media_id,
                "type": media_type,
                "message_id": message.id,
                "chat_id": chat_id,
                "file_name": file_name,
                "file_path": file_path,
                "file_size": file_size,
                "mime_type": getattr(media, "mime_type", None),
                "downloaded": True,
                "download_date": datetime.now(),
            }

            # Add type-specific metadata
            if hasattr(media, "photo"):
                photo = media.photo
                media_data["width"] = getattr(photo, "w", None)
                media_data["height"] = getattr(photo, "h", None)
            elif hasattr(media, "document"):
                doc = media.document
                for attr in doc.attributes:
                    if hasattr(attr, "w") and hasattr(attr, "h"):
                        media_data["width"] = attr.w
                        media_data["height"] = attr.h
                    if hasattr(attr, "duration"):
                        media_data["duration"] = attr.duration

            # Return media data - caller is responsible for inserting to database
            # (to ensure message exists before media FK constraint)
            return media_data

        except Exception as e:
            logger.error(f"Error downloading media: {e}")
            return {
                "id": media_id,
                "type": media_type,
                "message_id": message.id,
                "chat_id": chat_id,
                "downloaded": False,
            }

    def _get_media_size(self, media) -> int:
        """Get estimated size of media object in bytes."""
        # Document (Video, Audio, File)
        if hasattr(media, "document") and media.document:
            return getattr(media.document, "size", 0)

        # Photo (find largest size)
        if hasattr(media, "photo") and media.photo:
            sizes = getattr(media.photo, "sizes", [])
            if sizes:
                # Return size of the last one (usually the largest)
                # Some Size types have 'size' field, others don't (like PhotoCachedSize)
                largest = sizes[-1]
                return getattr(largest, "size", 0)

        # Fallback to direct attribute
        return getattr(media, "size", 0)

    def _get_media_type(self, media) -> str | None:
        """Get media type as string."""
        if isinstance(media, MessageMediaPhoto):
            return "photo"
        elif isinstance(media, MessageMediaDocument):
            # Check document attributes to determine specific type
            if hasattr(media, "document") and media.document:
                is_animated = False
                for attr in media.document.attributes:
                    attr_type = type(attr).__name__
                    if "Animated" in attr_type:
                        is_animated = True
                    if "Video" in attr_type:
                        # If animated, it's a GIF
                        return "animation" if is_animated else "video"
                    elif "Audio" in attr_type:
                        # Voice notes have .voice=True on DocumentAttributeAudio
                        if hasattr(attr, "voice") and attr.voice:
                            return "voice"
                        return "audio"
                    elif "Sticker" in attr_type:
                        return "sticker"
                # If animated but no video attribute, still an animation
                if is_animated:
                    return "animation"
            return "document"
        elif isinstance(media, MessageMediaContact):
            return "contact"
        elif isinstance(media, MessageMediaGeo):
            return "geo"
        elif isinstance(media, MessageMediaPoll):
            return "poll"
        return None

    def _get_media_filename(self, message: Message, media_type: str, telegram_file_id: str = None) -> str:
        """
        Generate a unique filename using Telegram's file_id.
        Properly handles files sent "as documents" by checking mime_type and original filename.
        """
        import mimetypes

        # First, try to get original filename from document attributes
        original_name = None
        mime_type = None

        if hasattr(message.media, "document") and message.media.document:
            doc = message.media.document
            mime_type = getattr(doc, "mime_type", None)

            for attr in doc.attributes:
                if hasattr(attr, "file_name") and attr.file_name:
                    original_name = attr.file_name
                    break

        # If we have original filename, use it (with file_id prefix for uniqueness)
        if original_name and telegram_file_id:
            safe_id = str(telegram_file_id).replace("/", "_").replace("\\", "_")
            return f"{safe_id}_{original_name}"

        # Determine extension from mime_type, then fall back to media_type
        extension = None

        if mime_type:
            # Use mimetypes to get proper extension from mime_type
            ext = mimetypes.guess_extension(mime_type)
            if ext:
                extension = ext.lstrip(".")
                # Fix common mimetypes oddities
                if extension == "jpe":
                    extension = "jpg"

        # Fall back to media_type-based extension
        if not extension:
            extension = self._get_media_extension(media_type)

        # Build filename
        if telegram_file_id:
            safe_id = str(telegram_file_id).replace("/", "_").replace("\\", "_")
            return f"{safe_id}.{extension}"

        # Last resort: timestamp-based
        timestamp = message.date.strftime("%Y%m%d_%H%M%S")
        return f"{message.id}_{timestamp}.{extension}"

    def _get_media_extension(self, media_type: str) -> str:
        """Get file extension for media type (fallback only)."""
        extensions = {
            "photo": "jpg",
            "video": "mp4",
            "audio": "mp3",
            "voice": "ogg",
            "document": "bin",  # Only used if mime_type detection fails
        }
        return extensions.get(media_type, "bin")
