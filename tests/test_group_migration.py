"""Tests for group→supergroup migration handling (issue #228).

Covers:
- ``_process_message`` persisting the migration pointer id in raw_data
  (backup_extraction.py).
- The always-on, count-only warning fired by ``TelegramBackup._reconcile_migrations``.
- The opt-in ``FOLLOW_CHAT_MIGRATIONS`` adopt-and-capture behaviour.
- ``get_migration_markers`` (offline-detection adapter read).
- The listener's ``_followed_live`` scope injection in both whitelist and
  type-based modes.
- The sweep's in-dialog injection point (``_is_followed_migration`` in the
  main filter loop of ``backup_all``).

Note: the sweep's *missing_include_ids* explicit-fetch fallback (for a
followed id that no longer appears in the dialog list at all) reuses the
pre-existing ``SimpleDialog`` wrapper, which predates this feature and lacks
a ``.message`` attribute the smart-skip check unconditionally reads. That is
a latent bug in the already-shipped GROUPS_INCLUDE_CHAT_IDS "missing dialog"
fallback, not something introduced here, so it is deliberately left alone and
not exercised by the sweep-injection test below (which keeps the followed
chat in the returned dialog list, the overwhelmingly common real-world case
for an active supergroup).
"""

import asyncio
import json
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from telethon.tl.types import (
    MessageActionChannelMigrateFrom,
    MessageActionChatEditTitle,
    MessageActionChatMigrateTo,
    PeerChannel,
    PeerChat,
)
from telethon.utils import get_peer_id

from src.config import Config
from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Chat, Message
from src.listener import TelegramListener
from src.telegram_backup import TelegramBackup


