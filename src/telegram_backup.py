"""
Main Telegram backup module.
Handles Telegram client connection, message fetching, and incremental backup logic.
"""

import logging
import os
from datetime import UTC, datetime

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChatForbiddenError,
    UserBannedInChannelError,
)
from telethon.tl.types import (
    Channel,
    Chat,
    User,
)

from .backup_extraction import BackupExtractionMixin
from .backup_media import BackupMediaMixin
from .config import Config
from .db import DatabaseAdapter, create_adapter

logger = logging.getLogger(__name__)


class TelegramBackup(BackupMediaMixin, BackupExtractionMixin):
    """Main class for managing Telegram backups."""

    def __init__(self, config: Config, db: DatabaseAdapter, client: TelegramClient | None = None):
        """
        Initialize Telegram backup manager.

        Args:
            config: Configuration object
            db: Async database adapter (must be initialized before passing)
            client: Optional existing TelegramClient to use (for shared connection).
                   If not provided, will create a new client in connect().
        """
        self.config = config
        self.config.validate_credentials()
        self.db = db
        self.client: TelegramClient | None = client
        self._owns_client = client is None  # Track if we created the client
        self._cleaned_media_chats: set[int] = set()  # Track chats already cleaned this session

        logger.info("TelegramBackup initialized")

    @classmethod
    async def create(cls, config: Config, client: TelegramClient | None = None) -> "TelegramBackup":
        """
        Factory method to create TelegramBackup with initialized database.

        Args:
            config: Configuration object
            client: Optional existing TelegramClient to use (for shared connection)

        Returns:
            Initialized TelegramBackup instance
        """
        db = await create_adapter()
        return cls(config, db, client=client)

    async def connect(self):
        """
        Connect to Telegram and authenticate.

        If a client was provided in __init__, verifies it's connected.
        Otherwise, creates a new client and connects.
        """
        # If using shared client, just verify it's connected
        if self.client is not None and not self._owns_client:
            if not self.client.is_connected():
                raise RuntimeError("Shared client is not connected")
            logger.debug("Using shared Telegram client")
            return

        # Create new client
        self.client = TelegramClient(self.config.session_path, self.config.api_id, self.config.api_hash)
        self._owns_client = True

        # Fix for database locked errors: Enable WAL mode for session DB
        # This is critical for concurrency when the viewer is also running
        try:
            if hasattr(self.client.session, "_conn"):
                # Ensure connection is open
                if self.client.session._conn is None:
                    # Trigger connection if lazy loaded (though usually it's open)
                    pass

                if self.client.session._conn:
                    self.client.session._conn.execute("PRAGMA journal_mode=WAL")
                    self.client.session._conn.execute("PRAGMA busy_timeout=30000")
                    logger.info("Enabled WAL mode for Telethon session database")
        except Exception as e:
            logger.warning(f"Could not enable WAL mode for session DB: {e}")

        # Connect without starting interactive flow
        await self.client.connect()

        # Check authorization status
        if not await self.client.is_user_authorized():
            logger.error("❌ Session not authorized!")
            logger.error("Please run the authentication setup first:")
            logger.error("  Docker: ./init_auth.bat (Windows) or ./init_auth.sh (Linux/Mac)")
            logger.error("  Local:  python -m src.setup_auth")
            raise RuntimeError("Session not authorized. Please run authentication setup.")

        me = await self.client.get_me()
        logger.info(f"Connected as {me.first_name} ({me.phone})")

    async def disconnect(self):
        """
        Disconnect from Telegram.

        Only disconnects if we own the client (created it ourselves).
        Shared clients are managed by the connection owner.
        """
        if self.client and self._owns_client:
            await self.client.disconnect()
            logger.info("Disconnected from Telegram")

    async def backup_all(self):
        """
        Perform backup of all configured chats.
        This is the main entry point for scheduled backups.
        """
        try:
            logger.info("Starting backup process...")

            # Connect to Telegram
            logger.info("Connecting to Telegram...")
            await self.client.start(phone=self.config.phone)

            # Get current user info
            me = await self.client.get_me()
            logger.info(f"Logged in as {me.first_name} ({me.id})")

            # Store owner ID and backfill is_outgoing for existing messages
            await self.db.set_metadata("owner_id", str(me.id))
            await self.db.backfill_is_outgoing(me.id)

            start_time = datetime.now()

            # Store last backup time in UTC at the START of backup (not when it finishes)
            last_backup_time = datetime.utcnow().isoformat() + "Z"
            await self.db.set_metadata("last_backup_time", last_backup_time)

            # Get all dialogs (chats)
            logger.info("Fetching dialog list...")
            dialogs = await self._get_dialogs()
            logger.info(f"Found {len(dialogs)} total dialogs")

            # v6.2.0: Fetch archived dialogs
            logger.info("Fetching archived dialogs...")
            archived_dialogs = await self._get_dialogs(archived=True)
            logger.info(f"Found {len(archived_dialogs)} archived dialogs")

            # Build set of archived chat IDs for fast lookup.
            # Only trust this for chats NOT found in the regular dialog list,
            # since Telegram's API may occasionally return a chat in both lists.
            archived_chat_ids = set()
            for dialog in archived_dialogs:
                archived_chat_ids.add(self._get_marked_id(dialog.entity))
            logger.info(
                f"Archived chat IDs from Telegram: {archived_chat_ids & (self.config.global_include_ids | self.config.private_include_ids | self.config.groups_include_ids | self.config.channels_include_ids) if archived_chat_ids else 'none matching includes'}"
            )

            # Filter dialogs based on chat type and ID filters
            # Also delete explicitly excluded chats from database
            filtered_dialogs = []
            explicitly_excluded_chat_ids = set()
            seen_chat_ids = set()  # Track which IDs we've processed from dialogs

            for dialog in dialogs:
                entity = dialog.entity
                # Use marked ID (with -100 prefix for channels/supergroups) to match user config
                chat_id = self._get_marked_id(entity)
                seen_chat_ids.add(chat_id)

                is_bot = isinstance(entity, User) and entity.bot
                is_user = isinstance(entity, User) and not entity.bot
                is_group = isinstance(entity, Chat) or (isinstance(entity, Channel) and entity.megagroup)
                is_channel = isinstance(entity, Channel) and not entity.megagroup

                # Check if chat is explicitly in an exclude list (not just filtered out)
                is_explicitly_excluded = (
                    chat_id in self.config.global_exclude_ids
                    or ((is_user or is_bot) and chat_id in self.config.private_exclude_ids)
                    or (is_group and chat_id in self.config.groups_exclude_ids)
                    or (is_channel and chat_id in self.config.channels_exclude_ids)
                )

                if is_explicitly_excluded:
                    # Chat is explicitly excluded - mark for deletion
                    explicitly_excluded_chat_ids.add(chat_id)
                elif self.config.should_backup_chat(chat_id, is_user, is_group, is_channel, is_bot):
                    # Chat should be backed up
                    filtered_dialogs.append(dialog)

            # Fetch explicitly included chats that weren't in dialogs
            # This handles cases where chats don't appear in the dialog list
            # (newly created, archived, or not recently messaged)
            all_include_ids = (
                self.config.global_include_ids
                | self.config.private_include_ids
                | self.config.groups_include_ids
                | self.config.channels_include_ids
            )
            missing_include_ids = all_include_ids - seen_chat_ids - explicitly_excluded_chat_ids

            if missing_include_ids:
                logger.info(
                    f"Fetching {len(missing_include_ids)} explicitly included chats not in regular dialogs: {missing_include_ids}"
                )
                for include_id in missing_include_ids:
                    is_in_archive = include_id in archived_chat_ids
                    try:
                        entity = await self.client.get_entity(include_id)

                        # Create a simple dialog-like wrapper
                        class SimpleDialog:
                            def __init__(self, entity):
                                self.entity = entity
                                self.date = datetime.now()

                        filtered_dialogs.append(SimpleDialog(entity))
                        logger.info(
                            f"  → Added: {self._get_chat_name(entity)} (ID: {include_id}){' [in archive]' if is_in_archive else ' [not in any dialog list]'}"
                        )
                    except Exception as e:
                        logger.warning(f"  → Could not fetch included chat {include_id}: {e}")

            # Delete only explicitly excluded chats from database
            if explicitly_excluded_chat_ids:
                logger.info(f"Deleting {len(explicitly_excluded_chat_ids)} explicitly excluded chats from database...")
                for chat_id in explicitly_excluded_chat_ids:
                    try:
                        await self.db.delete_chat_and_related_data(chat_id, self.config.media_path)
                    except Exception as e:
                        logger.error(f"Error deleting chat {chat_id}: {e}", exc_info=True)

            logger.info(f"Backing up {len(filtered_dialogs)} dialogs after filtering")

            if not filtered_dialogs:
                logger.info("No dialogs to back up after filtering")
                return

            # Sort dialogs: priority chats first, then by most recently active
            # Priority chats (PRIORITY_CHAT_IDS) are always processed first
            # Use .timestamp() to avoid comparing timezone-aware vs naive datetimes
            # (Saved Messages chat has UTC timezone, others may be naive)
            # Fixes: https://github.com/GeiserX/Telegram-Archive/issues/12
            priority_ids = self.config.priority_chat_ids

            def dialog_sort_key(d):
                chat_id = self._get_marked_id(d.entity)
                is_priority = chat_id in priority_ids
                timestamp = (getattr(d, "date", None) or datetime.min.replace(tzinfo=UTC)).timestamp()
                # Sort by: (not is_priority, -timestamp) so priority=True sorts first, then by recency
                return (not is_priority, -timestamp)

            filtered_dialogs.sort(key=dialog_sort_key)

            # Log priority chats if any
            if priority_ids:
                priority_count = sum(1 for d in filtered_dialogs if self._get_marked_id(d.entity) in priority_ids)
                if priority_count > 0:
                    logger.info(f"📌 {priority_count} priority chat(s) will be processed first")

            # Bulk-load sync status for smart skip (Phase 1)
            sync_map = await self.db.get_all_last_message_ids()
            has_synced_before = any(v > 0 for v in sync_map.values())

            # Backup each dialog
            # v6.2.0: Check archived_chat_ids so chats in both INCLUDE_CHAT_IDS
            # and the archived folder get the correct is_archived flag immediately.
            # A chat found in the regular dialog list (seen_chat_ids) is NEVER
            # archived, even if Telegram's API also returns it in folder=1.
            total_messages = 0
            backed_up_chat_ids = set()
            skipped_chats = 0
            consecutive_empty = 0
            early_stop_threshold = self.config.early_stop_threshold

            for i, dialog in enumerate(filtered_dialogs, 1):
                entity = dialog.entity
                chat_id = self._get_marked_id(entity)
                chat_name = self._get_chat_name(entity)
                is_archived = chat_id in archived_chat_ids and chat_id not in seen_chat_ids
                if chat_id in archived_chat_ids and chat_id in seen_chat_ids:
                    logger.warning(
                        f"  Chat {chat_name} (ID: {chat_id}) appears in both regular and archived dialog lists - treating as NOT archived"
                    )

                # Phase 1: Smart skip — compare dialog's top message with last synced ID
                last_synced = sync_map.get(chat_id, 0)
                dialog_top_id = dialog.message.id if dialog.message else 0

                if has_synced_before and last_synced > 0 and dialog_top_id <= last_synced:
                    # No new messages — update metadata only
                    chat_data = self._extract_chat_data(entity, is_archived=is_archived)
                    await self.db.upsert_chat(chat_data)
                    backed_up_chat_ids.add(chat_id)
                    skipped_chats += 1
                    consecutive_empty += 1

                    # Phase 2: Early termination after N consecutive empty chats
                    if (early_stop_threshold > 0
                            and consecutive_empty >= early_stop_threshold
                            and i > 10):
                        logger.info(
                            f"Early stop at chat {i}/{len(filtered_dialogs)}: "
                            f"{consecutive_empty} consecutive chats with no new messages"
                        )
                        break
                    continue

                label = f"[{i}/{len(filtered_dialogs)}] Backing up{' (archived)' if is_archived else ''}: {chat_name} (ID: {chat_id})"
                logger.info(label)

                try:
                    message_count = await self._backup_dialog(dialog, is_archived=is_archived)
                    total_messages += message_count
                    backed_up_chat_ids.add(chat_id)
                    logger.info(f"  → Backed up {message_count} new messages")

                    if message_count == 0:
                        consecutive_empty += 1
                        # Phase 2: Early termination also for full-scanned empty chats
                        if (has_synced_before
                                and early_stop_threshold > 0
                                and consecutive_empty >= early_stop_threshold
                                and i > 10):
                            logger.info(
                                f"Early stop at chat {i}/{len(filtered_dialogs)}: "
                                f"{consecutive_empty} consecutive chats with no new messages"
                            )
                            break
                    else:
                        consecutive_empty = 0

                except (ChannelPrivateError, ChatForbiddenError, UserBannedInChannelError) as e:
                    logger.warning(f"  → Skipped (no access): {e.__class__.__name__}")
                except Exception as e:
                    logger.error(f"  → Error backing up {chat_name}: {e}", exc_info=True)

            if skipped_chats:
                logger.info(f"Smart skip: {skipped_chats} chats had no new messages (metadata updated)")

            # v6.2.0: Backup archived dialogs that weren't already processed above.
            # Apply the same chat type/ID filters so we don't back up unintended chats.
            archived_to_backup = []
            for dialog in archived_dialogs:
                entity = dialog.entity
                chat_id = self._get_marked_id(entity)
                if chat_id in backed_up_chat_ids:
                    continue  # Already backed up with correct is_archived flag
                if chat_id in explicitly_excluded_chat_ids:
                    continue

                is_bot = isinstance(entity, User) and entity.bot
                is_user = isinstance(entity, User) and not entity.bot
                is_group = isinstance(entity, Chat) or (isinstance(entity, Channel) and entity.megagroup)
                is_channel = isinstance(entity, Channel) and not entity.megagroup

                if self.config.should_backup_chat(chat_id, is_user, is_group, is_channel, is_bot):
                    archived_to_backup.append(dialog)

            if archived_to_backup:
                logger.info(f"Backing up {len(archived_to_backup)} additional archived dialogs...")
                for i, dialog in enumerate(archived_to_backup, 1):
                    entity = dialog.entity
                    chat_id = self._get_marked_id(entity)
                    chat_name = self._get_chat_name(entity)
                    logger.info(f"  [Archived {i}/{len(archived_to_backup)}] {chat_name} (ID: {chat_id})")

                    try:
                        message_count = await self._backup_dialog(dialog, is_archived=True)
                        total_messages += message_count
                        backed_up_chat_ids.add(chat_id)
                        if message_count > 0:
                            logger.info(f"    → Backed up {message_count} new messages")
                    except (ChannelPrivateError, ChatForbiddenError, UserBannedInChannelError) as e:
                        logger.warning(f"    → Skipped (no access): {e.__class__.__name__}")
                    except Exception as e:
                        logger.error(f"    → Error: {e}", exc_info=True)
            else:
                logger.info("No additional archived dialogs to back up")

            # v6.2.0: Backup forum topics for forum-enabled chats
            logger.info("Checking for forum topics...")
            all_backed_up_dialogs = list(filtered_dialogs) + list(archived_to_backup)
            for dialog in all_backed_up_dialogs:
                entity = dialog.entity
                if isinstance(entity, Channel) and getattr(entity, "forum", False):
                    chat_id = self._get_marked_id(entity)
                    chat_name = self._get_chat_name(entity)
                    logger.info(f"  → Fetching topics for forum: {chat_name}")
                    await self._backup_forum_topics(chat_id, entity)

            # v6.2.0: Backup user's chat folders
            logger.info("Backing up chat folders...")
            await self._backup_folders()

            # Calculate and cache statistics (also updates metadata for the viewer)
            duration = (datetime.now() - start_time).total_seconds()
            stats = await self.db.calculate_and_store_statistics()

            # Note: last_backup_time is stored at the START of backup (see beginning of backup_all)

            logger.info("=" * 60)
            logger.info("Backup completed successfully!")
            logger.info(f"Duration: {duration:.2f} seconds")
            logger.info(f"New messages: {total_messages}")
            logger.info(f"Total chats: {stats['chats']}")
            logger.info(f"Total messages: {stats['messages']}")
            logger.info(f"Total media files: {stats['media_files']}")
            logger.info(f"Total storage: {stats['total_size_mb']} MB")
            logger.info("=" * 60)

            # Run media verification if enabled
            if self.config.verify_media:
                await self._verify_and_redownload_media()

        except Exception as e:
            logger.error(f"Backup failed: {e}", exc_info=True)
            raise

    async def _get_dialogs(self, archived: bool = False) -> list:
        """
        Get all dialogs (chats) from Telegram.

        Args:
            archived: If True, fetch archived dialogs (folder=1)

        Returns:
            List of dialog objects

        Note: folder=0 explicitly fetches non-archived dialogs only.
        Without folder parameter, Telethon returns ALL dialogs including
        archived ones, which causes overlap with the folder=1 results.
        """
        if archived:
            dialogs = await self.client.get_dialogs(folder=1)
        else:
            dialogs = await self.client.get_dialogs(folder=0)
        return dialogs

    async def _verify_and_redownload_media(self) -> None:
        """
        Verify all media files on disk and re-download missing/corrupted ones.

        This method:
        1. Queries all media records marked as downloaded
        2. Checks if files exist on disk
        3. Optionally verifies file size matches DB record
        4. Re-downloads missing/corrupted files from Telegram

        Edge cases handled:
        - File missing on disk: re-download
        - File is 0 bytes: re-download (interrupted download)
        - File size mismatch: re-download (corrupted)
        - Message deleted on Telegram: log warning, skip
        - Chat inaccessible: log warning, skip chat
        - Media expired: log warning, skip
        """
        logger.info("=" * 60)
        logger.info("Starting media verification...")

        media_records = await self.db.get_media_for_verification()
        logger.info(f"Found {len(media_records)} media records to verify")

        missing_files = []
        corrupted_files = []

        # Phase 1: Check which files need re-downloading
        for record in media_records:
            file_path = record.get("file_path")
            if not file_path:
                continue

            # Check if file exists
            if not os.path.exists(file_path):
                missing_files.append(record)
                continue

            # Check if file is empty (interrupted download)
            if os.path.getsize(file_path) == 0:
                corrupted_files.append(record)
                continue

            # Check file size matches (if we have the expected size)
            expected_size = record.get("file_size")
            if expected_size and expected_size > 0:
                actual_size = os.path.getsize(file_path)
                # Allow 1% tolerance for size differences (encoding variations)
                if abs(actual_size - expected_size) > expected_size * 0.01:
                    corrupted_files.append(record)

        total_issues = len(missing_files) + len(corrupted_files)
        if total_issues == 0:
            logger.info("✓ All media files verified - no issues found")
            logger.info("=" * 60)
            return

        logger.info(f"Found {len(missing_files)} missing files, {len(corrupted_files)} corrupted files")
        logger.info("Starting re-download process...")

        # Phase 2: Re-download missing/corrupted files
        files_to_redownload = missing_files + corrupted_files

        # Group by chat_id for efficient fetching
        by_chat: dict[int, list[dict]] = {}
        for record in files_to_redownload:
            chat_id = record.get("chat_id")
            if chat_id:
                by_chat.setdefault(chat_id, []).append(record)

        redownloaded = 0
        failed = 0

        for chat_id, records in by_chat.items():
            # Skip media verification for chats in skip list
            if chat_id in self.config.skip_media_chat_ids:
                logger.debug(f"Skipping media verification for chat {chat_id} (in SKIP_MEDIA_CHAT_IDS)")
                continue

            try:
                # Get message IDs to fetch
                message_ids = [r["message_id"] for r in records if r.get("message_id")]
                if not message_ids:
                    continue

                # Fetch messages from Telegram in batch
                try:
                    messages = await self.client.get_messages(chat_id, ids=message_ids)
                except Exception as e:
                    logger.warning(f"Cannot access chat {chat_id} for media verification: {e}")
                    failed += len(records)
                    continue

                # Create a map of message_id -> message
                msg_map = {}
                for msg in messages:
                    if msg:  # msg can be None if message was deleted
                        msg_map[msg.id] = msg

                # Re-download each file
                for record in records:
                    msg_id = record.get("message_id")
                    msg = msg_map.get(msg_id)

                    if not msg:
                        logger.warning(f"Message {msg_id} in chat {chat_id} was deleted - cannot recover media")
                        failed += 1
                        continue

                    if not msg.media:
                        logger.warning(f"Message {msg_id} no longer has media - cannot recover")
                        failed += 1
                        continue

                    try:
                        # Delete corrupted file if exists
                        file_path = record.get("file_path")
                        if file_path and os.path.exists(file_path):
                            os.remove(file_path)

                        # Re-download using existing method
                        result = await self._process_media(msg, chat_id)
                        if result and result.get("downloaded"):
                            # Insert media record (message already exists for re-downloads)
                            await self.db.insert_media(result)
                            redownloaded += 1
                            logger.debug(f"Re-downloaded media for message {msg_id}")
                        else:
                            failed += 1
                            logger.warning(f"Failed to re-download media for message {msg_id}")
                    except Exception as e:
                        failed += 1
                        logger.error(f"Error re-downloading media for message {msg_id}: {e}")

            except Exception as e:
                logger.error(f"Error processing chat {chat_id} for media verification: {e}")
                failed += len(records)

        logger.info("=" * 60)
        logger.info("Media verification completed!")
        logger.info(f"Re-downloaded: {redownloaded} files")
        logger.info(f"Failed/Unrecoverable: {failed} files")
        logger.info("=" * 60)

    async def _backup_dialog(self, dialog, is_archived: bool = False) -> int:
        """
        Backup a single dialog (chat).

        Args:
            dialog: Dialog object from Telegram
            is_archived: Whether this dialog is from the archived folder

        Returns:
            Number of new messages backed up
        """
        entity = dialog.entity
        # Use marked ID (with -100 prefix for channels/supergroups) for consistency
        chat_id = self._get_marked_id(entity)

        # Save chat information
        chat_data = self._extract_chat_data(entity, is_archived=is_archived)
        await self.db.upsert_chat(chat_data)

        # Clean up existing media if this chat is in the skip list (once per session)
        if (
            chat_id in self.config.skip_media_chat_ids
            and self.config.skip_media_delete_existing
            and chat_id not in self._cleaned_media_chats
        ):
            await self._cleanup_existing_media(chat_id)
            self._cleaned_media_chats.add(chat_id)

        # Ensure profile photos for users and groups/channels are backed up.
        # This runs on every dialog backup but only downloads new files when
        # Telegram reports a different profile photo.
        try:
            await self._ensure_profile_photo(entity, chat_id)
        except Exception as e:
            logger.error(f"Error downloading profile photo for {chat_id}: {e}", exc_info=True)

        # Get last synced message ID for incremental backup
        last_message_id = await self.db.get_last_message_id(chat_id)

        # Phase 3: Reverse-first fetch — newest messages first so they appear
        # in the viewer immediately. Stop when we reach already-synced messages.
        # On initial backup (last_message_id == 0), use forward order for efficiency.
        batch_data: list[dict] = []
        batch_size = self.config.batch_size
        grand_total = 0
        running_max_id = last_message_id

        if last_message_id > 0:
            # Incremental: fetch newest first, stop at last synced
            async for message in self.client.iter_messages(entity):
                if message.id <= last_message_id:
                    break
                msg_data = await self._process_message(message, chat_id)
                batch_data.append(msg_data)
                running_max_id = max(running_max_id, message.id)

                if len(batch_data) >= batch_size:
                    await self._commit_batch(batch_data, chat_id)
                    grand_total += len(batch_data)
                    logger.info(f"  → Processed {grand_total} messages...")
                    batch_data = []
        else:
            # Initial backup: forward order (old→new) for completeness
            async for message in self.client.iter_messages(entity, reverse=True):
                msg_data = await self._process_message(message, chat_id)
                batch_data.append(msg_data)
                running_max_id = max(running_max_id, message.id)

                if len(batch_data) >= batch_size:
                    await self._commit_batch(batch_data, chat_id)
                    grand_total += len(batch_data)
                    logger.info(f"  → Processed {grand_total} messages...")
                    batch_data = []

        # Flush remaining messages
        if batch_data:
            await self._commit_batch(batch_data, chat_id)
            grand_total += len(batch_data)

        # Update sync status with highest message ID
        if grand_total > 0:
            await self.db.update_sync_status(chat_id, running_max_id, grand_total)

        # Sync deletions and edits if enabled (expensive!)
        if self.config.sync_deletions_edits:
            await self._sync_deletions_and_edits(chat_id, entity)

        # Always sync pinned messages to keep them up-to-date
        await self._sync_pinned_messages(chat_id, entity)

        return grand_total

    async def _commit_batch(self, batch_data: list[dict], chat_id: int) -> None:
        """Persist a batch of processed messages, their media and reactions to the DB."""
        await self.db.insert_messages_batch(batch_data)

        for msg in batch_data:
            if msg.get("_media_data"):
                await self.db.insert_media(msg["_media_data"])

        for msg in batch_data:
            if msg.get("reactions"):
                reactions_list: list[dict] = []
                for reaction in msg["reactions"]:
                    if reaction.get("user_ids") and len(reaction["user_ids"]) > 0:
                        for user_id in reaction["user_ids"]:
                            reactions_list.append({"emoji": reaction["emoji"], "user_id": user_id, "count": 1})
                        remaining = reaction.get("count", 0) - len(reaction["user_ids"])
                        if remaining > 0:
                            reactions_list.append({"emoji": reaction["emoji"], "user_id": None, "count": remaining})
                    else:
                        reactions_list.append(
                            {"emoji": reaction["emoji"], "user_id": None, "count": reaction.get("count", 1)}
                        )
                if reactions_list:
                    await self.db.insert_reactions(msg["id"], chat_id, reactions_list)

    async def _fill_gap_range(self, entity, chat_id: int, gap_start: int, gap_end: int) -> int:
        """Fetch messages for a single gap range and insert them.

        Args:
            entity: Telegram entity for the chat.
            chat_id: Marked chat ID.
            gap_start: Last message ID before the gap (exclusive lower bound).
            gap_end: First message ID after the gap (exclusive upper bound).

        Returns:
            Number of messages recovered.
        """
        batch_data: list[dict] = []
        batch_size = self.config.batch_size
        total = 0

        async for message in self.client.iter_messages(
            entity, min_id=gap_start, max_id=gap_end, reverse=True
        ):
            msg_data = await self._process_message(message, chat_id)
            batch_data.append(msg_data)

            if len(batch_data) >= batch_size:
                await self._commit_batch(batch_data, chat_id)
                total += len(batch_data)
                batch_data = []

        if batch_data:
            await self._commit_batch(batch_data, chat_id)
            total += len(batch_data)

        return total

    async def _fill_gaps(self, chat_id: int | None = None) -> dict:
        """Detect and fill message gaps for backed-up chats.

        Args:
            chat_id: If provided, scan only this chat. Otherwise scan all.

        Returns:
            Summary dict with total_gaps, total_recovered, per_chat details.
        """
        threshold = self.config.gap_threshold

        if chat_id:
            chat_ids = [chat_id]
        else:
            chat_ids = await self.db.get_chats_with_messages()

        total_gaps = 0
        total_recovered = 0
        per_chat: list[dict] = []

        for cid in chat_ids:
            gaps = await self.db.detect_message_gaps(cid, threshold)
            if not gaps:
                continue

            total_gaps += len(gaps)
            chat_recovered = 0

            try:
                entity = await self.client.get_entity(cid)
            except (ChannelPrivateError, ChatForbiddenError, UserBannedInChannelError):
                logger.warning(f"Gap-fill: skipping chat {cid} (no access)")
                continue
            except Exception as e:
                logger.error(f"Gap-fill: cannot get entity for chat {cid}: {e}")
                continue

            chat_name = self._get_chat_name(entity)
            logger.info(f"Gap-fill: {chat_name} (ID: {cid}) — {len(gaps)} gap(s) detected")

            for gap_start, gap_end, gap_size in gaps:
                logger.info(f"  → Gap [{gap_start}..{gap_end}] (~{gap_size} IDs missing)")
                try:
                    recovered = await self._fill_gap_range(entity, cid, gap_start, gap_end)
                    chat_recovered += recovered
                    if recovered > 0:
                        logger.info(f"    Recovered {recovered} messages")
                    else:
                        logger.info("    No messages found (likely deleted)")
                except Exception as e:
                    logger.error(f"    Error filling gap: {e}", exc_info=True)

            total_recovered += chat_recovered
            per_chat.append({
                "chat_id": cid,
                "chat_name": chat_name,
                "gaps": len(gaps),
                "recovered": chat_recovered,
            })

        summary = {
            "chats_scanned": len(chat_ids),
            "chats_with_gaps": len(per_chat),
            "total_gaps": total_gaps,
            "total_recovered": total_recovered,
            "details": per_chat,
        }

        logger.info("=" * 60)
        logger.info("Gap-fill completed!")
        logger.info(f"Chats scanned: {summary['chats_scanned']}")
        logger.info(f"Chats with gaps: {summary['chats_with_gaps']}")
        logger.info(f"Total gaps: {summary['total_gaps']}")
        logger.info(f"Messages recovered: {summary['total_recovered']}")
        logger.info("=" * 60)

        return summary

    async def _sync_deletions_and_edits(self, chat_id: int, entity):
        """
        Sync deletions and edits for existing messages in the database.

        Args:
            chat_id: Chat ID to sync
            entity: Telegram entity
        """
        logger.info(f"  → Syncing deletions and edits for chat {chat_id}...")

        # Get all local message IDs and their edit dates
        local_messages = await self.db.get_messages_sync_data(chat_id)
        if not local_messages:
            return

        local_ids = list(local_messages.keys())
        total_checked = 0
        total_deleted = 0
        total_updated = 0

        # Process in batches
        batch_size = 100
        for i in range(0, len(local_ids), batch_size):
            batch_ids = local_ids[i : i + batch_size]

            try:
                # Fetch current state from Telegram
                remote_messages = await self.client.get_messages(entity, ids=batch_ids)

                for msg_id, remote_msg in zip(batch_ids, remote_messages):
                    # Check for deletion
                    if remote_msg is None:
                        await self.db.delete_message(chat_id, msg_id)
                        total_deleted += 1
                        continue

                    # Check for edits
                    # We compare string representations of edit_date
                    remote_edit_date = remote_msg.edit_date
                    local_edit_date_str = local_messages[msg_id]

                    should_update = False

                    if remote_edit_date:
                        # If remote has edit_date, check if it differs from local
                        # This handles cases where local is None or different
                        if str(remote_edit_date) != str(local_edit_date_str):
                            should_update = True

                    if should_update:
                        # Update text and edit_date
                        await self.db.update_message_text(chat_id, msg_id, remote_msg.message, remote_msg.edit_date)
                        total_updated += 1

            except Exception as e:
                logger.error(f"Error syncing batch for chat {chat_id}: {e}")

            total_checked += len(batch_ids)
            if total_checked % 1000 == 0:
                logger.info(f"  → Checked {total_checked}/{len(local_ids)} messages for sync...")

        if total_deleted > 0 or total_updated > 0:
            logger.info(f"  → Sync result: {total_deleted} deleted, {total_updated} updated")

    async def _sync_pinned_messages(self, chat_id: int, entity) -> None:
        """
        Sync pinned messages for a chat.

        Fetches all currently pinned messages from Telegram using the
        InputMessagesFilterPinned filter and updates the is_pinned field
        in the database.

        This ensures pinned status is always up-to-date after each backup,
        catching both newly pinned and unpinned messages.

        Args:
            chat_id: Chat ID (marked format)
            entity: Telegram entity
        """
        try:
            from telethon.tl.types import InputMessagesFilterPinned

            # Fetch all pinned messages from Telegram (up to 100)
            pinned_messages = await self.client.get_messages(entity, filter=InputMessagesFilterPinned(), limit=100)

            if pinned_messages:
                pinned_ids = [msg.id for msg in pinned_messages]
                await self.db.sync_pinned_messages(chat_id, pinned_ids)
                logger.debug(f"  → Synced {len(pinned_ids)} pinned messages")
            else:
                # No pinned messages - clear any existing
                await self.db.sync_pinned_messages(chat_id, [])

        except Exception as e:
            # Don't fail the backup if pinned sync fails
            logger.debug(f"  → Could not sync pinned messages: {e}")

    async def _backup_forum_topics(self, chat_id: int, entity) -> int:
        """
        Fetch and store forum topics for a forum-enabled chat.

        Uses message metadata to infer topics when GetForumTopicsRequest
        is not available in the current Telethon version.

        Args:
            chat_id: Chat ID (marked format)
            entity: Telegram entity

        Returns:
            Number of topics found
        """
        try:
            # Try using GetForumTopicsRequest via raw API
            # Note: In Telethon 1.42+, this is in messages, not channels
            from telethon.tl.functions.messages import GetForumTopicsRequest

            try:
                input_channel = await self.client.get_input_entity(entity)
                # offset_date must be a proper date object, not int 0
                from datetime import datetime as dt

                result = await self.client(
                    GetForumTopicsRequest(
                        peer=input_channel, offset_date=dt(1970, 1, 1), offset_id=0, offset_topic=0, limit=100
                    )
                )

                # Resolve custom emoji IDs to unicode emojis
                emoji_map = {}
                emoji_ids = [t.icon_emoji_id for t in result.topics if getattr(t, "icon_emoji_id", None)]
                if emoji_ids:
                    try:
                        from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest

                        docs = await self.client(GetCustomEmojiDocumentsRequest(document_id=emoji_ids))
                        for doc in docs:
                            for attr in doc.attributes:
                                if hasattr(attr, "alt") and attr.alt:
                                    emoji_map[doc.id] = attr.alt
                                    break
                        logger.info(f"  → Resolved {len(emoji_map)} topic emojis")
                    except Exception as e:
                        logger.warning(f"  → Could not resolve topic emojis: {e}")

                topics_count = 0
                for topic in result.topics:
                    emoji_id = getattr(topic, "icon_emoji_id", None)
                    topic_data = {
                        "id": topic.id,
                        "chat_id": chat_id,
                        "title": topic.title,
                        "icon_color": getattr(topic, "icon_color", None),
                        "icon_emoji_id": emoji_id,
                        "icon_emoji": emoji_map.get(emoji_id) if emoji_id else None,
                        "is_closed": 1 if getattr(topic, "closed", False) else 0,
                        "is_pinned": 1 if getattr(topic, "pinned", False) else 0,
                        "is_hidden": 1 if getattr(topic, "hidden", False) else 0,
                        "date": getattr(topic, "date", None),
                    }
                    await self.db.upsert_forum_topic(topic_data)
                    topics_count += 1

                logger.info(f"  → Backed up {topics_count} forum topics via API")
                return topics_count

            except Exception as e:
                logger.warning(
                    f"GetForumTopicsRequest failed ({e.__class__.__name__}: {e}), falling back to message inference"
                )
                # Fall through to inference method
        except ImportError:
            logger.warning("GetForumTopicsRequest not available in this Telethon version, using message inference")

        # Fallback: Infer topics from message reply_to_top_id values
        # This finds unique topic IDs and uses the topic's first message as metadata
        try:
            from sqlalchemy import distinct, select

            from .db.models import Message as MessageModel

            async with self.db.db_manager.async_session_factory() as session:
                # Get unique reply_to_top_id values for this chat
                stmt = (
                    select(distinct(MessageModel.reply_to_top_id))
                    .where(MessageModel.chat_id == chat_id)
                    .where(MessageModel.reply_to_top_id.isnot(None))
                )
                result = await session.execute(stmt)
                topic_ids = [row[0] for row in result]

            topics_count = 0
            for topic_id in topic_ids:
                # Try to get the topic's first message for metadata
                try:
                    msgs = await self.client.get_messages(entity, ids=[topic_id])
                    if msgs and msgs[0]:
                        msg = msgs[0]
                        topic_data = {
                            "id": topic_id,
                            "chat_id": chat_id,
                            "title": msg.text[:100] if msg.text else f"Topic {topic_id}",
                            "date": msg.date,
                        }
                        await self.db.upsert_forum_topic(topic_data)
                        topics_count += 1
                except Exception as e:
                    logger.debug(f"Could not fetch topic {topic_id} metadata: {e}")

            if topics_count > 0:
                logger.info(f"  → Inferred {topics_count} forum topics from messages")
            return topics_count

        except Exception as e:
            logger.warning(f"  → Failed to infer forum topics: {e}")
            return 0

    async def _backup_folders(self) -> int:
        """
        Fetch and store user's Telegram chat folders (dialog filters).

        Returns:
            Number of folders backed up
        """
        try:
            from telethon.tl.functions.messages import GetDialogFiltersRequest

            result = await self.client(GetDialogFiltersRequest())

            # result might be a list directly or have a .filters attribute
            filters = result.filters if hasattr(result, "filters") else result

            folder_count = 0
            active_folder_ids = []

            for idx, f in enumerate(filters):
                # Skip the default "All" filter
                if not hasattr(f, "id") or not hasattr(f, "title"):
                    continue

                folder_id = f.id
                # Handle title - might be string or TextWithEntities
                title = f.title
                if hasattr(title, "text"):
                    title = title.text
                title = str(title)

                active_folder_ids.append(folder_id)

                folder_data = {
                    "id": folder_id,
                    "title": title,
                    "emoticon": getattr(f, "emoticon", None),
                    "sort_order": idx,
                }
                await self.db.upsert_chat_folder(folder_data)

                # Resolve include_peers to chat IDs
                chat_ids = []
                include_peers = getattr(f, "include_peers", []) or []
                for peer in include_peers:
                    try:
                        chat_id = self._get_marked_id(peer)
                        chat_ids.append(chat_id)
                    except Exception:
                        # Some peers might not be resolvable
                        if hasattr(peer, "user_id"):
                            chat_ids.append(peer.user_id)
                        elif hasattr(peer, "chat_id"):
                            chat_ids.append(-peer.chat_id)
                        elif hasattr(peer, "channel_id"):
                            chat_ids.append(-1000000000000 - peer.channel_id)

                if chat_ids:
                    await self.db.sync_folder_members(folder_id, chat_ids)

                folder_count += 1
                logger.debug(f"  → Folder '{title}' (ID: {folder_id}): {len(chat_ids)} chats")

            # Remove folders that no longer exist
            await self.db.cleanup_stale_folders(active_folder_ids)

            if folder_count > 0:
                logger.info(f"Backed up {folder_count} chat folders")
            return folder_count

        except Exception as e:
            logger.warning(f"Failed to backup chat folders: {e}")
            return 0


async def run_backup(config: Config, client: TelegramClient | None = None):
    """
    Run a single backup operation.

    Args:
        config: Configuration object
        client: Optional existing TelegramClient to use (for shared connection).
               If provided, the backup will use this client instead of creating
               its own, avoiding session file lock conflicts.
    """
    backup = await TelegramBackup.create(config, client=client)
    try:
        await backup.connect()
        await backup.backup_all()
    finally:
        await backup.disconnect()
        await backup.db.close()


async def run_fill_gaps(
    config: Config, client: TelegramClient | None = None, chat_id: int | None = None
) -> dict:
    """Run gap-fill operation.

    Args:
        config: Configuration object.
        client: Optional shared TelegramClient.
        chat_id: If provided, fill gaps only for this chat.
    """
    backup = await TelegramBackup.create(config, client=client)
    try:
        await backup.connect()
        return await backup._fill_gaps(chat_id=chat_id)
    finally:
        await backup.disconnect()
        await backup.db.close()


def main():
    """Main entry point for CLI."""
    import asyncio

    from .config import Config, setup_logging

    config = Config()
    setup_logging(config)

    return asyncio.run(run_backup(config))


if __name__ == "__main__":
    # Test backup
    main()
