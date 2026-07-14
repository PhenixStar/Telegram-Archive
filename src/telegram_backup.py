"""
Main Telegram backup module.
Handles Telegram client connection, message fetching, and incremental backup logic.
"""

import asyncio
import logging
import os
import random
from datetime import UTC, datetime

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChatForbiddenError,
    FileReferenceExpiredError,
    FloodWaitError,
    RPCError,
    UserBannedInChannelError,
)
from telethon.tl.types import (
    Channel,
    Chat,
    InputPeerSelf,
    Message,
    User,
)

from .backup_extraction import BackupExtractionMixin
from .backup_media import BackupMediaMixin
from .config import Config
from .db import DatabaseAdapter, create_adapter
from .folder_utils import FolderChat, FolderRules, resolve_folder_member_ids
from .media_errors import is_media_location_error
from .parallel_download import ParallelDownloader

logger = logging.getLogger(__name__)


def _get_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default=%d", name, raw, default)
        return default


# Flood-wait retry budget (ported from base v7.x for shared use by listener/connection)
MAX_FLOOD_RETRIES = _get_int_env("MAX_FLOOD_RETRIES", 5)
MAX_FLOOD_WAIT_SECONDS = _get_int_env("MAX_FLOOD_WAIT_SECONDS", 3600)


def _get_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default=%s", name, raw, default)
        return default


# Backoff bounds for the transient media-refresh retry loop (issue #203).
BACKOFF_MIN_SECONDS = _get_float_env("BACKOFF_MIN_SECONDS", 2.0)
BACKOFF_MAX_SECONDS = _get_float_env("BACKOFF_MAX_SECONDS", 300.0)
# Bounded re-fetch+retry for transient media errors (expired reference / location
# unavailable). After this many download attempts the item is left for the next
# scheduled backup run instead of being retried indefinitely.
MEDIA_REFRESH_MAX_ATTEMPTS = _get_int_env("MEDIA_REFRESH_MAX_ATTEMPTS", 3)
# Upper bound on a single message-refresh round-trip so it can never hang.
MEDIA_REFRESH_TIMEOUT_SECONDS = _get_int_env("MEDIA_REFRESH_TIMEOUT_SECONDS", 120)


def _media_retry_backoff_seconds(attempt: int) -> float:
    """Bounded exponential backoff (+jitter) between media-refresh retries.

    Location errors are transient server-side conditions, so we pause before
    retrying rather than hammering ``upload.GetFile`` (which risks a FloodWait).
    """
    base = min(BACKOFF_MAX_SECONDS, BACKOFF_MIN_SECONDS * (2.0**attempt))
    return base + random.uniform(0.5, 1.5)


async def call_with_flood_retry(coro_fn, *args, max_retries=MAX_FLOOD_RETRIES, **kwargs):
    """Retry a single async Telegram call on FloodWaitError with bounded sleep.

    Use for one-shot API calls (``get_dialogs``, ``get_entity``, ``get_me``, etc.)
    that are not async iterators. Raises once the retry/wait budget is exceeded.
    """
    retries = 0
    while True:
        try:
            return await coro_fn(*args, **kwargs)
        except FloodWaitError as e:
            retries += 1
            if retries > max_retries:
                logger.error(
                    "FloodWait: exceeded %d retries on %s, giving up",
                    max_retries,
                    getattr(coro_fn, "__name__", coro_fn),
                )
                raise
            if e.seconds > MAX_FLOOD_WAIT_SECONDS:
                logger.error(
                    "FloodWait: required wait %ss exceeds MAX_FLOOD_WAIT_SECONDS=%s on %s",
                    e.seconds,
                    MAX_FLOOD_WAIT_SECONDS,
                    getattr(coro_fn, "__name__", coro_fn),
                )
                raise
            wait_seconds = max(0, e.seconds)
            logger.warning(
                "FloodWait: sleeping %ss before retrying %s (retry=%d/%d)",
                wait_seconds,
                getattr(coro_fn, "__name__", coro_fn),
                retries,
                max_retries,
            )
            await asyncio.sleep(wait_seconds + 1)  # +1s buffer to avoid boundary re-trigger


