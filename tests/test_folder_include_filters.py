"""Tests for the opt-in *_INCLUDE_FOLDER_IDS backup-selection filters
(semantic port of upstream #448: "back up whatever a Telegram folder
currently contains").

Covers:
- src.folder_utils.FolderPeers / resolve_include_folder_chat_ids (pure)
- Config parsing/validation and the has_folder_include_filters /
  update_folder_resolved_chat_ids surface
- should_backup_chat precedence: folder ids are additive with the matching
  *_INCLUDE_CHAT_IDS list, an exclude list still wins, and CHAT_IDS
  whitelist mode ignores folder ids entirely
- The default-off path (no *_INCLUDE_FOLDER_IDS set) is unchanged
- TelegramBackup._sync_folder_include_filters: no-ops when unused/whitelist
  mode, resolves live membership on success, and best-effort keeps the last
  good snapshot on failure
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.config import Config
from src.folder_utils import FolderPeers, resolve_include_folder_chat_ids
from src.telegram_backup import TelegramBackup

# --- pure resolver -----------------------------------------------------------


def test_union_of_configured_folders_only():
    folders = [
        FolderPeers(folder_id=1, peer_ids=frozenset({100, 200})),
        FolderPeers(folder_id=2, peer_ids=frozenset({300})),
        FolderPeers(folder_id=3, peer_ids=frozenset({999})),  # not requested
    ]
    assert resolve_include_folder_chat_ids(folders, {1, 2}) == frozenset({100, 200, 300})


def test_empty_folder_ids_yields_empty_set():
    folders = [FolderPeers(folder_id=1, peer_ids=frozenset({100}))]
    assert resolve_include_folder_chat_ids(folders, frozenset()) == frozenset()


def test_unresolvable_folder_id_contributes_nothing():
    """A configured folder id Telegram doesn't have fails closed, not open."""
    folders = [FolderPeers(folder_id=1, peer_ids=frozenset({100}))]
    assert resolve_include_folder_chat_ids(folders, {999}) == frozenset()


# --- Config -------------------------------------------------------------------