def _run(coro):
    """Run a coroutine in a fresh event loop (unittest.TestCase style)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ===========================================================================
# _process_message persists the migration pointer id (backup_extraction.py)
# ===========================================================================


def _make_service_message(action, text=""):
    """Minimal mock service message carrying ``action`` (reply_to explicitly None)."""
    msg = MagicMock()
    msg.id = 4242
    msg.sender = None
    msg.sender_id = 42
    msg.date = datetime(2024, 1, 15, 12, 0, 0)
    msg.text = text
    msg.reply_to_msg_id = None
    msg.reply_to = None  # avoid MagicMock truthiness triggering topic filtering
    msg.edit_date = None
    msg.out = False
    msg.pinned = False
    msg.grouped_id = None
    msg.fwd_from = None
    msg.media = None
    msg.reactions = None
    msg.post_author = None
    msg.action = action
    return msg


def _make_process_backup():
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.config = MagicMock()
    backup.db = AsyncMock()
    backup.client = AsyncMock()
    return backup


class TestProcessMessageMigrationPointer(unittest.TestCase):
    """The new supergroup / old group pointer id is persisted in raw_data."""

    def test_migrate_to_persists_marked_channel_pointer(self):
        """MessageActionChatMigrateTo(channel_id=N) -> raw_data.migrate_to_id == marked channel id."""
        backup = _make_process_backup()
        action = MessageActionChatMigrateTo(channel_id=555)
        result = _run(backup._process_message(_make_service_message(action), -100200300))
        self.assertEqual(result["raw_data"]["migrate_to_id"], get_peer_id(PeerChannel(555)))
        self.assertEqual(result["raw_data"]["action_type"], "chat_migrate_to")
        self.assertNotIn("migrate_from_id", result["raw_data"])

    def test_migrate_from_persists_marked_chat_pointer(self):
        """MessageActionChannelMigrateFrom(chat_id=M) -> raw_data.migrate_from_id == marked chat id."""
        backup = _make_process_backup()
        action = MessageActionChannelMigrateFrom(title="Old Group", chat_id=777)
        result = _run(backup._process_message(_make_service_message(action), -1000000000999))
        self.assertEqual(result["raw_data"]["migrate_from_id"], get_peer_id(PeerChat(777)))
        self.assertEqual(result["raw_data"]["action_type"], "channel_migrate_from")
        self.assertNotIn("migrate_to_id", result["raw_data"])

    def test_normal_service_action_has_no_migration_pointer(self):
        """A non-migration service message (title change) carries neither pointer."""
        backup = _make_process_backup()
        action = MessageActionChatEditTitle(title="New Title")
        result = _run(backup._process_message(_make_service_message(action), -100200300))
        self.assertNotIn("migrate_to_id", result["raw_data"])
        self.assertNotIn("migrate_from_id", result["raw_data"])
        self.assertEqual(result["raw_data"]["action_type"], "chat_edit_title")


# ===========================================================================
# _reconcile_migrations — warn (default) / follow (opt-in)
# ===========================================================================

_LOGGER = "src.telegram_backup"


def _make_reconcile_backup(follow=False):
    backup = TelegramBackup.__new__(TelegramBackup)
    cfg = MagicMock()
    cfg.follow_chat_migrations = follow
    cfg.chat_ids = set()
    cfg.global_include_ids = set()
    cfg.groups_include_ids = set()
    cfg.channels_include_ids = set()
    cfg.global_exclude_ids = set()
    cfg.groups_exclude_ids = set()
    cfg.channels_exclude_ids = set()
    # Default: the new supergroup is NOT in type-based scope (out of scope)
    # unless a test opts it in. Real Config.should_backup_chat is exercised
    # separately in TestFollowChatMigrationsConfig / the base config tests.
    cfg.should_backup_chat = MagicMock(return_value=False)
    backup.config = cfg
    backup.db = AsyncMock()
    backup.db.get_migration_markers = AsyncMock(return_value=[])
    backup.client = AsyncMock()
    backup._followed_migration_ids = set()
    backup._backup_dialog = AsyncMock(return_value=5)
    return backup


def _migrated_dialog(channel_id):
    """A dialog whose entity is a migrated basic group (carries .migrated_to)."""
    entity = SimpleNamespace(migrated_to=SimpleNamespace(channel_id=channel_id))
    return SimpleNamespace(entity=entity)


class TestReconcileMigrationsWarn(unittest.TestCase):
    """Always-on, count-only warning; no ids leaked; deduped per run."""

    def test_warns_for_primary_migrated_entity(self):
        backup = _make_reconcile_backup(follow=False)
        backup._get_marked_id = MagicMock(return_value=-100200300)
        new_id = get_peer_id(PeerChannel(555))
        with self.assertLogs(_LOGGER, level="WARNING") as cm:
            _run(backup._reconcile_migrations([_migrated_dialog(555)], set()))
        joined = "\n".join(cm.output)
        self.assertIn("migrated to a supergroup not in scope", joined)
        self.assertIn("1 tracked group", joined)
        # PII: counts only — neither the new nor the old marked id appears.
        self.assertNotIn(str(new_id), joined)
        self.assertNotIn("100200300", joined)
        backup.db.set_metadata.assert_not_awaited()

    def test_warns_from_secondary_stored_marker(self):
        """Offline migration: detected purely from a stored chat_migrate_to marker."""
        backup = _make_reconcile_backup(follow=False)
        new_id = get_peer_id(PeerChannel(999))
        backup.db.get_migration_markers = AsyncMock(return_value=[(-4242, new_id)])
        with self.assertLogs(_LOGGER, level="WARNING") as cm:
            _run(backup._reconcile_migrations([], set()))
        joined = "\n".join(cm.output)
        self.assertIn("1 tracked group", joined)
        self.assertNotIn(str(new_id), joined)

    def test_suppressed_when_new_id_already_captured(self):
        backup = _make_reconcile_backup(follow=False)
        backup._get_marked_id = MagicMock(return_value=-100)
        new_id = get_peer_id(PeerChannel(555))
        with self.assertNoLogs(_LOGGER, level="WARNING"):
            _run(backup._reconcile_migrations([_migrated_dialog(555)], {new_id}))

    def test_suppressed_when_new_id_in_configured_include(self):
        backup = _make_reconcile_backup(follow=False)
        backup._get_marked_id = MagicMock(return_value=-100)
        new_id = get_peer_id(PeerChannel(555))
        backup.config.groups_include_ids = {new_id}
        with self.assertNoLogs(_LOGGER, level="WARNING"):
            _run(backup._reconcile_migrations([_migrated_dialog(555)], set()))

    def test_suppressed_when_new_id_in_type_based_scope(self):
        """All-groups mode: the migrated supergroup is type-in-scope, so no nag."""
        backup = _make_reconcile_backup(follow=False)
        backup._get_marked_id = MagicMock(return_value=-100)
        backup.config.should_backup_chat = MagicMock(return_value=True)
        with self.assertNoLogs(_LOGGER, level="WARNING"):
            _run(backup._reconcile_migrations([_migrated_dialog(555)], set()))
        # Queried as a megagroup (is_group=True), not a broadcast channel.
        _, kwargs = backup.config.should_backup_chat.call_args
        self.assertTrue(kwargs.get("is_group"))
        self.assertFalse(kwargs.get("is_channel"))

    def test_suppressed_when_new_id_explicitly_excluded(self):
        backup = _make_reconcile_backup(follow=False)
        backup._get_marked_id = MagicMock(return_value=-100)
        new_id = get_peer_id(PeerChannel(555))
        backup.config.groups_exclude_ids = {new_id}
        with self.assertNoLogs(_LOGGER, level="WARNING"):
            _run(backup._reconcile_migrations([_migrated_dialog(555)], set()))

    def test_dedups_to_single_warning_per_run(self):
        backup = _make_reconcile_backup(follow=False)
        backup._get_marked_id = MagicMock(side_effect=[-101, -102])
        with self.assertLogs(_LOGGER, level="WARNING") as cm:
            _run(backup._reconcile_migrations([_migrated_dialog(555), _migrated_dialog(666)], set()))
        warn_lines = [line for line in cm.output if "migrated to a supergroup" in line]
        self.assertEqual(len(warn_lines), 1)
        self.assertIn("2 tracked group", warn_lines[0])

    def test_off_persists_nothing(self):
        backup = _make_reconcile_backup(follow=False)
        backup._get_marked_id = MagicMock(return_value=-100)
        _run(backup._reconcile_migrations([_migrated_dialog(555)], set()))
        backup.db.set_metadata.assert_not_awaited()
        backup._backup_dialog.assert_not_awaited()


class TestReconcileMigrationsFollow(unittest.TestCase):
    """Opt-in follow persists + captures this run; guards inaccessibility."""

    def test_follow_persists_and_captures_without_warning(self):
        backup = _make_reconcile_backup(follow=True)
        backup._get_marked_id = MagicMock(return_value=-100)
        backup.client.get_entity = AsyncMock(return_value=MagicMock())
        new_id = get_peer_id(PeerChannel(555))
        backed = set()
        with self.assertNoLogs(_LOGGER, level="WARNING"):
            _run(backup._reconcile_migrations([_migrated_dialog(555)], backed))
        backup.db.set_metadata.assert_awaited_once()
        key, value = backup.db.set_metadata.await_args.args
        self.assertEqual(key, "followed_migrations")
        self.assertIn(new_id, json.loads(value))
        backup._backup_dialog.assert_awaited_once()
        self.assertIn(new_id, backup._followed_migration_ids)
        self.assertIn(new_id, backed)

    def test_follow_already_followed_is_noop(self):
        backup = _make_reconcile_backup(follow=True)
        backup._get_marked_id = MagicMock(return_value=-100)
        new_id = get_peer_id(PeerChannel(555))
        backup._followed_migration_ids = {new_id}
        with self.assertNoLogs(_LOGGER, level="WARNING"):
            _run(backup._reconcile_migrations([_migrated_dialog(555)], set()))
        backup.db.set_metadata.assert_not_awaited()
        backup._backup_dialog.assert_not_awaited()

    def test_follow_inaccessible_channel_does_not_raise(self):
        backup = _make_reconcile_backup(follow=True)
        backup._get_marked_id = MagicMock(return_value=-100)
        backup.client.get_entity = AsyncMock(side_effect=Exception("no access"))
        new_id = get_peer_id(PeerChannel(555))
        backed = set()
        _run(backup._reconcile_migrations([_migrated_dialog(555)], backed))  # must not raise
        backup.db.set_metadata.assert_awaited_once()  # persisted before capture attempt
        self.assertIn(new_id, backup._followed_migration_ids)
        backup._backup_dialog.assert_not_awaited()
        self.assertNotIn(new_id, backed)


# ===========================================================================
# FOLLOW_CHAT_MIGRATIONS config flag + followed-set loading (backup side)
# ===========================================================================


class TestFollowChatMigrationsConfig(unittest.TestCase):
    def test_default_off(self):
        with patch("os.makedirs"), patch.dict(os.environ, {"CHAT_TYPES": "private"}, clear=True):
            self.assertFalse(Config().follow_chat_migrations)

    def test_enabled_true(self):
        with (
            patch("os.makedirs"),
            patch.dict(os.environ, {"CHAT_TYPES": "private", "FOLLOW_CHAT_MIGRATIONS": "true"}, clear=True),
        ):
            self.assertTrue(Config().follow_chat_migrations)

    def test_enabled_accepts_common_truthy_variants(self):
        for variant in ("1", "yes", "on", "TRUE"):
            with (
                patch("os.makedirs"),
                patch.dict(os.environ, {"CHAT_TYPES": "private", "FOLLOW_CHAT_MIGRATIONS": variant}, clear=True),
            ):
                self.assertTrue(Config().follow_chat_migrations, variant)


class TestFollowedMigrationScope(unittest.TestCase):
    """_load_followed_migrations + _is_followed_migration (sweep scope predicate)."""

    def test_load_short_circuits_when_off(self):
        backup = _make_reconcile_backup(follow=False)
        backup.db.get_metadata = AsyncMock(return_value=json.dumps([-100, -200]))
        _run(backup._load_followed_migrations())
        self.assertEqual(backup._followed_migration_ids, set())
        backup.db.get_metadata.assert_not_awaited()

    def test_load_reads_metadata_when_on(self):
        backup = _make_reconcile_backup(follow=True)
        followed = get_peer_id(PeerChannel(555))
        backup.db.get_metadata = AsyncMock(return_value=json.dumps([followed]))
        _run(backup._load_followed_migrations())
        self.assertIn(followed, backup._followed_migration_ids)
        self.assertTrue(backup._is_followed_migration(followed))

    def test_load_malformed_degrades_to_empty(self):
        backup = _make_reconcile_backup(follow=True)
        backup.db.get_metadata = AsyncMock(return_value="not json{")
        _run(backup._load_followed_migrations())
        self.assertEqual(backup._followed_migration_ids, set())

    def test_load_missing_value_degrades_to_empty(self):
        backup = _make_reconcile_backup(follow=True)
        backup.db.get_metadata = AsyncMock(return_value=None)
        _run(backup._load_followed_migrations())
        self.assertEqual(backup._followed_migration_ids, set())

    def test_is_followed_false_when_flag_off(self):
        backup = _make_reconcile_backup(follow=False)
        backup._followed_migration_ids = {-100}
        self.assertFalse(backup._is_followed_migration(-100))

    def test_is_followed_true_when_on_and_present(self):
        backup = _make_reconcile_backup(follow=True)
        backup._followed_migration_ids = {-100}
        self.assertTrue(backup._is_followed_migration(-100))


# ===========================================================================
# Listener: followed supergroups tracked live in BOTH whitelist and type modes
# ===========================================================================


def _make_listener_config(*, follow, whitelist_mode=False, chat_ids=None):
    """Config for the listener with follow_chat_migrations as a REAL bool.

    (A MagicMock default would make follow_chat_migrations truthy and hide the
    off-branch — the exact gap this covers.)
    """
    cfg = MagicMock()
    cfg.api_id = 12345
    cfg.api_hash = "test_hash"
    cfg.phone = "+1234567890"
    cfg.session_path = "/tmp/test_session"
    cfg.validate_credentials = MagicMock()
    cfg.global_include_ids = set()
    cfg.private_include_ids = set()
    cfg.groups_include_ids = set()
    cfg.channels_include_ids = set()
    cfg.whitelist_mode = whitelist_mode
    cfg.chat_ids = chat_ids or set()
    cfg.follow_chat_migrations = follow  # real bool, not MagicMock
    cfg.listen_edits = True
    cfg.listen_deletions = False
    cfg.listen_new_messages = True
    cfg.listen_new_messages_media = False
    cfg.skip_topic_ids = {}
    cfg.mass_operation_threshold = 10
    cfg.mass_operation_window_seconds = 30
    cfg.mass_operation_buffer_delay = 2.0
    return cfg


class TestListenerFollowScope(unittest.TestCase):
    """A followed supergroup must be live-processed in whitelist AND type modes."""

    def test_type_mode_tracks_and_processes_followed(self):
        followed = get_peer_id(PeerChannel(555))
        cfg = _make_listener_config(follow=True, whitelist_mode=False)
        db = AsyncMock()
        db.get_all_chats = AsyncMock(return_value=[{"id": -111}])
        db.get_metadata = AsyncMock(return_value=json.dumps([followed]))
        listener = TelegramListener(cfg, db)
        _run(listener._load_tracked_chats())
        self.assertIn(followed, listener._tracked_chat_ids)
        self.assertIn(followed, listener._followed_live)
        self.assertTrue(listener._should_process_chat(followed))

    def test_whitelist_mode_processes_followed(self):
        """Whitelist mode ignores _tracked_chat_ids, so follow must ride _followed_live."""
        followed = get_peer_id(PeerChannel(555))
        cfg = _make_listener_config(follow=True, whitelist_mode=True, chat_ids={-999})
        db = AsyncMock()
        db.get_all_chats = AsyncMock(return_value=[])
        db.get_metadata = AsyncMock(return_value=json.dumps([followed]))
        listener = TelegramListener(cfg, db)
        _run(listener._load_tracked_chats())
        self.assertIn(followed, listener._followed_live)
        self.assertTrue(listener._should_process_chat(followed))  # via follow
        self.assertTrue(listener._should_process_chat(-999))  # explicit whitelist
        self.assertFalse(listener._should_process_chat(-12345))  # neither

    def test_load_followed_off_short_circuits(self):
        cfg = _make_listener_config(follow=False)
        db = AsyncMock()
        db.get_metadata = AsyncMock(return_value=json.dumps([-1]))
        listener = TelegramListener(cfg, db)
        self.assertEqual(_run(listener._load_followed_migration_ids()), set())
        db.get_metadata.assert_not_awaited()

    def test_load_followed_on_reads_metadata(self):
        followed = get_peer_id(PeerChannel(555))
        cfg = _make_listener_config(follow=True)
        db = AsyncMock()
        db.get_metadata = AsyncMock(return_value=json.dumps([followed]))
        listener = TelegramListener(cfg, db)
        self.assertEqual(_run(listener._load_followed_migration_ids()), {followed})

    def test_load_followed_malformed_degrades_to_empty(self):
        cfg = _make_listener_config(follow=True)
        db = AsyncMock()
        db.get_metadata = AsyncMock(return_value="not json{")
        listener = TelegramListener(cfg, db)
        self.assertEqual(_run(listener._load_followed_migration_ids()), set())

    def test_load_followed_missing_degrades_to_empty(self):
        cfg = _make_listener_config(follow=True)
        db = AsyncMock()
        db.get_metadata = AsyncMock(return_value=None)
        listener = TelegramListener(cfg, db)
        self.assertEqual(_run(listener._load_followed_migration_ids()), set())


# ===========================================================================
# Sweep in-dialog injection: a followed id present in the dialog list lands
# in filtered_dialogs via the elif _is_followed_migration(...) branch, in
# BOTH type-based and whitelist mode (a single shared filter loop in this
# fork — no separate whitelist-mode dialog-fetch path to duplicate).
# ===========================================================================


class _Ent:
    """Marked-id-carrying stand-in entity (not a User/Chat/Channel instance, so
    the type-based filter classifies it as none-of-the-above)."""

    def __init__(self, mid):
        self.id = mid
        self.mid = mid


def _make_sweep_backup(*, followed, whitelist_mode, chat_ids, main_dialogs, exclude_ids=None):
    backup = TelegramBackup.__new__(TelegramBackup)
    cfg = MagicMock()
    cfg.whitelist_mode = whitelist_mode
    cfg.chat_ids = chat_ids
    cfg.phone = "+1234567890"
    cfg.priority_chat_ids = set()
    cfg.early_stop_threshold = 0
    cfg.global_include_ids = set()
    cfg.private_include_ids = set()
    cfg.groups_include_ids = set()
    cfg.channels_include_ids = set()
    # global_exclude_ids applies unconditionally (no chat-type check), which
    # keeps this stand-in entity (neither User/Chat/Channel) usable here — the
    # exclude-gate ordering under test is identical regardless of which
    # exclude list matches.
    cfg.global_exclude_ids = exclude_ids or set()
    cfg.groups_exclude_ids = set()
    cfg.channels_exclude_ids = set()
    cfg.private_exclude_ids = set()
    cfg.follow_chat_migrations = True  # real bool so _is_followed_migration works
    cfg.verify_media = False
    # Nothing is in scope by type/include — only the follow path can pull ids in
    # (whitelist_mode is honored internally by should_backup_chat via chat_ids).
    cfg.should_backup_chat = MagicMock(side_effect=lambda cid, *a, **k: whitelist_mode and cid in chat_ids)
    backup.config = cfg

    db = AsyncMock()
    db.get_all_last_message_ids = AsyncMock(return_value={})
    db.calculate_and_store_statistics = AsyncMock(
        return_value={"chats": 1, "messages": 1, "media_files": 0, "total_size_mb": 0}
    )
    backup.db = db

    client = AsyncMock()
    me = MagicMock()
    me.id = 1
    client.get_me = AsyncMock(return_value=me)
    client.start = AsyncMock()
    backup.client = client

    backup._followed_migration_ids = set(followed)  # PRE-POPULATED
    backup._get_marked_id = MagicMock(side_effect=lambda e: e.mid)
    backup._get_chat_name = MagicMock(side_effect=lambda e: f"chat-{e.mid}")
    backup._get_dialogs = AsyncMock(side_effect=lambda archived=False: [] if archived else main_dialogs)
    # Isolate the dialog-filtering injection points under test: stub the
    # surrounding orchestration this fork's backup_all also performs.
    backup._load_followed_migrations = AsyncMock()  # no-op: keep the pre-populated set
    backup._reconcile_migrations = AsyncMock()
    backup._backup_folders = AsyncMock()
    backup._backup_dialog = AsyncMock(return_value=0)
    return backup


def _backed_up_ids(backup):
    return {call.args[0].entity.mid for call in backup._backup_dialog.call_args_list}


class TestFollowedSweepInjection(unittest.TestCase):
    """A followed id already present in the dialog list reaches _backup_dialog."""

    def test_type_mode_elif_injection(self):
        f_in = get_peer_id(PeerChannel(555))
        dialog_in = MagicMock()
        dialog_in.entity = _Ent(f_in)
        dialog_in.date = datetime(2024, 1, 1, 0, 0, 0)
        backup = _make_sweep_backup(
            followed={f_in}, whitelist_mode=False, chat_ids=set(), main_dialogs=[dialog_in]
        )
        _run(backup.backup_all())
        self.assertIn(f_in, _backed_up_ids(backup))

    def test_whitelist_mode_elif_injection(self):
        """A followed id rides the same elif fallback in whitelist mode too."""
        followed = get_peer_id(PeerChannel(555))
        whitelisted = -999
        dialog_followed = MagicMock()
        dialog_followed.entity = _Ent(followed)
        dialog_followed.date = datetime(2024, 1, 1, 0, 0, 0)
        dialog_whitelisted = MagicMock()
        dialog_whitelisted.entity = _Ent(whitelisted)
        dialog_whitelisted.date = datetime(2024, 1, 1, 0, 0, 0)
        backup = _make_sweep_backup(
            followed={followed},
            whitelist_mode=True,
            chat_ids={whitelisted},
            main_dialogs=[dialog_followed, dialog_whitelisted],
        )
        _run(backup.backup_all())
        backed = _backed_up_ids(backup)
        self.assertIn(followed, backed)
        self.assertIn(whitelisted, backed)

    def test_excluded_followed_id_not_captured(self):
        """A followed id under an explicit GROUPS_EXCLUDE_CHAT_IDS is never
        pulled in via the elif fallback (the exclude gate runs first)."""
        followed = get_peer_id(PeerChannel(555))
        dialog_in = MagicMock()
        dialog_in.entity = _Ent(followed)
        dialog_in.date = datetime(2024, 1, 1, 0, 0, 0)
        backup = _make_sweep_backup(
            followed={followed},
            whitelist_mode=False,
            chat_ids=set(),
            main_dialogs=[dialog_in],
            exclude_ids={followed},
        )
        _run(backup.backup_all())
        self.assertNotIn(followed, _backed_up_ids(backup))


# ===========================================================================
# get_migration_markers — offline-detection adapter read (real in-memory DB)
# ===========================================================================


@pytest_asyncio.fixture
async def migration_adapter():
    """In-memory DB seeded with a mix of migration and non-migration service rows."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    db_manager = DatabaseManager.__new__(DatabaseManager)
    db_manager.engine = engine
    db_manager.database_url = "sqlite+aiosqlite://"
    db_manager._is_sqlite = True
    db_manager.async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    new_id = get_peer_id(PeerChannel(555))
    rows = [
        (1, -777, json.dumps({"service_type": "service", "action_type": "chat_migrate_to", "migrate_to_id": new_id})),
        (1, -1, json.dumps({"action_type": "chat_edit_title", "new_title": "x"})),
        (1, -2, "not valid json"),
        (2, -3, None),
        (1, -4, json.dumps({"action_type": "chat_migrate_to"})),  # missing pointer
        (2, -5, json.dumps({"action_type": "chat_migrate_to", "migrate_to_id": "nope"})),  # non-int pointer
    ]
    async with db_manager.async_session_factory() as session:
        for chat_id in {chat_id for _mid, chat_id, _raw in rows}:
            session.add(Chat(id=chat_id, type="group", title="Chat"))
        await session.flush()
        for mid, chat_id, raw in rows:
            session.add(Message(id=mid, chat_id=chat_id, date=datetime(2024, 1, 1), text="", raw_data=raw))
        await session.commit()

    yield DatabaseAdapter(db_manager), new_id
    await engine.dispose()


class TestGetMigrationMarkers:
    async def test_returns_only_valid_migrate_to_markers(self, migration_adapter):
        adapter, new_id = migration_adapter
        markers = await adapter.get_migration_markers()
        assert markers == [(-777, new_id)]


if __name__ == "__main__":
    unittest.main()