async def iter_messages_with_flood_retry(client, entity, *, min_id=0, **kwargs):
    """Wrap ``client.iter_messages`` so FloodWaitError is logged and retried.

    With ``flood_sleep_threshold=0`` on the client, every flood-wait bubbles up
    as an exception. We log the wait and resume iteration from the last yielded
    message id so progress isn't lost. Bounded by MAX_FLOOD_RETRIES consecutive
    waits-without-progress and MAX_FLOOD_WAIT_SECONDS. FLOOD_WAIT_LOG_THRESHOLD
    (default 10s) suppresses noise for short routine waits. Ascending only.
    """
    if not kwargs.get("reverse", False):
        raise ValueError("iter_messages_with_flood_retry only supports reverse=True (ascending) iteration")
    try:
        log_threshold_seconds = int(os.getenv("FLOOD_WAIT_LOG_THRESHOLD", "10"))
    except (ValueError, TypeError):
        log_threshold_seconds = 10
    resume_from = min_id
    retries = 0
    while True:
        try:
            async for msg in client.iter_messages(entity, min_id=resume_from, **kwargs):
                yield msg
                if getattr(msg, "id", None) is not None:
                    resume_from = max(resume_from, msg.id)
                retries = 0
            return
        except FloodWaitError as e:
            retries += 1
            if retries > MAX_FLOOD_RETRIES:
                logger.error(
                    "FloodWait: exceeded %d retries without progress, giving up (last_msg_id=%s)",
                    MAX_FLOOD_RETRIES,
                    resume_from,
                )
                raise
            if e.seconds > MAX_FLOOD_WAIT_SECONDS:
                logger.error(
                    "FloodWait: required wait %ss exceeds MAX_FLOOD_WAIT_SECONDS=%s; aborting (last_msg_id=%s)",
                    e.seconds,
                    MAX_FLOOD_WAIT_SECONDS,
                    resume_from,
                )
                raise
            wait_seconds = max(0, e.seconds)
            if e.seconds >= log_threshold_seconds:
                logger.warning(
                    "FloodWait: sleeping %ss before resuming (last_msg_id=%s, retry=%d/%d)",
                    wait_seconds,
                    resume_from,
                    retries,
                    MAX_FLOOD_RETRIES,
                )
            await asyncio.sleep(wait_seconds + 1)  # +1s buffer to avoid boundary re-trigger


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
        # Lazily-built parallel downloader (issue #183). Stays None until the
        # first large file when the feature is enabled; disabled for the rest of
        # the run if the client lacks the required Telethon internals.
        self._parallel_downloader: ParallelDownloader | None = None
        self._parallel_download_disabled = False

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

        # Create new client (pass shared kwargs so proxy + flood settings apply,
        # matching listener/setup_auth/restore client creation).
        logger.info(f"Using Telethon session database: {self.config.session_path}.session")
        self.client = TelegramClient(
            self.config.session_path,
            self.config.api_id,
            self.config.api_hash,
            **self.config.get_telegram_client_kwargs(),
        )
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

        me = await call_with_flood_retry(self.client.get_me)
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
            me = await call_with_flood_retry(self.client.get_me)
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

                # Skip channels where user is NOT admin/creator (broadcast channels only)
                # Groups (megagroups) are always backed up regardless of admin status
                if is_channel and not getattr(entity, "creator", False) and not getattr(entity, "admin_rights", None):
                    continue

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
                        entity = await call_with_flood_retry(self.client.get_entity, include_id)

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

                # Skip channels where user is NOT admin/creator
                if is_channel and not getattr(entity, "creator", False) and not getattr(entity, "admin_rights", None):
                    continue

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
            dialogs = await call_with_flood_retry(self.client.get_dialogs, folder=1)
        else:
            dialogs = await call_with_flood_retry(self.client.get_dialogs, folder=0)
        return dialogs

    async def _refresh_message_for_media(self, chat_id: int, message: Message) -> Message | None:
        """Best-effort re-fetch so Telegram issues an updated media reference/location.

        Bounded by ``MEDIA_REFRESH_TIMEOUT_SECONDS`` so it can never hang, and
        swallows transient errors (returning ``None``) so a failed refresh never
        blows up the surrounding retry loop. Handles a deleted/unavailable
        message (``[]`` or ``[None]``) by returning ``None``.
        """

        async def _get_messages_once():
            # Time only the single Telegram call, so call_with_flood_retry still
            # owns (and is never cancelled mid-) any FloodWait sleep.
            return await asyncio.wait_for(
                self.client.get_messages(chat_id, ids=[message.id]),
                timeout=MEDIA_REFRESH_TIMEOUT_SECONDS,
            )

        try:
            fresh_messages = await call_with_flood_retry(_get_messages_once)
        except (TimeoutError, RPCError, ConnectionError, OSError) as e:
            logger.debug("Could not refresh media reference (%s)", type(e).__name__)
            return None
        if fresh_messages and fresh_messages[0]:
            return fresh_messages[0]
        return None

    async def _fetch_media_bytes_bounded(self, message: Message, tmp_path: str, file_size: int, timeout_val):
        """``_fetch_media_bytes`` bounded by a per-operation timeout.

        Timing only the single download operation (rather than the whole
        ``call_with_flood_retry`` wrapper) ensures a Telegram FloodWait sleep is
        never cancelled by the download timeout. A timed-out operation raises
        ``TimeoutError``, which the outer loop in ``_download_media_to_path``
        handles.
        """
        coro = self._fetch_media_bytes(message, tmp_path, file_size)
        if timeout_val is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout_val)

    async def _download_media_to_path(self, message: Message, tmp_path: str, file_size: int, chat_id: int):
        """Download a message's media to ``tmp_path`` with bounded refresh + retry.

        Transient Telegram errors that a fresh message can fix — an expired file
        reference, or an unavailable/invalid media *location* — trigger a
        re-fetch of the message (for a new reference/location). A location error
        is a transient server-side condition, so we also pause with exponential
        backoff before retrying; an expired reference is fixed by the refresh
        itself and is retried immediately. After ``MEDIA_REFRESH_MAX_ATTEMPTS``
        the last real error is raised so the caller records the item as
        not-downloaded; the next scheduled backup run re-attempts it.

        Non-FloodWait errors propagate straight out of ``call_with_flood_retry``
        (which only retries FloodWait), so this loop sees them directly.

        Returns the downloaded path on success.
        """
        timeout = getattr(self.config, "download_timeout_seconds", 3600)
        timeout_val = timeout if isinstance(timeout, int) and timeout > 0 else None
        last = MEDIA_REFRESH_MAX_ATTEMPTS - 1
        try:
            for attempt in range(MEDIA_REFRESH_MAX_ATTEMPTS):
                # Start each attempt clean so a prior partial never corrupts it.
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                try:
                    return await call_with_flood_retry(
                        self._fetch_media_bytes_bounded,
                        message,
                        tmp_path,
                        file_size,
                        timeout_val,
                    )
                except (FileReferenceExpiredError, RPCError) as e:
                    is_expired_ref = isinstance(e, FileReferenceExpiredError)
                    if not is_expired_ref and not is_media_location_error(e):
                        raise  # not refreshable — let the outer handler record it
                    if attempt >= last:
                        logger.warning(
                            "Media still unavailable after %d attempt(s) (%s); leaving it for a future backup run",
                            attempt + 1,
                            type(e).__name__,
                        )
                        raise
                    refreshed = await self._refresh_message_for_media(chat_id, message)
                    if refreshed is not None:
                        message = refreshed
                        logger.info(
                            "Refreshed media reference after a transient error (attempt %d/%d); retrying",
                            attempt + 1,
                            MEDIA_REFRESH_MAX_ATTEMPTS,
                        )
                    else:
                        logger.info(
                            "Could not refresh media reference (attempt %d/%d); retrying anyway",
                            attempt + 1,
                            MEDIA_REFRESH_MAX_ATTEMPTS,
                        )
                    if not is_expired_ref:
                        await asyncio.sleep(_media_retry_backoff_seconds(attempt))
                except TimeoutError:
                    if attempt >= last:
                        logger.error(
                            "Media download timed out after %ss on attempt %d/%d; giving up for this run",
                            timeout,
                            attempt + 1,
                            MEDIA_REFRESH_MAX_ATTEMPTS,
                        )
                        raise
                    logger.warning(
                        "Media download timed out after %ss (attempt %d/%d); retrying",
                        timeout,
                        attempt + 1,
                        MEDIA_REFRESH_MAX_ATTEMPTS,
                    )
            # Defensive: the loop returns on success or raises on the final attempt.
            raise FileReferenceExpiredError(request=None)
        except BaseException:
            # Never leave a partial behind on failure or cancellation.
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

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
        skipped_symlinks = 0

        # Phase 1: Check which files need re-downloading
        for record in media_records:
            file_path = record.get("file_path")
            if not file_path:
                continue

            # Detect "truly missing" via lexists so an existing symlink
            # whose ultimate target is unreachable (e.g. git-annex object
            # outside the bind mount) is not flagged for re-download.
            # Re-downloading it would atomic-rename a regular file on top
            # of the symlink, mutating an archived working tree (issue #143).
            if not os.path.lexists(file_path):
                missing_files.append(record)
                continue

            # Trust symlinks: their content is managed externally and may
            # be unreachable from this process. We cannot meaningfully
            # check size or emptiness without following the link.
            if os.path.islink(file_path):
                skipped_symlinks += 1
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
            msg = "✓ All media files verified - no issues found"
            if skipped_symlinks:
                msg += f" ({skipped_symlinks} symlink entries skipped)"
            logger.info(msg)
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
                    messages = await call_with_flood_retry(self.client.get_messages, chat_id, ids=message_ids)
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
        checkpoint_interval = self.config.checkpoint_interval
        grand_total = 0
        uncheckpointed_count = 0
        batches_since_checkpoint = 0
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
                    count = len(batch_data)
                    grand_total += count
                    uncheckpointed_count += count
                    batches_since_checkpoint += 1
                    logger.info(f"  → Processed {grand_total} messages...")
                    # Checkpoint sync_status every checkpoint_interval batches so a
                    # crash only re-fetches since the last checkpoint, not the whole chat.
                    if batches_since_checkpoint >= checkpoint_interval:
                        await self.db.update_sync_status(chat_id, running_max_id, uncheckpointed_count)
                        uncheckpointed_count = 0
                        batches_since_checkpoint = 0
                    batch_data = []
        else:
            # Initial backup: forward order (old→new) for completeness
            async for message in iter_messages_with_flood_retry(self.client, entity, min_id=0, reverse=True):
                msg_data = await self._process_message(message, chat_id)
                batch_data.append(msg_data)
                running_max_id = max(running_max_id, message.id)

                if len(batch_data) >= batch_size:
                    await self._commit_batch(batch_data, chat_id)
                    count = len(batch_data)
                    grand_total += count
                    uncheckpointed_count += count
                    batches_since_checkpoint += 1
                    logger.info(f"  → Processed {grand_total} messages...")
                    # Checkpoint sync_status every checkpoint_interval batches so a
                    # crash only re-fetches since the last checkpoint, not the whole chat.
                    if batches_since_checkpoint >= checkpoint_interval:
                        await self.db.update_sync_status(chat_id, running_max_id, uncheckpointed_count)
                        uncheckpointed_count = 0
                        batches_since_checkpoint = 0
                    batch_data = []

        # Flush remaining messages
        if batch_data:
            await self._commit_batch(batch_data, chat_id)
            count = len(batch_data)
            grand_total += count
            uncheckpointed_count += count

        # Final checkpoint: persist remaining un-checkpointed messages, or a
        # cursor that advanced without persisting new messages.
        if uncheckpointed_count > 0 or (grand_total == 0 and running_max_id > last_message_id):
            await self.db.update_sync_status(chat_id, running_max_id, uncheckpointed_count)

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

        async for message in iter_messages_with_flood_retry(
            self.client, entity, min_id=gap_start, max_id=gap_end, reverse=True
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
                entity = await call_with_flood_retry(self.client.get_entity, cid)
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
                remote_messages = await call_with_flood_retry(self.client.get_messages, entity, ids=batch_ids)

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
            pinned_messages = await call_with_flood_retry(self.client.get_messages, entity, filter=InputMessagesFilterPinned(), limit=100)

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
                    msgs = await call_with_flood_retry(self.client.get_messages, entity, ids=[topic_id])
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

    def _resolve_peer_ids(self, peers, own_id: int | None = None) -> set[int]:
        """Resolve a DialogFilter peer list (InputPeer objects) to marked chat ids.

        ``own_id`` maps ``InputPeerSelf`` (how a pinned Saved Messages chat is
        stored) to the account's own user id, which get_peer_id cannot resolve.
        """
        ids: set[int] = set()
        for peer in peers or []:
            if own_id is not None and isinstance(peer, InputPeerSelf):
                ids.add(own_id)
                continue
            try:
                ids.add(self._get_marked_id(peer))
            except Exception:
                # Some peers might not be resolvable via get_peer_id; fall back to
                # the raw id fields with the standard marked-id conventions.
                if hasattr(peer, "user_id"):
                    ids.add(peer.user_id)
                elif hasattr(peer, "chat_id"):
                    ids.add(-peer.chat_id)
                elif hasattr(peer, "channel_id"):
                    ids.add(-1000000000000 - peer.channel_id)
        return ids

    def _folder_rules_from_filter(self, f, own_id: int | None = None) -> FolderRules:
        """Build resolver rules from a DialogFilter / DialogFilterChatlist.

        Chatlist (shareable) folders carry no flags or exclude_peers; getattr
        defaults keep them as a pure pinned+include allowlist.
        """
        return FolderRules(
            pinned_ids=frozenset(self._resolve_peer_ids(getattr(f, "pinned_peers", []), own_id)),
            include_ids=frozenset(self._resolve_peer_ids(getattr(f, "include_peers", []), own_id)),
            exclude_ids=frozenset(self._resolve_peer_ids(getattr(f, "exclude_peers", []), own_id)),
            contacts=bool(getattr(f, "contacts", False)),
            non_contacts=bool(getattr(f, "non_contacts", False)),
            groups=bool(getattr(f, "groups", False)),
            broadcasts=bool(getattr(f, "broadcasts", False)),
            bots=bool(getattr(f, "bots", False)),
            exclude_muted=bool(getattr(f, "exclude_muted", False)),
            exclude_read=bool(getattr(f, "exclude_read", False)),
            exclude_archived=bool(getattr(f, "exclude_archived", False)),
        )

    async def _get_contact_ids(self) -> set[int]:
        """Fetch the account's contact user ids (for contacts/non_contacts flags).

        Returns an empty set on failure — folders relying on those flags simply
        fall back to their explicit peers rather than aborting the backup.
        """
        try:
            from telethon.tl.functions.contacts import GetContactsRequest

            result = await call_with_flood_retry(self.client, GetContactsRequest(hash=0))
            return {u.id for u in getattr(result, "users", [])}
        except Exception as e:
            logger.warning(f"Could not fetch contacts for folder resolution: {e}")
            return set()

    async def _get_own_id(self) -> int | None:
        """Return the account's own user id (for resolving self/Saved Messages)."""
        try:
            me = await call_with_flood_retry(self.client.get_me)
            return me.id if me is not None else None
        except Exception as e:
            logger.warning(f"Could not resolve own id for folder resolution: {e}")
            return None

    async def _backup_folders(self) -> int:
        """
        Fetch and store user's Telegram chat folders (dialog filters).

        Resolves each folder's FULL effective membership against the chats we've
        archived — explicit pinned/include peers minus exclude peers, plus the
        category flags (contacts/non_contacts/groups/broadcasts/bots), not only
        include_peers — so folders defined by pins or flags aren't left empty.

        Returns:
            Number of folders backed up
        """
        try:
            from telethon.tl.functions.messages import GetDialogFiltersRequest

            result = await self.client(GetDialogFiltersRequest())

            # result might be a list directly or have a .filters attribute
            filters = result.filters if hasattr(result, "filters") else result

            # The archived-chat snapshot and contacts are fetched at most once per
            # run, lazily, and reused across folders — an account with only the
            # default "All" filter pays for neither.
            resolution_chats: list[FolderChat] | None = None
            contact_ids: set[int] | None = None
            own_id = await self._get_own_id()

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

                if resolution_chats is None:
                    resolution_chats = [
                        FolderChat(id=r["id"], type=r["type"], is_bot=r["is_bot"], is_archived=r["is_archived"])
                        for r in await self.db.get_chats_for_folder_resolution()
                    ]

                rules = self._folder_rules_from_filter(f, own_id)
                if (rules.contacts or rules.non_contacts) and contact_ids is None:
                    contact_ids = await self._get_contact_ids()
                    # Saved Messages (self) counts as a contact, matching Telegram.
                    if own_id is not None:
                        contact_ids.add(own_id)

                member_ids = resolve_folder_member_ids(rules, resolution_chats, contact_ids or set())
                # Always sync (even to an empty set) so a folder that lost all its
                # archived chats is emptied rather than keeping stale members.
                await self.db.sync_folder_members(folder_id, list(member_ids))

                folder_count += 1
                logger.debug(f"  → Folder '{title}' (ID: {folder_id}): {len(member_ids)} chats")

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
