#!/usr/bin/env python3
"""Re-fetch messages the archive stored incompletely, and repair empty media files.

Two repairs, both opt-in and both read-only against Telegram:

``--mode blank``
    Messages stored with no text, no media row and no service marker. Most are
    media kinds the archive did not recognise before 7.15.0 (venue, dice,
    invoice, story, giveaway, live location, game, unsupported), which rendered
    as an empty bubble. Re-fetching them also fills in the formatting entities,
    forward provenance and link previews added in 7.14.0, since all of that is
    captured at write time.

``--mode media``
    Media rows the database records as downloaded whose file is empty or missing
    on disk. Interrupted downloads leave these behind; the September 2026 storage
    outage produced a batch of them.

Both modes reuse the live capture code, so a repaired row is written exactly as a
normal backup would write it. The only thing ever deleted is a ZERO-BYTE media
file (and its equally empty deduplication target), which has to go before the
download will run again — a file with real bytes in it is never touched.

SAFETY
    Dry-run by default. Pass --apply to write. Every Telegram call goes through
    the same flood-wait retry the backup uses, and --sleep throttles the calls.
    Progress is checkpointed in the metadata table, so an interrupted run resumes
    where it stopped instead of re-fetching from the beginning.

    By default the script works on a COPY of the Telegram session file so it does
    not contend with the running scheduler for the session database. Pass
    --no-session-copy to use the session in place, which is only safe while the
    backup container is stopped.

USAGE
    # See what would be repaired, no API calls beyond resolving chats
    python scripts/refetch_incomplete_messages.py --mode blank

    # Repair, conservatively, a few chats at a time
    python scripts/refetch_incomplete_messages.py --mode blank --apply --max-chats 25

    # Empty media files
    python scripts/refetch_incomplete_messages.py --mode media --apply

    # Start over rather than resuming
    python scripts/refetch_incomplete_messages.py --mode blank --apply --restart

ENVIRONMENT
    The same variables the backup container uses: TELEGRAM_API_ID,
    TELEGRAM_API_HASH, DB_PATH (or the POSTGRES_* set), BACKUP_PATH, SESSION_DIR.
"""

import argparse
import asyncio
import logging
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from telethon import TelegramClient  # noqa: E402

from src.config import Config, build_telegram_client_kwargs  # noqa: E402
from src.db import DatabaseAdapter, init_database  # noqa: E402
from src.message_utils import METADATA_ONLY_MEDIA_TYPES, normalize_media_path  # noqa: E402
from src.telegram_backup import TelegramBackup, call_with_flood_retry  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("refetch")

# Telegram accepts up to 100 ids per getMessages call.
ID_BATCH = 100
PROGRESS_KEY = "refetch_progress:{mode}"

# Stored path of each empty media file, keyed by (chat_id, message_id), filled in
# while selecting targets so the repair can clear the file before re-downloading.
EMPTY_FILE_PATHS: dict[tuple[int, int], str] = {}

# A message is "blank" only when nothing at all renders for it: no text, no media
# row, and no raw_data payload the viewer draws something from. Service markers,
# polls, link previews and the metadata-only media kinds all render, so a message
# carrying one of those is already repaired and must not be fetched again — the
# earlier version of this query kept selecting them, which would have re-fetched
# the same rows on every run forever.
_RENDERABLE_RAW_DATA_KEYS = ("service_type", "poll", "webpage", *sorted(METADATA_ONLY_MEDIA_TYPES))
BLANK_MESSAGES_SQL = """
    SELECT g.chat_id, g.id
    FROM messages g
    WHERE (g.text IS NULL OR g.text = '')
      AND NOT EXISTS (SELECT 1 FROM media m WHERE m.chat_id = g.chat_id AND m.message_id = g.id)
      AND (g.raw_data IS NULL OR ({renderable}))
    ORDER BY g.chat_id, g.id
""".format(renderable=" AND ".join(f"g.raw_data NOT LIKE '%\"{key}\"%'" for key in _RENDERABLE_RAW_DATA_KEYS))

DOWNLOADED_MEDIA_SQL = """
    SELECT chat_id, message_id, file_path, type
    FROM media
    WHERE downloaded = 1 AND file_path IS NOT NULL
    ORDER BY chat_id, message_id
"""


async def _rows(db: DatabaseAdapter, sql: str) -> list[tuple]:
    """Run a read-only query through the adapter's own engine."""
    from sqlalchemy import text

    async with db.db_manager.async_session_factory() as session:
        result = await session.execute(text(sql))
        return list(result.all())


