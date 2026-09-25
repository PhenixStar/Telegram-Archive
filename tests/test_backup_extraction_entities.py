"""Tests for formatting-entity capture in ``_process_message`` (#402).

Entity offsets are UTF-16 code units into ``message.raw_text`` (the
unmodified server text), not the markdown-rendered ``text`` column this
project already stores. Both are captured into ``raw_data`` (``raw_text`` +
``entities``) so entity offsets stay resolvable, additively, without changing
what the ``text`` column means.
"""

import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from telethon.tl.types import (
    MessageEntityBold,
    MessageEntityCode,
    MessageEntityMentionName,
    MessageEntityPre,
    MessageEntitySpoiler,
    MessageEntityTextUrl,
)

from src.telegram_backup import TelegramBackup


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_message(entities=None, raw_text="hello world"):
    msg = MagicMock()
    msg.id = 100
    msg.sender = None
    msg.sender_id = 42
    msg.date = datetime(2024, 1, 15, 12, 0, 0)
    msg.text = raw_text
    msg.raw_text = raw_text
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
    msg.entities = entities
    return msg


def _make_backup():
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.config = MagicMock()
    backup.db = AsyncMock()
    backup.client = AsyncMock()
    return backup


class TestFormattingEntities(unittest.TestCase):
    def test_no_entities_omits_raw_data_keys(self):
        backup = _make_backup()

        result = _run(backup._process_message(_make_message(entities=None), -100))

        self.assertNotIn("entities", result["raw_data"])
        self.assertNotIn("raw_text", result["raw_data"])

    def test_captures_raw_text_alongside_entities(self):
        backup = _make_backup()
        entities = [MessageEntityBold(offset=0, length=5)]

        result = _run(backup._process_message(_make_message(entities=entities, raw_text="hello world"), -100))

        self.assertEqual(result["raw_data"]["raw_text"], "hello world")
        self.assertEqual(result["raw_data"]["entities"], [{"type": "bold", "offset": 0, "length": 5}])

    def test_text_url_entity_includes_url(self):
        backup = _make_backup()
        entities = [MessageEntityTextUrl(offset=0, length=4, url="https://example.com")]

        result = _run(backup._process_message(_make_message(entities=entities), -100))

        self.assertEqual(
            result["raw_data"]["entities"],
            [{"type": "text_url", "offset": 0, "length": 4, "url": "https://example.com"}],
        )

    def test_mention_name_entity_is_stored_as_a_mention_with_its_user_id(self):
        backup = _make_backup()
        entities = [MessageEntityMentionName(offset=0, length=4, user_id=555)]

        result = _run(backup._process_message(_make_message(entities=entities), -100))

        self.assertEqual(
            result["raw_data"]["entities"],
            # Stored as "mention": a mention by user id is rendered exactly like
            # a plain one, and the viewer only knows the Telegram vocabulary.
            [{"type": "mention", "offset": 0, "length": 4, "user_id": 555}],
        )

    def test_pre_entity_includes_language(self):
        backup = _make_backup()
        entities = [MessageEntityPre(offset=0, length=10, language="python")]

        result = _run(backup._process_message(_make_message(entities=entities), -100))

        self.assertEqual(
            result["raw_data"]["entities"],
            [{"type": "pre", "offset": 0, "length": 10, "language": "python"}],
        )

    def test_multiple_entity_types_normalize_to_snake_case(self):
        backup = _make_backup()
        entities = [
            MessageEntitySpoiler(offset=0, length=3),
            MessageEntityCode(offset=4, length=6),
        ]

        result = _run(backup._process_message(_make_message(entities=entities), -100))

        self.assertEqual(
            result["raw_data"]["entities"],
            [
                {"type": "spoiler", "offset": 0, "length": 3},
                {"type": "code", "offset": 4, "length": 6},
            ],
        )

    def test_does_not_change_the_text_column(self):
        """Additive per an explicit user decision: `text` keeps its markdown value."""
        backup = _make_backup()
        entities = [MessageEntityBold(offset=0, length=5)]
        message = _make_message(entities=entities, raw_text="**hi** there")
        message.text = "**hi** there"  # markdown text column, unrelated to raw_text

        result = _run(backup._process_message(message, -100))

        self.assertEqual(result["text"], "**hi** there")
        self.assertEqual(result["raw_data"]["raw_text"], "**hi** there")


if __name__ == "__main__":
    unittest.main()


class TestEntityTypeVocabulary:
    """The stored type strings are a contract with the viewer's renderer."""

    def test_telethon_names_map_to_the_names_the_viewer_renders(self):
        from telethon.tl import types as t

        from src.message_utils import _entity_type

        expected = {
            "Bold": "bold",
            "Italic": "italic",
            "Underline": "underline",
            # Telethon calls it Strike; Telegram's own vocabulary, and the viewer,
            # call it strikethrough.
            "Strike": "strikethrough",
            "Spoiler": "spoiler",
            "Code": "code",
            "Pre": "pre",
            "Blockquote": "blockquote",
            "TextUrl": "text_url",
            "Url": "url",
            "Mention": "mention",
            # A mention by user id is displayed like any other mention.
            "MentionName": "mention",
            "Hashtag": "hashtag",
        }
        for suffix, stored in expected.items():
            cls = getattr(t, "MessageEntity" + suffix)
            assert _entity_type(cls.__new__(cls)) == stored, suffix
