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
from .message_utils import METADATA_ONLY_MEDIA_TYPES, sanitize_media_filename
from .parallel_download import (
    ParallelDownloader,
    ParallelDownloadUnavailable,
    supports_parallel_download,
)
from .telegram_stall_guard import TELEGRAM_CALL_TIMEOUT_SECONDS, with_call_timeout

logger = logging.getLogger(__name__)


def media_download_allowed(config, media: object, media_type: str | None) -> bool:
    """The DOWNLOAD_MEDIA_TYPES / DOWNLOAD_DOCUMENT_MIME_TYPES predicate.

    Shared by the scheduled sweep (``BackupMediaMixin._process_media``) and the
    real-time listener (``TelegramListener._download_media``) so both lanes
    decline the same files the same way. Module-level (not a mixin method) so
    the listener can import it without depending on TelegramBackup.
    Config-agnostic on purpose: ``config`` only needs the two predicates
    ``Config`` exposes (``should_download_media_type`` /
    ``document_mime_allowed``), so a double in tests works fine.

    Metadata-only kinds (contact/geo/poll/...) have no file behind them, so a
    download whitelist has no opinion on them -- they always pass.

    Documents get a second, narrower gate: exact MIME match or a filename
    extension derived from the configured MIME types, so a file Telethon
    reports as ``application/octet-stream`` but named ``report.pdf`` still
    passes an ``application/pdf`` whitelist.
    """
    if media_type in METADATA_ONLY_MEDIA_TYPES:
        return True

    if not config.should_download_media_type(media_type):
        return False

    if media_type == "document" and config.download_document_mime_types:
        document = getattr(media, "document", None)
        mime_type = getattr(document, "mime_type", None) if document is not None else None
        file_name = None
        if document is not None:
            for attr in getattr(document, "attributes", None) or ():
                file_name = getattr(attr, "file_name", None)
                if file_name:
                    break
        return config.document_mime_allowed(mime_type, file_name)

    return True


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

            result = await with_call_timeout(
                self.client.download_profile_photo(
                    entity,
                    file=avatar_path,
                    download_big=False,  # Small size is usually sufficient
                ),
                TELEGRAM_CALL_TIMEOUT_SECONDS,
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
        content = None
        if hasattr(media, "photo"):
            content = media.photo
        elif hasattr(media, "document"):
            content = media.document
        content_id = getattr(content, "id", None)
        if content_id is not None:
            telegram_file_id = str(content_id)

        # A photo/document message whose content Telegram no longer holds (an
        # expired view-once or timer photo keeps the media wrapper with no photo
        # inside). There is nothing to download: record it as unavailable. The
        # id used to be str(None), so every such message was filed as "None.jpg",
        # linked to a shared file that never existed and marked downloaded.
        if (hasattr(media, "photo") or hasattr(media, "document")) and content_id is None:
            logger.debug(f"Media content no longer available on Telegram (type: {media_type})")
            return {
                "id": media_id,
                "type": media_type,
                "message_id": message.id,
                "chat_id": chat_id,
                "downloaded": False,
                "skip_reason": "unavailable",
            }

        # Check file size (estimated)
        file_size = self._get_media_size(media)
        max_size = self.config.get_max_media_size_bytes()

        # DOWNLOAD_MEDIA_TYPES / DOWNLOAD_DOCUMENT_MIME_TYPES (opt-in, default
        # OFF): media the operator did not ask for is recorded with its
        # metadata, exactly like the over-size skip below, so the viewer still
        # shows that media existed. Only the bytes stay on Telegram.
        if not media_download_allowed(self.config, media, media_type):
            logger.debug(f"Skipping filtered media (type: {media_type})")
            return {
                "id": media_id,
                "type": media_type,
                "message_id": message.id,
                "chat_id": chat_id,
                "file_size": file_size,
                "downloaded": False,                "downloaded": False,
                "skip_reason": "filtered",  # the viewer says why, not "will download"
            }

        if file_size > max_size:
            logger.debug(f"Skipping large media file: {file_size / 1024 / 1024:.2f} MB")
            return {
                "id": media_id,
                "type": media_type,
                "message_id": message.id,
                "chat_id": chat_id,
                "file_size": file_size,
                "downloaded": False,                "downloaded": False,
                "skip_reason": "oversize",  # the viewer says why, not "will download"
            }

        # Download media (with optional global deduplication)
        try:
            # Create chat-specific media directory
            chat_media_dir = os.path.join(self.config.media_path, str(chat_id))
            os.makedirs(chat_media_dir, exist_ok=True)

            # Generate filename using file_id for automatic deduplication
            file_name = self._get_media_filename(message, media_type, telegram_file_id)
            file_path = os.path.join(chat_media_dir, file_name)
            # Set when this call fetched bytes; a link recorded by an earlier run
            # is kept as downloaded even if its target is unreachable (#143).
            attempted_download = False

            # Check if deduplication is enabled
            if getattr(self.config, "deduplicate_media", True):
                # Global deduplication: use _shared directory for actual files
                shared_dir = os.path.join(self.config.media_path, "_shared")
                os.makedirs(shared_dir, exist_ok=True)
                shared_file_path = os.path.join(shared_dir, file_name)

                # Use lexists for the chat-dir gate so an already-recorded
                # symlink short-circuits the download even when its target is
                # unreachable (e.g. git-annex object outside the bind mount).
                # This keeps re-runs idempotent and never rewrites the link
                # target on a subsequent run (issue #143).
                if not os.path.lexists(file_path):
                    if os.path.exists(shared_file_path):
                        # File exists in shared - create symlink
                        try:
                            # Use relative symlink for portability
                            rel_path = os.path.relpath(shared_file_path, chat_media_dir)
                            os.symlink(rel_path, file_path)
                            logger.debug(f"Created symlink for deduplicated media: {file_name}")
                        except OSError as e:
                            # Symlink failed (e.g., Windows / unsupported FS).
                            # The shared file already exists, so copy it into
                            # the chat dir instead of re-downloading.
                            logger.warning(f"Symlink failed, copying shared file: {e}")
                            import shutil

                            shutil.copy2(shared_file_path, file_path)
                    else:
                        # First time seeing this file - download to shared and create symlink.
                        # Capture the actual path returned by download_media: Telethon may
                        # append an extension (e.g. .bin -> .mp4), so the symlink target must
                        # point at the returned path, not the requested one.
                        attempted_download = True
                        actual_shared_path = await self._download_media_to_path(
                            message, shared_file_path, file_size, chat_id
                        )
                        if isinstance(actual_shared_path, str) and actual_shared_path:
                            shared_file_path = actual_shared_path
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
                # No deduplication - download directly to chat directory.
                # lexists short-circuits when a symlink is already recorded.
                if not os.path.lexists(file_path):
                    attempted_download = True
                    returned_path = await self._download_media_to_path(message, file_path, file_size, chat_id)
                    # Telethon may append the real extension (.bin -> .jpg):
                    # record the file that was actually written.
                    if isinstance(returned_path, str) and returned_path:
                        file_path = returned_path
                        file_name = os.path.basename(returned_path)
                    logger.debug(f"Downloaded media: {file_name}")

                # Update file_size with actual size from disk
                if os.path.exists(file_path):
                    file_size = os.path.getsize(file_path)

            # Only a file that actually landed counts as downloaded. A download
            # that produced nothing must not leave a dangling link behind: the
            # link would make every later run skip this media as already there.
            if attempted_download and not os.path.exists(file_path):
                if os.path.islink(file_path):
                    os.remove(file_path)
                logger.warning("Media download produced no file; leaving it pending for a later run")
                return {
                    "id": media_id,
                    "type": media_type,
                    "message_id": message.id,
                    "chat_id": chat_id,
                    "file_size": file_size,
                    "downloaded": False,
                }

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
                for attr in getattr(doc, "attributes", None) or ():
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

    def _should_parallelize(self, message, file_size: int) -> bool:
        """Decide whether this file should use the parallel chunked path.

        Gated by config (default OFF), a size threshold, and a one-time client
        capability probe. Returns False for anything that should stay on the
        proven single-stream ``download_media`` path.
        """
        # Strict ``is True`` (not truthiness): a real Config sets a bool, while a
        # MagicMock config returns a truthy mock — this keeps the feature off in
        # tests/callers that never opted in, and off by default in production.
        if getattr(self.config, "parallel_download_enabled", False) is not True:
            return False
        if getattr(self, "_parallel_download_disabled", False):
            return False
        if file_size < self.config.get_parallel_download_min_size_bytes():
            return False
        if not supports_parallel_download(self.client):
            # Probe once; if the installed Telethon lacks the internals we need,
            # stop trying for the whole run instead of re-probing every file.
            logger.warning("Parallel download unavailable (Telethon internals missing); using single-stream")
            self._parallel_download_disabled = True
            return False
        return True

    async def _fetch_media_bytes(self, message, tmp_path, file_size: int):
        """Fetch a message's media to ``tmp_path`` (the bytes-fetch primitive).

        Swaps only the transport: callers keep dedup/sharding and the
        ``FileReferenceExpired`` handling. Uses the parallel transferrer for
        large files when enabled, otherwise the single-stream
        ``client.download_media``. A parallel attempt that reports itself
        unavailable transparently falls back to single-stream for that file;
        FloodWait and other real errors propagate unchanged.
        """
        if self._should_parallelize(message, file_size):
            if getattr(self, "_parallel_downloader", None) is None:
                self._parallel_downloader = ParallelDownloader(
                    self.client,
                    connections=self.config.parallel_download_connections,
                    part_size=self.config.get_parallel_download_part_size_bytes(),
                    max_file_size=self.config.get_max_media_size_bytes(),
                    chunk_timeout=getattr(self.config, "parallel_download_chunk_timeout", 120.0),
                )
            try:
                return await self._parallel_downloader.download_media(message, tmp_path)
            except ParallelDownloadUnavailable as exc:
                logger.info("Parallel download not applicable (%s); falling back to single-stream", exc)
        return await self.client.download_media(message, tmp_path)

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
                # DocumentEmpty is truthy but has no .attributes; its reference is
                # unusable, so treat it like a missing document.
                attributes = getattr(media.document, "attributes", None)
                if attributes is None:
                    return None
                is_animated = False
                for attr in attributes:
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
            return None  # document reference unavailable (e.g. forwarded from a private channel)
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

            for attr in getattr(doc, "attributes", None) or ():
                if hasattr(attr, "file_name") and attr.file_name:
                    original_name = attr.file_name
                    break

        # If we have original filename, use it (with file_id prefix for uniqueness)
        if original_name and telegram_file_id:
            safe_id = str(telegram_file_id).replace("/", "_").replace("\\", "_")
            return sanitize_media_filename(f"{safe_id}_{original_name}")

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