def _clear_empty_file(stored_path: str, media_root: str) -> None:
    """Remove an empty media file so the download actually re-runs.

    Media downloads are deduplicated: the chat directory holds a symlink into a
    shared store, and ``_process_media`` short-circuits when that link already
    exists, so re-processing a message whose file is empty would re-commit the
    row and never fetch a byte. Both the link and its empty target have to go
    first. Only zero-byte files are removed, so a real file is never destroyed
    on the strength of a bad path.
    """
    relative = normalize_media_path(stored_path, media_root)
    if relative is None:
        return
    link = os.path.join(media_root, relative)

    target = None
    if os.path.islink(link):
        target = os.path.realpath(link)

    for path in (link, target):
        if not path:
            continue
        try:
            if os.path.getsize(path) == 0:
                os.remove(path)
        except OSError:
            # Missing already, or unreadable: the download path handles both.
            try:
                if os.path.islink(path):
                    os.remove(path)
            except OSError:
                pass


def _empty_on_disk(stored_path: str, media_root: str) -> bool:
    """True when a row claims a downloaded file that is missing or zero bytes."""
    relative = normalize_media_path(stored_path, media_root)
    if relative is None:
        return False  # unresolvable path: a different problem, left alone
    full = os.path.join(media_root, relative)
    try:
        return os.path.getsize(full) == 0
    except OSError:
        return True


async def _select_targets(db: DatabaseAdapter, config: Config, mode: str) -> dict[int, list[int]]:
    """Message ids needing repair, grouped by chat."""
    targets: dict[int, list[int]] = {}
    if mode == "blank":
        for chat_id, message_id in await _rows(db, BLANK_MESSAGES_SQL):
            targets.setdefault(chat_id, []).append(message_id)
        return targets

    media_root = str(config.media_path)
    for chat_id, message_id, file_path, media_type in await _rows(db, DOWNLOADED_MEDIA_SQL):
        if media_type in METADATA_ONLY_MEDIA_TYPES:
            # A location, contact or poll is a message payload, not a file. Some
            # rows carry a file_path anyway, but there is nothing to download and
            # re-fetching them would only spend API calls.
            continue
        if _empty_on_disk(file_path, media_root):
            targets.setdefault(chat_id, []).append(message_id)
            EMPTY_FILE_PATHS[(chat_id, message_id)] = file_path
    return targets


async def _repair_chat(
    backup: TelegramBackup, chat_id: int, message_ids: list[int], mode: str, sleep_seconds: float
) -> tuple[int, int]:
    """Re-fetch one chat's messages. Returns (repaired, unavailable)."""
    entity = await call_with_flood_retry(backup.client.get_entity, chat_id)

    repaired = 0
    unavailable = 0
    for start in range(0, len(message_ids), ID_BATCH):
        batch = message_ids[start : start + ID_BATCH]
        fetched = await call_with_flood_retry(backup.client.get_messages, entity, ids=batch)
        # Pair by id: Telegram omits ids it will not return rather than leaving a
        # null slot, so a positional pairing would attribute one message's content
        # to another id.
        by_id = {message.id: message for message in (fetched or []) if message is not None}

        processed: list[dict] = []
        for message_id in batch:
            message = by_id.get(message_id)
            if message is None:
                unavailable += 1
                continue
            if mode == "media":
                # Clear the empty file first, or deduplication short-circuits the
                # download and the row is re-committed with no bytes behind it.
                stored = EMPTY_FILE_PATHS.get((chat_id, message_id))
                if stored:
                    _clear_empty_file(stored, str(backup.config.media_path))
            data = await backup._process_message_isolated(message, chat_id)
            if data is None:
                unavailable += 1
                continue
            if mode == "media" and not data.get("_media_data"):
                # Nothing to repair: the message no longer carries media.
                unavailable += 1
                continue
            processed.append(data)

        if processed:
            await backup._commit_batch(processed, chat_id)
            if mode != "media":
                repaired += len(processed)
            else:
                # A committed row is not a repair: only a file with bytes behind
                # it is. Anything still empty is counted as unrecovered, so the
                # run reports what actually changed on disk.
                media_root = str(backup.config.media_path)
                for data in processed:
                    stored = (data.get("_media_data") or {}).get("file_path")
                    if stored and not _empty_on_disk(stored, media_root):
                        repaired += 1
                    else:
                        unavailable += 1

        if sleep_seconds:
            await asyncio.sleep(sleep_seconds)

    return repaired, unavailable


