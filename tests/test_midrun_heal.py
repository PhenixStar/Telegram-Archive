"""Unit tests for mid-run reconnect healing (fork port of upstream issue #265).

Telethon stops reconnecting after a connection failure and marks the client
disconnected; every later request then raises until something calls
``connect()`` again. Before this port, only ``scheduler.py`` called
``ensure_connected()``, and only at the START of each backup job — so a
network outage mid-run failed every remaining item in that run.

``TelegramBackup`` can now optionally hold a reference to the shared
``TelegramConnection``. When a connection-type error is caught mid-run,
``_heal_connection()`` calls ``ensure_connected()`` and refreshes
``self.client`` so the REST of the current run can continue, instead of
waiting for the next scheduled cycle. Omitting ``connection=`` (the old
contract) leaves behavior unchanged: the current item is skipped and the
client stays disconnected until the next cycle reconnects it.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from src.telegram_backup import TelegramBackup


def _make_backup(*, connection=None):
    """Build a bare TelegramBackup, bypassing __init__ (matches the
    convention used elsewhere in this test suite), with just the attributes
    the heal path touches."""
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.client = MagicMock()
    backup._connection = connection
    return backup


class TestInitStoresConnection(unittest.TestCase):
    """TelegramBackup.__init__ accepts and stores an optional connection,
    without disturbing the pre-existing client= contract."""

    def test_connection_defaults_to_none(self):
        backup = TelegramBackup(MagicMock(), MagicMock())
        self.assertIsNone(backup._connection)

    def test_connection_is_stored_when_provided(self):
        connection = MagicMock()
        backup = TelegramBackup(MagicMock(), MagicMock(), connection=connection)
        self.assertIs(backup._connection, connection)

    def test_client_param_still_works_without_connection(self):
        """Old contract: constructing with just client= is unaffected."""
        client = MagicMock()
        backup = TelegramBackup(MagicMock(), MagicMock(), client=client)
        self.assertIs(backup.client, client)
        self.assertIsNone(backup._connection)


class TestHealConnection(unittest.IsolatedAsyncioTestCase):
    """_heal_connection: the mid-run reconnect helper itself."""

    async def test_noop_when_no_connection(self):
        """Old contract: with connection=None, healing is a no-op and the
        client is left untouched."""
        backup = _make_backup(connection=None)
        original_client = backup.client

        await backup._heal_connection()

        self.assertIs(backup.client, original_client)

    async def test_ensures_connected_and_refreshes_client(self):
        """With a connection set, healing calls ensure_connected() and
        adopts the (possibly new) healed client."""
        healed_client = MagicMock()
        connection = MagicMock()
        connection.ensure_connected = AsyncMock(return_value=healed_client)
        connection.client = healed_client
        backup = _make_backup(connection=connection)

        await backup._heal_connection()

        connection.ensure_connected.assert_awaited_once()
        self.assertIs(backup.client, healed_client)

    async def test_swallows_reconnect_failure(self):
        """A still-down reconnect attempt is logged, not raised, so the
        caller's surrounding retry/skip loop keeps working."""
        connection = MagicMock()
        connection.ensure_connected = AsyncMock(side_effect=ConnectionError("still down"))
        backup = _make_backup(connection=connection)

        await backup._heal_connection()  # must not raise

        connection.ensure_connected.assert_awaited_once()


class TestRefreshMediaHealsOnConnectionError(unittest.IsolatedAsyncioTestCase):
    """`_refresh_message_for_media`'s connection-error path heals the shared
    connection when one was provided, and stays a no-op otherwise."""

    async def test_heals_when_connection_provided(self):
        connection = MagicMock()
        connection.ensure_connected = AsyncMock(return_value=MagicMock())
        connection.client = connection.ensure_connected.return_value
        backup = _make_backup(connection=connection)
        backup.client.get_messages = AsyncMock(side_effect=ConnectionError("boom"))

        result = await backup._refresh_message_for_media(7, MagicMock(id=1))

        self.assertIsNone(result)
        connection.ensure_connected.assert_awaited_once()

    async def test_no_heal_when_connection_is_none(self):
        """Old contract: no connection means no reconnect attempt, just the
        existing best-effort None return."""
        backup = _make_backup(connection=None)
        backup.client.get_messages = AsyncMock(side_effect=ConnectionError("boom"))

        result = await backup._refresh_message_for_media(7, MagicMock(id=1))

        self.assertIsNone(result)  # unchanged: no exception raised


class TestFillGapsHealsOnConnectionError(unittest.IsolatedAsyncioTestCase):
    """`_fill_gaps`'s per-gap loop heals on a connection error and moves on
    to the next gap in the same run, instead of aborting the whole sweep."""

    async def test_heals_and_continues_after_connection_error_mid_gap(self):
        connection = MagicMock()
        connection.ensure_connected = AsyncMock(return_value=MagicMock())
        connection.client = connection.ensure_connected.return_value
        backup = _make_backup(connection=connection)
        backup.config = MagicMock(gap_threshold=50)
        backup.db = AsyncMock()
        backup.db.get_chats_with_messages = AsyncMock(return_value=[123])
        backup.db.detect_message_gaps = AsyncMock(return_value=[(10, 20, 10), (30, 40, 10)])
        backup.client.get_entity = AsyncMock(return_value=MagicMock())
        backup._get_chat_name = MagicMock(return_value="Test Chat")
        # First gap hits a connection drop; second gap succeeds after healing.
        backup._fill_gap_range = AsyncMock(side_effect=[ConnectionError("dropped"), 3])

        summary = await backup._fill_gaps()

        connection.ensure_connected.assert_awaited_once()
        self.assertEqual(summary["total_gaps"], 2)
        self.assertEqual(summary["total_recovered"], 3)  # only the healed second gap counted

    async def test_no_heal_when_connection_is_none(self):
        """Old contract: without a connection, a connection error during
        gap-fill is logged and the gap is skipped — no reconnect attempted."""
        backup = _make_backup(connection=None)
        backup.config = MagicMock(gap_threshold=50)
        backup.db = AsyncMock()
        backup.db.get_chats_with_messages = AsyncMock(return_value=[123])
        backup.db.detect_message_gaps = AsyncMock(return_value=[(10, 20, 10)])
        backup.client.get_entity = AsyncMock(return_value=MagicMock())
        backup._get_chat_name = MagicMock(return_value="Test Chat")
        backup._fill_gap_range = AsyncMock(side_effect=ConnectionError("dropped"))

        summary = await backup._fill_gaps()  # must not raise

        self.assertEqual(summary["total_recovered"], 0)


if __name__ == "__main__":
    unittest.main()