class TestFolderIncludeConfig(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _config(self, **extra_env):
        env_vars = {"CHAT_TYPES": "private,groups,channels", "BACKUP_PATH": self.temp_dir, **extra_env}
        with patch.dict(os.environ, env_vars, clear=True):
            return Config()

    def test_defaults_off(self):
        """No *_INCLUDE_FOLDER_IDS set -> feature fully off, live sets empty."""
        config = self._config()
        self.assertFalse(config.has_folder_include_filters)
        self.assertEqual(config.global_include_folder_ids, set())
        self.assertEqual(config.global_include_folder_chat_ids, frozenset())

    def test_global_alias_and_scoped_vars_parsed(self):
        config = self._config(
            INCLUDE_FOLDER_IDS="1,2",
            PRIVATE_INCLUDE_FOLDER_IDS="3",
            GROUPS_INCLUDE_FOLDER_IDS="4",
            CHANNELS_INCLUDE_FOLDER_IDS="5",
        )
        self.assertEqual(config.global_include_folder_ids, {1, 2})
        self.assertEqual(config.private_include_folder_ids, {3})
        self.assertEqual(config.groups_include_folder_ids, {4})
        self.assertEqual(config.channels_include_folder_ids, {5})
        self.assertTrue(config.has_folder_include_filters)

    def test_global_scoped_var_wins_over_alias(self):
        config = self._config(INCLUDE_FOLDER_IDS="1", GLOBAL_INCLUDE_FOLDER_IDS="2")
        self.assertEqual(config.global_include_folder_ids, {2})

    def test_update_folder_resolved_chat_ids_replaces_live_snapshot(self):
        config = self._config(GROUPS_INCLUDE_FOLDER_IDS="7")
        self.assertEqual(config.groups_include_folder_chat_ids, frozenset())
        config.update_folder_resolved_chat_ids(group_ids={-100111, -100222})
        self.assertEqual(config.groups_include_folder_chat_ids, frozenset({-100111, -100222}))
        # Other scopes are untouched by a partial update.
        self.assertEqual(config.global_include_folder_chat_ids, frozenset())


class TestShouldBackupChatFolderPrecedence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _config(self, **extra_env):
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir, **extra_env}
        with patch.dict(os.environ, env_vars, clear=True):
            return Config()

    def test_default_off_path_is_unchanged(self):
        """No folder ids configured -> should_backup_chat behaves exactly as before.

        CHAT_TYPES=private only: a group is out of scope, a private chat is in.
        """
        config = self._config()
        self.assertFalse(config.should_backup_chat(-100123, is_user=False, is_group=True, is_channel=False))
        self.assertTrue(config.should_backup_chat(555, is_user=True, is_group=False, is_channel=False))

    def test_folder_id_before_refresh_admits_nothing(self):
        """Fails closed: configured but not yet resolved -> no extra chats admitted."""
        config = self._config(GROUPS_INCLUDE_FOLDER_IDS="9")
        self.assertFalse(config.should_backup_chat(-100123, is_user=False, is_group=True, is_channel=False))

    def test_folder_resolved_membership_admits_chat_outside_chat_types(self):
        """A group is admitted via the folder even though CHAT_TYPES=private only."""
        config = self._config(GROUPS_INCLUDE_FOLDER_IDS="9")
        config.update_folder_resolved_chat_ids(group_ids={-100123})
        self.assertTrue(config.should_backup_chat(-100123, is_user=False, is_group=True, is_channel=False))
        # A different group not in the resolved set stays out of scope.
        self.assertFalse(config.should_backup_chat(-100999, is_user=False, is_group=True, is_channel=False))

    def test_folder_ids_are_additive_with_static_include_ids(self):
        config = self._config(GROUPS_INCLUDE_CHAT_IDS="-100111", GROUPS_INCLUDE_FOLDER_IDS="9")
        config.update_folder_resolved_chat_ids(group_ids={-100222})
        self.assertTrue(config.should_backup_chat(-100111, is_user=False, is_group=True, is_channel=False))
        self.assertTrue(config.should_backup_chat(-100222, is_user=False, is_group=True, is_channel=False))

    def test_exclude_list_still_wins_over_folder_membership(self):
        config = self._config(GROUPS_INCLUDE_FOLDER_IDS="9", GROUPS_EXCLUDE_CHAT_IDS="-100123")
        config.update_folder_resolved_chat_ids(group_ids={-100123})
        self.assertFalse(config.should_backup_chat(-100123, is_user=False, is_group=True, is_channel=False))

    def test_whitelist_mode_ignores_folder_ids_entirely(self):
        config = self._config(CHAT_IDS="555", GROUPS_INCLUDE_FOLDER_IDS="9")
        config.update_folder_resolved_chat_ids(group_ids={-100123})
        self.assertTrue(config.whitelist_mode)
        self.assertFalse(config.should_backup_chat(-100123, is_user=False, is_group=True, is_channel=False))
        self.assertTrue(config.should_backup_chat(555, is_user=True, is_group=False, is_channel=False))

    def test_global_include_folder_ids_apply_across_all_types(self):
        config = self._config(GLOBAL_INCLUDE_FOLDER_IDS="9")
        config.update_folder_resolved_chat_ids(global_ids={-1002003})
        self.assertTrue(config.should_backup_chat(-1002003, is_user=False, is_group=False, is_channel=True))


# --- TelegramBackup._sync_folder_include_filters ------------------------------


class _DialogFilterStub:
    """Minimal stand-in for telethon's DialogFilter."""

    def __init__(self, folder_id, pinned_peers=(), include_peers=()):
        self.id = folder_id
        self.pinned_peers = tuple(pinned_peers)
        self.include_peers = tuple(include_peers)