async def run(args: argparse.Namespace) -> int:
    config = Config()
    db_manager = await init_database()
    db = DatabaseAdapter(db_manager)

    targets = await _select_targets(db, config, args.mode)
    total_messages = sum(len(ids) for ids in targets.values())
    logger.info("Found %d message(s) to repair across %d chat(s)", total_messages, len(targets))
    if not targets:
        return 0

    progress_key = PROGRESS_KEY.format(mode=args.mode)
    # Resume by the last chat id finished, not by a position in the list: the
    # target set shrinks as rows are repaired, so an index would point somewhere
    # different on every run and silently skip chats.
    last_done: int | None = None
    if not args.restart:
        raw = await db.get_metadata(progress_key)
        if raw:
            try:
                last_done = int(raw)
            except ValueError:
                last_done = None
        if last_done is not None:
            logger.info("Resuming after chat %s (use --restart to start over)", last_done)

    chat_ids = sorted(targets)
    if last_done is not None:
        chat_ids = [cid for cid in chat_ids if cid > last_done]
    if args.max_chats:
        chat_ids = chat_ids[: args.max_chats]

    if not args.apply:
        preview = ", ".join(f"{cid} ({len(targets[cid])})" for cid in chat_ids[:5])
        logger.info("DRY RUN — would repair %d chat(s). First few: %s", len(chat_ids), preview)
        logger.info("Re-run with --apply to write.")
        return 0

    client = TelegramClient(args.session, int(config.api_id), config.api_hash, **build_telegram_client_kwargs())
    await client.connect()
    if not await client.is_user_authorized():
        logger.error("Telegram session is not authorized; run the backup container first.")
        return 1

    backup = TelegramBackup(config, db, client=client)
    repaired_total = 0
    unavailable_total = 0

    try:
        for index, chat_id in enumerate(chat_ids, start=1):
            try:
                repaired, unavailable = await _repair_chat(
                    backup, chat_id, targets[chat_id], args.mode, args.sleep
                )
            except Exception as e:
                # A chat we can no longer read (left, banned, deleted) must not
                # end the run; the remaining chats are still repairable.
                logger.warning("Chat %s could not be repaired: %s", chat_id, e)
                unavailable_total += len(targets[chat_id])
            else:
                repaired_total += repaired
                unavailable_total += unavailable
                logger.info(
                    "[%d/%d] chat %s: repaired %d, unavailable %d",
                    index, len(chat_ids), chat_id, repaired, unavailable,
                )

            await db.set_metadata(progress_key, str(chat_id))
    finally:
        await client.disconnect()

    logger.info("Done. Repaired %d message(s); %d could not be recovered.", repaired_total, unavailable_total)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("blank", "media"), required=True, help="What to repair")
    parser.add_argument("--apply", action="store_true", help="Write changes (default is a dry run)")
    parser.add_argument("--max-chats", type=int, default=0, help="Stop after this many chats (0 = no limit)")
    parser.add_argument("--sleep", type=float, default=1.5, help="Seconds to wait between Telegram calls")
    parser.add_argument("--restart", action="store_true", help="Ignore saved progress and start from the first chat")
    parser.add_argument(
        "--no-session-copy",
        dest="session_copy",
        action="store_false",
        help="Use the session file in place; only safe while the backup container is stopped",
    )
    args = parser.parse_args()

    session_dir = os.getenv("SESSION_DIR", "/data/session")
    session_name = os.getenv("SESSION_NAME", "telegram_backup")
    session_path = os.getenv("SESSION_PATH") or os.path.join(session_dir, session_name)

    temp_dir = None
    if args.session_copy and args.apply:
        # The scheduler holds the session database open. Working on a copy keeps
        # this script from contending with it for the SQLite lock; the copy is
        # only read, so nothing is lost by discarding it afterwards.
        temp_dir = tempfile.mkdtemp(prefix="refetch-session-")
        source = f"{session_path}.session"
        if not os.path.exists(source):
            logger.error("Session file not found at %s", source)
            return 1
        copy_path = os.path.join(temp_dir, f"{session_name}.session")
        shutil.copy2(source, copy_path)
        args.session = copy_path[: -len(".session")]
    else:
        args.session = session_path

    try:
        return asyncio.run(run(args))
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
