"""Tests for capture-time sender_name snapshotting in ``_process_message``
(backup_extraction.py, #241 part B: preserve sender history across capture).
"""

import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from src.telegram_backup import TelegramBackup


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_message(sender=None, sender_id=42, text="hi"):
    """Minimal mock regular message (no service action)."""
    msg = MagicMock()
    msg.id = 100
    msg.sender = sender
    msg.sender_id = sender_id
    msg.date = datetime(2024, 1, 15, 12, 0, 0)
    msg.text = text
    msg.reply_to_msg_id = None
    msg.reply_to = None
    msg.edit_date = None
    msg.out = False
    msg.pinned = False
    msg.grouped_id = None
    msg.fwd_from = None
    msg.media = None
    msg.reactions = None
    msg.post_author = None
    msg.action = None
    return msg


def _make_backup():
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.config = MagicMock()
    backup.db = AsyncMock()
    backup.client = AsyncMock()
    return backup


class TestProcessMessageSenderName(unittest.TestCase):
    def test_captures_first_last_name_from_attached_sender(self):
        sender = MagicMock()
        sender.first_name = "Alice"
        sender.last_name = "Smith"
        sender.title = None
        sender.username = None
        backup = _make_backup()

        result = _run(backup._process_message(_make_message(sender=sender), -100))

        self.assertEqual(result["sender_name"], "Alice Smith")

    def test_falls_back_to_title_for_channel_sender(self):
        sender = MagicMock()
        sender.first_name = None
        sender.last_name = None
        sender.title = "Announcements Channel"
        backup = _make_backup()

        result = _run(backup._process_message(_make_message(sender=sender), -100))

        self.assertEqual(result["sender_name"], "Announcements Channel")

    def test_no_sender_yields_none(self):
        backup = _make_backup()

        result = _run(backup._process_message(_make_message(sender=None), -100))

        self.assertIsNone(result["sender_name"])

    def test_never_resolves_a_missing_sender(self):
        """Scheduled sweeps must never issue an extra get_entity() call for a
        missing sender — that would add flood risk on large histories."""
        backup = _make_backup()
        backup.client.get_entity = AsyncMock(side_effect=AssertionError("must not be called"))

        result = _run(backup._process_message(_make_message(sender=None), -100))

        self.assertIsNone(result["sender_name"])
        backup.client.get_entity.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