class TestSyncFolderIncludeFilters(unittest.TestCase):
    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.client = AsyncMock()
        self.backup._resolve_peer_ids = MagicMock(side_effect=lambda peers, own_id=None: set(peers))
        self.backup._get_own_id = AsyncMock(return_value=42)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_noop_when_no_folder_filters_configured(self):
        self.backup.config = MagicMock()
        self.backup.config.whitelist_mode = False
        self.backup.config.has_folder_include_filters = False

        self._run(self.backup._sync_folder_include_filters(own_id=42))

        self.backup.client.assert_not_called()
        self.backup.config.update_folder_resolved_chat_ids.assert_not_called()

    def test_noop_in_whitelist_mode_even_with_folder_filters(self):
        self.backup.config = MagicMock()
        self.backup.config.whitelist_mode = True
        self.backup.config.has_folder_include_filters = True

        self._run(self.backup._sync_folder_include_filters(own_id=42))

        self.backup.client.assert_not_called()
        self.backup.config.update_folder_resolved_chat_ids.assert_not_called()

    def test_successful_refresh_resolves_membership_per_scope(self):
        self.backup.config = MagicMock()
        self.backup.config.whitelist_mode = False
        self.backup.config.has_folder_include_filters = True
        self.backup.config.global_include_folder_ids = frozenset()
        self.backup.config.private_include_folder_ids = frozenset()
        self.backup.config.groups_include_folder_ids = frozenset({9})
        self.backup.config.channels_include_folder_ids = frozenset()

        from telethon.tl.types import DialogFilter

        folder = DialogFilter(
            id=9,
            title="Work groups",
            pinned_peers=[],
            include_peers=[],
            exclude_peers=[],
            contacts=False,
            non_contacts=False,
            groups=False,
            broadcasts=False,
            bots=False,
            exclude_muted=False,
            exclude_read=False,
            exclude_archived=False,
            emoticon=None,
        )
        self.backup._resolve_peer_ids = MagicMock(return_value={-100123, -100456})
        result_obj = MagicMock()
        result_obj.filters = [folder]
        self.backup.client.return_value = result_obj

        self._run(self.backup._sync_folder_include_filters(own_id=42))

        self.backup.config.update_folder_resolved_chat_ids.assert_called_once_with(
            global_ids=frozenset(),
            private_ids=frozenset(),
            group_ids=frozenset({-100123, -100456}),
            channel_ids=frozenset(),
        )

    def test_missing_configured_folder_logs_warning_and_admits_nothing(self):
        self.backup.config = MagicMock()
        self.backup.config.whitelist_mode = False
        self.backup.config.has_folder_include_filters = True
        self.backup.config.global_include_folder_ids = frozenset({999})  # does not exist on the account
        self.backup.config.private_include_folder_ids = frozenset()
        self.backup.config.groups_include_folder_ids = frozenset()
        self.backup.config.channels_include_folder_ids = frozenset()

        result_obj = MagicMock()
        result_obj.filters = []  # account has no folders at all
        self.backup.client.return_value = result_obj

        with self.assertLogs("src.telegram_backup", level="WARNING") as cm:
            self._run(self.backup._sync_folder_include_filters(own_id=42))

        self.backup.config.update_folder_resolved_chat_ids.assert_called_once_with(
            global_ids=frozenset(), private_ids=frozenset(), group_ids=frozenset(), channel_ids=frozenset()
        )
        self.assertTrue(any("not among this account's Telegram" in msg for msg in cm.output))

    def test_configured_folder_with_no_explicit_peers_warns_flag_only_trap(self):
        """A folder built purely from category-flag toggles resolves to zero
        explicit peers -- the operator must be told, not just left with a
        debug line, or the folder silently admits nothing forever."""
        self.backup.config = MagicMock()
        self.backup.config.whitelist_mode = False
        self.backup.config.has_folder_include_filters = True
        self.backup.config.global_include_folder_ids = frozenset()
        self.backup.config.private_include_folder_ids = frozenset()
        self.backup.config.groups_include_folder_ids = frozenset({9})
        self.backup.config.channels_include_folder_ids = frozenset()

        from telethon.tl.types import DialogFilter

        folder = DialogFilter(
            id=9,
            title="All groups (flags only)",
            pinned_peers=[],
            include_peers=[],  # no explicit peers -- built from flags in Telegram
            exclude_peers=[],
            contacts=False,
            non_contacts=False,
            groups=True,
            broadcasts=False,
            bots=False,
            exclude_muted=False,
            exclude_read=False,
            exclude_archived=False,
            emoticon=None,
        )
        self.backup._resolve_peer_ids = MagicMock(return_value=set())
        result_obj = MagicMock()
        result_obj.filters = [folder]
        self.backup.client.return_value = result_obj

        with self.assertLogs("src.telegram_backup", level="WARNING") as cm:
            self._run(self.backup._sync_folder_include_filters(own_id=42))

        self.backup.config.update_folder_resolved_chat_ids.assert_called_once_with(
            global_ids=frozenset(), private_ids=frozenset(), group_ids=frozenset(), channel_ids=frozenset()
        )
        self.assertTrue(any("no explicit (pinned/included) chats" in msg for msg in cm.output))

    def test_fetch_failure_leaves_previous_snapshot_untouched(self):
        self.backup.config = MagicMock()
        self.backup.config.whitelist_mode = False
        self.backup.config.has_folder_include_filters = True
        self.backup.client.side_effect = ConnectionError("boom")

        with self.assertLogs("src.telegram_backup", level="WARNING") as cm:
            self._run(self.backup._sync_folder_include_filters(own_id=42))

        self.backup.config.update_folder_resolved_chat_ids.assert_not_called()
        self.assertTrue(any("Could not refresh folder-based include filters" in msg for msg in cm.output))


if __name__ == "__main__":
    unittest.main()
