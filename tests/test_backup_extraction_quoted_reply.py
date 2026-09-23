"""Tests for quoted-reply excerpt capture in ``_process_message`` (#362).

``message.reply_to`` is a Telethon ``MessageReplyHeader``, which never had a
``.message`` attribute. The old ``hasattr(reply_msg, "message")`` guard could
therefore never be true, so a reply's quoted excerpt was silently dropped at
capture time. The fix reads ``quote_text`` instead, the field Telegram
actually populates when the sender selects specific text to quote.
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


def _make_message(reply_to=None, reply_to_msg_id=None):
    msg = MagicMock()
    msg.id = 100
    msg.sender = None
    msg.sender_id = 42
    msg.date = datetime(2024, 1, 15, 12, 0, 0)
    msg.text = "reply text"
    msg.reply_to_msg_id = reply_to_msg_id
    msg.reply_to = reply_to
    msg.edit_date = None
    msg.out = False
    msg.pinned = False
    msg.grouped_id = None
    msg.fwd_from = None
    msg.media = None
    msg.reactions = None
    msg.post_author = None
    msg.action = None
    msg.entities = None
    return msg


def _make_backup():
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.config = MagicMock()
    backup.db = AsyncMock()
    backup.client = AsyncMock()
    return backup


def _make_reply_header(quote_text=None):
    """A stand-in for Telethon's MessageReplyHeader: no `.message` attribute."""
    header = MagicMock(spec=["forum_topic", "reply_to_top_id", "reply_to_msg_id", "quote_text"])
    header.forum_topic = False
    header.quote_text = quote_text
    return header


class TestQuotedReplyExcerpt(unittest.TestCase):
    def test_captures_quote_text_when_sender_quoted_specific_text(self):
        reply_header = _make_reply_header(quote_text="the exact quoted part")
        backup = _make_backup()

        result = _run(backup._process_message(_make_message(reply_to=reply_header, reply_to_msg_id=99), -100))

        self.assertEqual(result["reply_to_text"], "the exact quoted part")

    def test_truncates_quote_text_to_100_chars_like_telegram(self):
        long_quote = "x" * 150
        reply_header = _make_reply_header(quote_text=long_quote)
        backup = _make_backup()

        result = _run(backup._process_message(_make_message(reply_to=reply_header, reply_to_msg_id=99), -100))

        self.assertEqual(result["reply_to_text"], "x" * 100)

    def test_plain_reply_without_quote_leaves_reply_to_text_none(self):
        """A reply with no explicit quote selection has quote_text unset."""
        reply_header = _make_reply_header(quote_text=None)
        backup = _make_backup()

        result = _run(backup._process_message(_make_message(reply_to=reply_header, reply_to_msg_id=99), -100))

        self.assertIsNone(result["reply_to_text"])

    def test_no_reply_leaves_reply_to_text_none(self):
        backup = _make_backup()

        result = _run(backup._process_message(_make_message(reply_to=None, reply_to_msg_id=None), -100))

        self.assertIsNone(result["reply_to_text"])


if __name__ == "__main__":
    unittest.main()
