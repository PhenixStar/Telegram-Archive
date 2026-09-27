import asyncio
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.telegram_import import (
    TelegramImporter,
    _build_service_text,
    _detect_media,
    _find_html_files,
    _parse_html_duration,
    _parse_html_export,
    derive_chat_id,
    flatten_text,
    parse_date,
    parse_edited_date,
    parse_from_id,
    parse_html_date,
)


def _make_mock_db() -> AsyncMock:
    """An AsyncMock db seeded so the importer's already-have guards read
    "nothing on record yet" instead of an unconfigured MagicMock.

    An unseeded AsyncMock's awaited calls resolve to a MagicMock, which is
    truthy and (for get_last_message_id) not comparable to an int - a
    test-harness artifact the guards would otherwise trip on, not production
    behavior. Individual tests still override any of these per case (e.g. an
    existing-chat merge-conflict test sets get_chat_stats afterward).
    """
    db = AsyncMock()
    db.get_chat_stats.return_value = {"messages": 0}
    db.get_chat_by_id.return_value = None
    db.get_user_by_id.return_value = None
    db.get_media_for_message.return_value = None
    db.get_last_message_id.return_value = 0
    db.get_setting.return_value = None
    return db


class TestParseFromId(unittest.TestCase):
    def test_user_id(self):
        self.assertEqual(parse_from_id("user123456789"), 123456789)

    def test_channel_id(self):
        self.assertEqual(parse_from_id("channel1234567890"), -1001234567890)

    def test_group_id(self):
        self.assertEqual(parse_from_id("group123456789"), -123456789)

    def test_none(self):
        self.assertIsNone(parse_from_id(None))

    def test_empty_string(self):
        self.assertIsNone(parse_from_id(""))

    def test_unknown_prefix(self):
        self.assertIsNone(parse_from_id("bot123"))

    def test_invalid_number(self):
        self.assertIsNone(parse_from_id("userabc"))


class TestDeriveChatId(unittest.TestCase):
    def test_personal_chat(self):
        self.assertEqual(derive_chat_id(123456, "personal_chat"), 123456)

    def test_bot_chat(self):
        self.assertEqual(derive_chat_id(99999, "bot_chat"), 99999)

    def test_saved_messages(self):
        self.assertEqual(derive_chat_id(42, "saved_messages"), 42)

    def test_private_group(self):
        self.assertEqual(derive_chat_id(123456, "private_group"), -123456)

    def test_private_supergroup(self):
        self.assertEqual(derive_chat_id(1234567890, "private_supergroup"), -1001234567890)

    def test_public_supergroup(self):
        self.assertEqual(derive_chat_id(1234567890, "public_supergroup"), -1001234567890)

    def test_private_channel(self):
        self.assertEqual(derive_chat_id(1234567890, "private_channel"), -1001234567890)

    def test_public_channel(self):
        self.assertEqual(derive_chat_id(1234567890, "public_channel"), -1001234567890)

    def test_unknown_type(self):
        self.assertEqual(derive_chat_id(42, "unknown_type"), 42)


class TestFlattenText(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(flatten_text("Hello world"), "Hello world")

    def test_empty_string(self):
        self.assertEqual(flatten_text(""), "")

    def test_none(self):
        self.assertEqual(flatten_text(None), "")

    def test_entity_list(self):
        entities = [
            {"type": "plain", "text": "Hello "},
            {"type": "bold", "text": "world"},
            {"type": "plain", "text": "!"},
        ]
        self.assertEqual(flatten_text(entities), "Hello world!")

    def test_mixed_list(self):
        entities = ["plain text", {"type": "link", "text": "http://example.com"}]
        self.assertEqual(flatten_text(entities), "plain texthttp://example.com")

    def test_empty_list(self):
        self.assertEqual(flatten_text([]), "")


class TestParseDate(unittest.TestCase):
    def test_unixtime(self):
        msg = {"date_unixtime": "1673779800"}
        result = parse_date(msg)
        self.assertIsInstance(result, datetime)
        self.assertEqual(result.year, 2023)

    def test_iso_format(self):
        msg = {"date": "2023-01-15T10:30:00"}
        result = parse_date(msg)
        self.assertIsInstance(result, datetime)
        self.assertEqual(result.year, 2023)
        self.assertEqual(result.month, 1)
        self.assertEqual(result.day, 15)

    def test_prefers_unixtime(self):
        msg = {"date_unixtime": "1673779800", "date": "2025-06-01T00:00:00"}
        result = parse_date(msg)
        self.assertEqual(result.year, 2023)

    def test_no_date(self):
        self.assertIsNone(parse_date({}))

    def test_invalid_date(self):
        self.assertIsNone(parse_date({"date": "not-a-date"}))

    def test_aware_offset_converted_to_naive_utc(self):
        """An aware ISO string (e.g. from an HTML export) is stored as naive UTC,
        the same instant, not the local wall-clock time relabeled as UTC."""
        msg = {"date": "2024-01-15T10:00:00+02:00"}
        result = parse_date(msg)
        self.assertEqual(result, datetime(2024, 1, 15, 8, 0, 0))

    def test_html_export_offset_survives_into_the_stored_instant(self):
        """End-to-end: parse_html_date's offset, fed back through parse_date,
        lands on the correct UTC instant instead of the exporter's local time."""
        iso = parse_html_date("15.01.2024 10:00:00 UTC+02:00")
        result = parse_date({"date": iso})
        self.assertEqual(result, datetime(2024, 1, 15, 8, 0, 0))


class TestParseEditedDate(unittest.TestCase):
    def test_edited_unixtime(self):
        msg = {"edited_unixtime": "1673780100"}
        result = parse_edited_date(msg)
        self.assertIsInstance(result, datetime)

    def test_edited_iso(self):
        msg = {"edited": "2023-01-15T10:35:00"}
        result = parse_edited_date(msg)
        self.assertIsInstance(result, datetime)

    def test_no_edited(self):
        self.assertIsNone(parse_edited_date({}))


class TestDetectMedia(unittest.TestCase):
    def test_photo(self):
        msg = {"photo": "photos/photo_1.jpg"}
        media_type, rel, fname = _detect_media(msg)
        self.assertEqual(media_type, "photo")
        self.assertEqual(rel, "photos/photo_1.jpg")
        self.assertEqual(fname, "photo_1.jpg")

    def test_document(self):
        msg = {"file": "files/doc.pdf", "file_name": "document.pdf", "mime_type": "application/pdf"}
        media_type, rel, fname = _detect_media(msg)
        self.assertEqual(media_type, "document")
        self.assertEqual(fname, "document.pdf")

    def test_video(self):
        msg = {"file": "videos/vid.mp4", "media_type": "video_file"}
        media_type, rel, fname = _detect_media(msg)
        self.assertEqual(media_type, "video")

    def test_voice(self):
        msg = {"file": "voice/msg.ogg", "media_type": "voice_message"}
        media_type, rel, fname = _detect_media(msg)
        self.assertEqual(media_type, "voice")

    def test_animation(self):
        msg = {"file": "animations/anim.mp4", "media_type": "animation"}
        media_type, rel, fname = _detect_media(msg)
        self.assertEqual(media_type, "animation")

    def test_no_media(self):
        media_type, rel, fname = _detect_media({})
        self.assertIsNone(media_type)
        self.assertIsNone(rel)

    def test_photo_takes_precedence(self):
        msg = {"photo": "photos/p.jpg", "file": "files/f.pdf"}
        media_type, _, _ = _detect_media(msg)
        self.assertEqual(media_type, "photo")


class TestBuildServiceText(unittest.TestCase):
    def test_pin_message(self):
        msg = {"action": "pin_message", "from": "Alice"}
        self.assertIn("pinned a message", _build_service_text(msg))
        self.assertIn("Alice", _build_service_text(msg))

    def test_create_group(self):
        msg = {"action": "create_group", "actor": "Bob", "title": "My Group"}
        result = _build_service_text(msg)
        self.assertIn("Bob", result)
        self.assertIn("created the group", result)
        self.assertIn("My Group", result)

    def test_unknown_action(self):
        msg = {"action": "some_new_action", "from": "Charlie"}
        result = _build_service_text(msg)
        self.assertIn("some new action", result)


class TestTelegramImporterExtractChats(unittest.TestCase):
    def _make_importer(self):
        db = MagicMock()
        return TelegramImporter(db, "/tmp/media")

    def test_single_chat_export(self):
        data = {"name": "Test Chat", "type": "personal_chat", "id": 123, "messages": []}
        importer = self._make_importer()
        chats = importer._extract_chats(data)
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["name"], "Test Chat")

    def test_full_account_export(self):
        data = {
            "chats": {
                "list": [
                    {"name": "Chat 1", "type": "personal_chat", "id": 1, "messages": []},
                    {"name": "Chat 2", "type": "private_group", "id": 2, "messages": []},
                ]
            }
        }
        importer = self._make_importer()
        chats = importer._extract_chats(data)
        self.assertEqual(len(chats), 2)

    def test_empty_data(self):
        importer = self._make_importer()
        self.assertEqual(importer._extract_chats({}), [])


class TestTelegramImporterRun(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _write_export(self, data):
        with open(os.path.join(self.export_dir, "result.json"), "w") as f:
            json.dump(data, f)

    def test_dry_run_no_db_writes(self):
        self._write_export(
            {
                "name": "Test",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "from": "Alice",
                        "from_id": "user42",
                        "text": "Hello",
                    },
                    {
                        "id": 2,
                        "type": "message",
                        "date": "2024-01-15T10:01:00",
                        "from": "Bob",
                        "from_id": "user99",
                        "text": "World",
                    },
                ],
            }
        )

        db = _make_mock_db()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir, dry_run=True))

        self.assertEqual(summary["total_messages"], 2)
        self.assertEqual(summary["chats_imported"], 1)
        db.upsert_chat.assert_not_called()
        db.insert_messages_batch.assert_not_called()

    def test_import_with_merge_check(self):
        self._write_export(
            {
                "name": "Existing Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "text": "Hi"},
                ],
            }
        )

        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 100}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        with self.assertRaises(ValueError) as ctx:
            self._run(importer.run(self.export_dir, merge=False))
        self.assertIn("already has", str(ctx.exception))

    def test_import_messages(self):
        self._write_export(
            {
                "name": "Test Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "from": "Alice",
                        "from_id": "user42",
                        "text": "Hello",
                    },
                    {
                        "id": 2,
                        "type": "service",
                        "date": "2024-01-15T10:05:00",
                        "from": "Alice",
                        "from_id": "user42",
                        "action": "pin_message",
                    },
                ],
            }
        )

        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_messages"], 2)
        db.upsert_chat.assert_called_once()
        db.insert_messages_batch.assert_called_once()
        db.update_sync_status.assert_called_once_with(42, 2, 2)

    def test_import_with_media(self):
        photos_dir = os.path.join(self.export_dir, "photos")
        os.makedirs(photos_dir)
        photo_path = os.path.join(photos_dir, "photo_1.jpg")
        with open(photo_path, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

        self._write_export(
            {
                "name": "Media Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "from": "Alice",
                        "from_id": "user42",
                        "text": "",
                        "photo": "photos/photo_1.jpg",
                        "width": 800,
                        "height": 600,
                    },
                ],
            }
        )

        media_dir = os.path.join(self.temp_dir, "media")
        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, media_dir)

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_media"], 1)
        db.insert_media.assert_called_once()
        media_call = db.insert_media.call_args[0][0]
        self.assertEqual(media_call["type"], "photo")
        self.assertEqual(media_call["message_id"], 1)
        self.assertTrue(Path(media_dir, "42").exists())

    def test_skip_media_flag(self):
        photos_dir = os.path.join(self.export_dir, "photos")
        os.makedirs(photos_dir)
        with open(os.path.join(photos_dir, "photo_1.jpg"), "wb") as f:
            f.write(b"\x00" * 50)

        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "text": "",
                        "photo": "photos/photo_1.jpg",
                    },
                ],
            }
        )

        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir, skip_media=True))

        self.assertEqual(summary["total_media"], 0)
        db.insert_media.assert_not_called()

    def test_missing_result_json(self):
        db = _make_mock_db()
        importer = TelegramImporter(db, "/tmp/media")

        with self.assertRaises(FileNotFoundError):
            self._run(importer.run(self.export_dir))

    def test_forwarded_message(self):
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "text": "Forwarded content",
                        "forwarded_from": "Some Channel",
                    },
                ],
            }
        )

        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir))

        call_args = db.insert_messages_batch.call_args[0][0]
        self.assertEqual(call_args[0]["raw_data"]["forward_from_name"], "Some Channel")

    def test_chat_id_override(self):
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "text": "Hi"},
                ],
            }
        )

        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir, chat_id_override=-1009999))

        self.assertEqual(summary["details"][0]["chat_id"], -1009999)
        chat_call = db.upsert_chat.call_args[0][0]
        self.assertEqual(chat_call["id"], -1009999)


# ---------------------------------------------------------------------------
# HTML import tests
# ---------------------------------------------------------------------------

SAMPLE_HTML_MESSAGE = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Test Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message100">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00 UTC+02:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="text">Hello world!</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_JOINED = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Group Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message200">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="text">First message</div>
   </div>
  </div>
  <div class="message default clearfix joined" id="message201">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:01:00">10:01</div>
    <div class="text">Second message (same sender)</div>
   </div>
  </div>
  <div class="message default clearfix" id="message202">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:02:00">10:02</div>
    <div class="from_name">Bob</div>
    <div class="text">Different sender</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_SERVICE = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Group</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message service" id="message300">
   <div class="body details">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    Alice joined group via invite link
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_REPLY = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message400">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="text">Original message</div>
   </div>
  </div>
  <div class="message default clearfix" id="message401">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:01:00">10:01</div>
    <div class="from_name">Bob</div>
    <div class="reply_to details">
     In reply to <a href="#go_to_message400">this message</a>
    </div>
    <div class="text">This is a reply</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_FORWARDED = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message500">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="forwarded body">
     <div class="from_name">Original Channel</div>
     <div class="text">Forwarded content</div>
    </div>
    <div class="text">Alice's comment</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_PHOTO = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Media Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message600">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <a class="photo_wrap clearfix pull_left" href="photos/photo_1@15-01-2024_10-00-00.jpg">
     <img class="photo" src="photos/photo_1@15-01-2024_10-00-00.jpg" style="width: 320px; height: 240px">
    </a>
    <div class="text">Check this photo!</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_VIDEO = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message700">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="media_wrap clearfix">
     <div class="media clearfix pull_left media_video">
      <a href="video_files/video@15-01-2024_10-00-00.mp4">Video</a>
      <div class="description">01:30</div>
     </div>
    </div>
    <div class="text"></div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_VOICE = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message800">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="media_wrap clearfix">
     <div class="media clearfix pull_left media_voice_message">
      <a href="voice_messages/audio_1@15-01-2024_10-00-00.ogg">Voice message</a>
      <div class="description">00:15</div>
     </div>
    </div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_FILE = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message900">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="media_wrap clearfix">
     <div class="media clearfix pull_left media_file">
      <a href="files/document.pdf">document.pdf (1.2 MB)</a>
     </div>
    </div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""


class TestParseHtmlDate(unittest.TestCase):
    def test_basic_date(self):
        self.assertEqual(parse_html_date("15.01.2024 10:30:00"), "2024-01-15T10:30:00")

    def test_date_with_timezone(self):
        # The offset is preserved rather than discarded: dropping it (the old
        # behavior) shifted every HTML-imported message by the exporter's
        # timezone relative to messages captured live in the same chat.
        self.assertEqual(parse_html_date("15.01.2024 10:30:00 UTC+02:00"), "2024-01-15T10:30:00+02:00")

    def test_date_with_out_of_range_offset_degrades_to_naive(self):
        """A malformed offset (e.g. an impossible UTC+24:00) is dropped, not trusted."""
        self.assertEqual(parse_html_date("15.01.2024 10:30:00 UTC+24:00"), "2024-01-15T10:30:00")

    def test_date_with_invalid_minute_component_degrades_to_naive(self):
        """UTC+02:60 (invalid minutes) is dropped rather than silently normalized."""
        self.assertEqual(parse_html_date("15.01.2024 10:30:00 UTC+02:60"), "2024-01-15T10:30:00")

    def test_date_with_bare_zone_name_degrades_to_naive(self):
        """A non-offset third token (bare zone name) degrades to the old behavior."""
        self.assertEqual(parse_html_date("15.01.2024 10:30:00 CEST"), "2024-01-15T10:30:00")

    def test_empty_string(self):
        self.assertIsNone(parse_html_date(""))

    def test_none(self):
        self.assertIsNone(parse_html_date(None))

    def test_invalid_format(self):
        self.assertIsNone(parse_html_date("not a date"))

    def test_partial_date(self):
        self.assertIsNone(parse_html_date("15.01.2024"))


class TestFindHtmlFiles(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_single_file(self):
        Path(self.temp_dir, "messages.html").touch()
        files = _find_html_files(Path(self.temp_dir))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "messages.html")

    def test_multiple_files(self):
        Path(self.temp_dir, "messages.html").touch()
        Path(self.temp_dir, "messages2.html").touch()
        Path(self.temp_dir, "messages3.html").touch()
        files = _find_html_files(Path(self.temp_dir))
        self.assertEqual(len(files), 3)
        self.assertEqual([f.name for f in files], ["messages.html", "messages2.html", "messages3.html"])

    def test_no_html_files(self):
        files = _find_html_files(Path(self.temp_dir))
        self.assertEqual(files, [])

    def test_only_numbered_files(self):
        # messages2.html without messages.html - starts from messages2
        Path(self.temp_dir, "messages2.html").touch()
        files = _find_html_files(Path(self.temp_dir))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "messages2.html")


class TestParseHtmlDuration(unittest.TestCase):
    def test_minutes_seconds(self):
        self.assertEqual(_parse_html_duration("01:30"), 90)

    def test_hours_minutes_seconds(self):
        self.assertEqual(_parse_html_duration("1:30:00"), 5400)

    def test_zero_duration(self):
        self.assertEqual(_parse_html_duration("00:00"), 0)

    def test_invalid(self):
        self.assertIsNone(_parse_html_duration("not a duration"))

    def test_empty(self):
        self.assertIsNone(_parse_html_duration(""))


class TestParseHtmlExport(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_html(self, content, filename="messages.html"):
        filepath = os.path.join(self.temp_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return filepath

    def test_basic_message(self):
        self._write_html(SAMPLE_HTML_MESSAGE)
        html_files = _find_html_files(Path(self.temp_dir))
        chat_name, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(chat_name, "Test Chat")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], 100)
        self.assertEqual(messages[0]["from"], "Alice")
        self.assertEqual(messages[0]["text"], "Hello world!")
        self.assertEqual(messages[0]["date"], "2024-01-15T10:00:00+02:00")
        self.assertEqual(messages[0]["type"], "message")

    def test_joined_messages(self):
        self._write_html(SAMPLE_HTML_JOINED)
        html_files = _find_html_files(Path(self.temp_dir))
        chat_name, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(chat_name, "Group Chat")
        self.assertEqual(len(messages), 3)
        # Joined message inherits sender from previous
        self.assertEqual(messages[0]["from"], "Alice")
        self.assertEqual(messages[1]["from"], "Alice")
        self.assertEqual(messages[1]["text"], "Second message (same sender)")
        self.assertEqual(messages[2]["from"], "Bob")

    def test_service_message(self):
        self._write_html(SAMPLE_HTML_SERVICE)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["type"], "service")
        self.assertEqual(messages[0]["id"], 300)
        self.assertIn("Alice joined group", messages[0]["text"])

    def test_reply_reference(self):
        self._write_html(SAMPLE_HTML_REPLY)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(len(messages), 2)
        self.assertIsNone(messages[0].get("reply_to_message_id"))
        self.assertEqual(messages[1]["reply_to_message_id"], 400)
        self.assertEqual(messages[1]["text"], "This is a reply")

    def test_forwarded_message(self):
        self._write_html(SAMPLE_HTML_FORWARDED)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(len(messages), 1)
        # Sender should be the forwarder (Alice), not the original (from .forwarded body)
        self.assertEqual(messages[0]["from"], "Alice")
        self.assertEqual(messages[0]["forwarded_from"], "Original Channel")
        self.assertEqual(messages[0]["text"], "Alice's comment")

    def test_photo_media(self):
        self._write_html(SAMPLE_HTML_PHOTO)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["photo"], "photos/photo_1@15-01-2024_10-00-00.jpg")
        self.assertEqual(messages[0]["width"], 320)
        self.assertEqual(messages[0]["height"], 240)
        self.assertEqual(messages[0]["text"], "Check this photo!")

    def test_video_media(self):
        self._write_html(SAMPLE_HTML_VIDEO)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["file"], "video_files/video@15-01-2024_10-00-00.mp4")
        self.assertEqual(messages[0]["media_type"], "video_file")
        self.assertEqual(messages[0]["duration_seconds"], 90)

    def test_voice_media(self):
        self._write_html(SAMPLE_HTML_VOICE)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["file"], "voice_messages/audio_1@15-01-2024_10-00-00.ogg")
        self.assertEqual(messages[0]["media_type"], "voice_message")
        self.assertEqual(messages[0]["duration_seconds"], 15)

    def test_file_media(self):
        self._write_html(SAMPLE_HTML_FILE)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["file"], "files/document.pdf")
        self.assertEqual(messages[0]["file_name"], "document.pdf")

    def test_multi_file_html(self):
        """Test that multiple HTML files are combined in order."""
        html1 = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Multi Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message1">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="text">Message in file 1</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>"""
        html2 = """\
<html><body>
<div class="page_wrap">
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message2">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 11:00:00">11:00</div>
    <div class="from_name">Bob</div>
    <div class="text">Message in file 2</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>"""
        self._write_html(html1, "messages.html")
        self._write_html(html2, "messages2.html")

        html_files = _find_html_files(Path(self.temp_dir))
        chat_name, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(chat_name, "Multi Chat")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["text"], "Message in file 1")
        self.assertEqual(messages[1]["text"], "Message in file 2")

    def test_message_without_id_skipped(self):
        html = """\
<html><body>
<div class="page_wrap">
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix">
   <div class="body">
    <div class="from_name">Alice</div>
    <div class="text">No ID message</div>
   </div>
  </div>
  <div class="message default clearfix" id="message1">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="text">Has ID</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>"""
        self._write_html(html)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], "Has ID")


class TestHtmlImportIntegration(unittest.TestCase):
    """Integration tests for HTML import through TelegramImporter."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _write_html(self, content, filename="messages.html"):
        filepath = os.path.join(self.export_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

    def test_html_import_requires_chat_id(self):
        self._write_html(SAMPLE_HTML_MESSAGE)
        db = _make_mock_db()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        with self.assertRaises(ValueError) as ctx:
            self._run(importer.run(self.export_dir))
        self.assertIn("chat ID", str(ctx.exception))

    def test_html_import_basic(self):
        self._write_html(SAMPLE_HTML_MESSAGE)
        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir, chat_id_override=-1001234567890))

        self.assertEqual(summary["total_messages"], 1)
        self.assertEqual(summary["chats_imported"], 1)
        self.assertEqual(summary["details"][0]["chat_name"], "Test Chat")
        self.assertEqual(summary["details"][0]["chat_id"], -1001234567890)
        db.upsert_chat.assert_called_once()
        db.insert_messages_batch.assert_called_once()

    def test_html_import_dry_run(self):
        self._write_html(SAMPLE_HTML_MESSAGE)
        db = _make_mock_db()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir, chat_id_override=42, dry_run=True))

        self.assertEqual(summary["total_messages"], 1)
        db.upsert_chat.assert_not_called()
        db.insert_messages_batch.assert_not_called()

    def test_html_import_with_media(self):
        self._write_html(SAMPLE_HTML_PHOTO)

        # Create the actual photo file
        photos_dir = os.path.join(self.export_dir, "photos")
        os.makedirs(photos_dir)
        with open(os.path.join(photos_dir, "photo_1@15-01-2024_10-00-00.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

        media_dir = os.path.join(self.temp_dir, "media")
        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, media_dir)

        summary = self._run(importer.run(self.export_dir, chat_id_override=42))

        self.assertEqual(summary["total_media"], 1)
        db.insert_media.assert_called_once()
        media_call = db.insert_media.call_args[0][0]
        self.assertEqual(media_call["type"], "photo")
        self.assertTrue(Path(media_dir, "42").exists())

    def test_html_import_skip_media(self):
        self._write_html(SAMPLE_HTML_PHOTO)

        photos_dir = os.path.join(self.export_dir, "photos")
        os.makedirs(photos_dir)
        with open(os.path.join(photos_dir, "photo_1@15-01-2024_10-00-00.jpg"), "wb") as f:
            f.write(b"\x00" * 50)

        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir, chat_id_override=42, skip_media=True))

        self.assertEqual(summary["total_media"], 0)
        db.insert_media.assert_not_called()

    def test_html_import_forwarded(self):
        self._write_html(SAMPLE_HTML_FORWARDED)
        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir, chat_id_override=42))

        call_args = db.insert_messages_batch.call_args[0][0]
        self.assertEqual(call_args[0]["raw_data"]["forward_from_name"], "Original Channel")

    def test_html_import_reply(self):
        self._write_html(SAMPLE_HTML_REPLY)
        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir, chat_id_override=42))

        call_args = db.insert_messages_batch.call_args[0][0]
        # First message has no reply
        self.assertIsNone(call_args[0]["reply_to_msg_id"])
        # Second message replies to first
        self.assertEqual(call_args[1]["reply_to_msg_id"], 400)

    def test_json_takes_priority_over_html(self):
        """When both result.json and messages.html exist, JSON is used."""
        self._write_html(SAMPLE_HTML_MESSAGE)
        # Also write a result.json
        with open(os.path.join(self.export_dir, "result.json"), "w") as f:
            json.dump(
                {
                    "name": "JSON Chat",
                    "type": "personal_chat",
                    "id": 42,
                    "messages": [
                        {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "text": "From JSON"},
                    ],
                },
                f,
            )

        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir))

        # Should use JSON (chat_id derived from JSON data, not requiring override)
        self.assertEqual(summary["details"][0]["chat_name"], "JSON Chat")

    def test_no_export_files_raises_error(self):
        """Neither result.json nor messages.html should raise FileNotFoundError."""
        db = _make_mock_db()
        importer = TelegramImporter(db, "/tmp/media")

        with self.assertRaises(FileNotFoundError) as ctx:
            self._run(importer.run(self.export_dir))
        self.assertIn("No result.json or messages.html", str(ctx.exception))


# ---------------------------------------------------------------------------
# Sender-name capture (#241 part B)
# ---------------------------------------------------------------------------


class TestImportSenderNameCapture(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _write_export(self, data):
        with open(os.path.join(self.export_dir, "result.json"), "w") as f:
            json.dump(data, f)

    def test_regular_message_uses_from_field(self):
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "from": "  Alice  ",
                        "from_id": "user42",
                        "text": "hi",
                    },
                ],
            }
        )
        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir))

        call_args = db.insert_messages_batch.call_args[0][0]
        self.assertEqual(call_args[0]["sender_name"], "Alice")

    def test_service_message_prefers_actor_over_from(self):
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "service",
                        "date": "2024-01-15T10:00:00",
                        "actor": "Bob",
                        "actor_id": "user7",
                        "from": "Someone Else",
                        "action": "pin_message",
                    },
                ],
            }
        )
        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir))

        call_args = db.insert_messages_batch.call_args[0][0]
        self.assertEqual(call_args[0]["sender_name"], "Bob")

    def test_blank_from_field_becomes_none(self):
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "from": "   ", "text": "hi"},
                ],
            }
        )
        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir))

        call_args = db.insert_messages_batch.call_args[0][0]
        self.assertIsNone(call_args[0]["sender_name"])


# ---------------------------------------------------------------------------
# Secure imports: media path confinement (#241 part A)
# ---------------------------------------------------------------------------


class TestSecureImportPathConfinement(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        os.makedirs(self.export_dir)
        self.media_dir = os.path.join(self.temp_dir, "media")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _write_export(self, data):
        with open(os.path.join(self.export_dir, "result.json"), "w") as f:
            json.dump(data, f)

    def test_traversal_photo_path_rejected(self):
        """A crafted ``../`` photo path must not escape the export directory."""
        # Secret file lives OUTSIDE the export dir; a traversal path would reach it.
        secret_path = os.path.join(self.temp_dir, "secret.jpg")
        with open(secret_path, "wb") as f:
            f.write(b"secret-bytes")

        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "text": "",
                        "photo": "../secret.jpg",
                    },
                ],
            }
        )

        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, self.media_dir)

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_media"], 0)
        db.insert_media.assert_not_called()
        # The traversal target must never be copied into the media store.
        if os.path.isdir(self.media_dir):
            for _root, _dirs, files in os.walk(self.media_dir):
                self.assertNotIn("secret.jpg", files)

    def test_absolute_photo_path_rejected(self):
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "text": "",
                        "photo": "/etc/passwd",
                    },
                ],
            }
        )

        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, self.media_dir)

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_media"], 0)
        db.insert_media.assert_not_called()

    def test_symlinked_export_subdir_component_rejected(self):
        """A symlinked directory component inside the export must not be followed."""
        outside_dir = os.path.join(self.temp_dir, "outside")
        os.makedirs(outside_dir)
        with open(os.path.join(outside_dir, "photo.jpg"), "wb") as f:
            f.write(b"outside-bytes")

        symlink_path = os.path.join(self.export_dir, "photos")
        try:
            os.symlink(outside_dir, symlink_path)
        except OSError:
            self.skipTest("symlinks not supported on this filesystem")

        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "text": "",
                        "photo": "photos/photo.jpg",
                    },
                ],
            }
        )

        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, self.media_dir)

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_media"], 0)
        db.insert_media.assert_not_called()

    def test_missing_media_file_is_skipped_not_fatal(self):
        """A referenced-but-absent media file must not abort the import."""
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "text": "",
                        "photo": "photos/missing.jpg",
                    },
                    {"id": 2, "type": "message", "date": "2024-01-15T10:01:00", "text": "still imported"},
                ],
            }
        )

        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, self.media_dir)

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_media"], 0)
        self.assertEqual(summary["total_messages"], 2)

    def test_valid_media_still_imported_inside_export_root(self):
        """Sanity check: a well-formed relative media path still imports normally."""
        photos_dir = os.path.join(self.export_dir, "photos")
        os.makedirs(photos_dir)
        with open(os.path.join(photos_dir, "ok.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 50)

        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "text": "",
                        "photo": "photos/ok.jpg",
                    },
                ],
            }
        )

        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, self.media_dir)

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_media"], 1)
        db.insert_media.assert_called_once()

    def test_dest_filename_length_is_capped(self):
        """A very long export filename must not blow the on-disk filename budget."""
        from src.telegram_import import _build_import_media_filename

        long_name = "a" * 500 + ".jpg"
        result = _build_import_media_filename("import_42_1", long_name, max_filename_bytes=100)

        self.assertLessEqual(len(result.encode("utf-8")), 100)
        self.assertTrue(result.endswith(".jpg"))

    def test_dest_filename_preserves_short_names(self):
        from src.telegram_import import _build_import_media_filename

        result = _build_import_media_filename("import_42_1", "photo.jpg", max_filename_bytes=143)
        self.assertEqual(result, "import_42_1_photo.jpg")

    def test_dest_filename_sanitizes_traversal_in_original_name(self):
        """A crafted ``file_name`` with path components must be collapsed to a basename."""
        from src.telegram_import import _build_import_media_filename

        result = _build_import_media_filename("import_42_1", "../../etc/passwd", max_filename_bytes=143)
        self.assertNotIn("..", result)
        self.assertNotIn("/", result)


# ---------------------------------------------------------------------------
# Sweep-cursor amputation guard
# ---------------------------------------------------------------------------


class TestSweepCursorGuard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _write_export(self, data):
        with open(os.path.join(self.export_dir, "result.json"), "w") as f:
            json.dump(data, f)

    def test_partial_export_does_not_advance_cursor(self):
        """An export that starts mid-history (ids 8..9, not the chat head) must
        not raise the sweep cursor - the still-retrievable older history would
        silently never be fetched by the next backup run."""
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {"id": 8, "type": "message", "date": "2024-01-15T10:00:00", "text": "A"},
                    {"id": 9, "type": "message", "date": "2024-01-15T10:01:00", "text": "B"},
                ],
            }
        )
        db = _make_mock_db()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_messages"], 2)
        db.update_sync_status.assert_not_called()

    def test_full_export_from_head_advances_cursor(self):
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "text": "A"},
                    {"id": 2, "type": "message", "date": "2024-01-15T10:01:00", "text": "B"},
                ],
            }
        )
        db = _make_mock_db()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir))

        db.update_sync_status.assert_called_once_with(42, 2, 2)

    def test_cursor_never_lowered_below_an_existing_higher_value(self):
        """A chat the API has already swept far ahead of must not have its
        cursor regressed by an older/partial import re-run with --merge."""
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "text": "A"},
                ],
            }
        )
        db = _make_mock_db()
        db.get_chat_stats.return_value = {"messages": 500}
        db.get_last_message_id.return_value = 500
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir, merge=True))

        db.update_sync_status.assert_not_called()

    def test_skipped_message_does_not_advance_cursor_past_itself(self):
        """A message with no valid date is never accepted for insert, so its id
        must not count toward the cursor bounds either."""
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "text": "A"},
                    {"id": 999, "type": "message", "text": "no date, skipped"},
                ],
            }
        )
        db = _make_mock_db()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir))

        db.update_sync_status.assert_called_once_with(42, 1, 1)


# ---------------------------------------------------------------------------
# Chat vocabulary, selective chat upsert, and honest is_outgoing
# ---------------------------------------------------------------------------


class TestChatVocabularyAndSelectiveUpsert(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _write_export(self, data):
        with open(os.path.join(self.export_dir, "result.json"), "w") as f:
            json.dump(data, f)

    def _write_html(self, content):
        with open(os.path.join(self.export_dir, "messages.html"), "w", encoding="utf-8") as f:
            f.write(content)

    def test_chat_type_map_uses_the_forks_private_vocabulary(self):
        """'user' appears nowhere else in this codebase; the viewer/sidebar
        expect 'private' for personal/bot/saved chats."""
        from src.telegram_import import CHAT_TYPE_MAP

        self.assertEqual(CHAT_TYPE_MAP["personal_chat"], "private")
        self.assertEqual(CHAT_TYPE_MAP["bot_chat"], "private")
        self.assertEqual(CHAT_TYPE_MAP["saved_messages"], "private")

    def test_json_personal_chat_upsert_supplies_only_known_fields(self):
        self._write_export(
            {
                "name": "Alice",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "text": "hi"},
                ],
            }
        )
        db = _make_mock_db()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir))

        chat_row = db.upsert_chat.call_args[0][0]
        self.assertEqual(chat_row["type"], "private")
        self.assertEqual(chat_row["first_name"], "Alice")
        self.assertNotIn("title", chat_row)

    def test_html_import_into_existing_chat_leaves_type_and_name_untouched(self):
        """Re-importing an HTML export over an already-captured chat must not
        rewrite its type to 'unknown' or NULL its captured contact name."""
        self._write_html(SAMPLE_HTML_MESSAGE)
        db = _make_mock_db()
        db.get_chat_by_id.return_value = {"id": -1001234567890, "type": "private", "first_name": "Alice"}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir, chat_id_override=-1001234567890))

        chat_row = db.upsert_chat.call_args[0][0]
        self.assertNotIn("title", chat_row)
        self.assertNotIn("type", chat_row)
        self.assertNotIn("first_name", chat_row)

    def test_html_import_into_new_chat_sets_only_the_name(self):
        self._write_html(SAMPLE_HTML_MESSAGE)
        db = _make_mock_db()  # get_chat_by_id defaults to None: no row yet
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir, chat_id_override=-1001234567890))

        chat_row = db.upsert_chat.call_args[0][0]
        self.assertEqual(chat_row["title"], "Test Chat")
        self.assertNotIn("type", chat_row)

    def test_full_account_export_sets_honest_is_outgoing(self):
        self._write_export(
            {
                "personal_information": {"user_id": 42},
                "chats": {
                    "list": [
                        {
                            "name": "Chat",
                            "type": "personal_chat",
                            "id": 42,
                            "messages": [
                                {
                                    "id": 1,
                                    "type": "message",
                                    "date": "2024-01-15T10:00:00",
                                    "from": "Me",
                                    "from_id": "user42",
                                    "text": "outgoing",
                                },
                                {
                                    "id": 2,
                                    "type": "message",
                                    "date": "2024-01-15T10:01:00",
                                    "from": "Bob",
                                    "from_id": "user99",
                                    "text": "incoming",
                                },
                            ],
                        }
                    ]
                },
            }
        )
        db = _make_mock_db()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir))

        rows = db.insert_messages_batch.call_args[0][0]
        self.assertEqual(rows[0]["is_outgoing"], 1)
        self.assertEqual(rows[1]["is_outgoing"], 0)

    def test_html_import_leaves_is_outgoing_absent(self):
        """No owner info is available from a chat-scoped export: the key must
        be OMITTED (not defaulted to 0), so a --merge cannot clobber an
        is_outgoing the live sweep already determined correctly."""
        self._write_html(SAMPLE_HTML_MESSAGE)
        db = _make_mock_db()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir, chat_id_override=-1001234567890))

        rows = db.insert_messages_batch.call_args[0][0]
        self.assertNotIn("is_outgoing", rows[0])


# ---------------------------------------------------------------------------
# Media the archive already holds is not re-copied or re-inserted
# ---------------------------------------------------------------------------


class TestMediaAlreadyArchivedGuard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _write_export(self, data):
        with open(os.path.join(self.export_dir, "result.json"), "w") as f:
            json.dump(data, f)

    def _write_photo(self):
        photos_dir = os.path.join(self.export_dir, "photos")
        os.makedirs(photos_dir, exist_ok=True)
        with open(os.path.join(photos_dir, "photo_1.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

    def _write_single_photo_message(self):
        self._write_photo()
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "text": "",
                        "photo": "photos/photo_1.jpg",
                    },
                ],
            }
        )

    def test_skips_media_the_live_sweep_already_downloaded(self):
        self._write_single_photo_message()
        db = _make_mock_db()
        db.get_media_for_message.return_value = {
            "id": "42_1_photo",
            "message_id": 1,
            "chat_id": 42,
            "type": "photo",
            "file_path": "42/photo.jpg",
            "downloaded": 1,
        }
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_media"], 0)
        db.insert_media.assert_not_called()

    def test_a_replay_under_its_own_import_id_still_proceeds(self):
        """A row under OUR OWN import id is a resumed replay, not someone
        else's duplicate, and must still be written (idempotently)."""
        self._write_single_photo_message()
        db = _make_mock_db()
        db.get_media_for_message.return_value = {
            "id": "import_42_1",
            "message_id": 1,
            "chat_id": 42,
            "type": "photo",
            "file_path": "42/import_42_1_photo_1.jpg",
            "downloaded": 1,
        }
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_media"], 1)
        db.insert_media.assert_called_once()

    def test_a_not_yet_downloaded_sweep_row_does_not_block_the_import(self):
        """A sweep row that exists but was never downloaded (e.g. size- or
        filter-skipped) must not block importing the file the export has."""
        self._write_single_photo_message()
        db = _make_mock_db()
        db.get_media_for_message.return_value = {
            "id": "42_1_photo",
            "message_id": 1,
            "chat_id": 42,
            "type": "photo",
            "file_path": None,
            "downloaded": 0,
        }
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_media"], 1)
        db.insert_media.assert_called_once()


# ---------------------------------------------------------------------------
# upsert_user only refreshes what the live API has never seen
# ---------------------------------------------------------------------------


class TestUpsertUserOnlyWhenNotAlreadyCaptured(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _write_export(self, data):
        with open(os.path.join(self.export_dir, "result.json"), "w") as f:
            json.dump(data, f)

    def _export_with_sender(self):
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "from": "Alice",
                        "from_id": "user77",
                        "text": "hi",
                    },
                ],
            }
        )

    def test_a_sender_the_api_has_never_seen_is_created(self):
        self._export_with_sender()
        db = _make_mock_db()  # get_user_by_id defaults to None
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir))

        db.upsert_user.assert_called_once()

    def test_an_already_captured_sender_is_not_overwritten(self):
        """upsert_user refreshes username/last_name/phone/is_bot unconditionally
        on every call; an export only ever knows first_name, so writing over an
        already-captured user would NULL out identity fields the live API
        recorded."""
        self._export_with_sender()
        db = _make_mock_db()
        db.get_user_by_id.return_value = {"id": 77, "username": "alice_real", "first_name": "Alice"}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir))

        db.upsert_user.assert_not_called()


# ---------------------------------------------------------------------------
# Resumable JSON import (checkpoint per chat, no streaming)
# ---------------------------------------------------------------------------


class TestResumableJsonImport(unittest.TestCase):
    """Covers the resume/checkpoint half of the large-export durability fix.

    Memory-flat streaming of result.json is NOT implemented here (it would
    require the ijson dependency, which is out of scope - see the lane
    report); this covers the part that is portable without it: an
    interrupted multi-chat import can be retried without redoing completed
    chats or inflating their counters.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        os.makedirs(self.export_dir)
        self.result_json_path = os.path.join(self.export_dir, "result.json")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _write_export(self, data):
        with open(self.result_json_path, "w") as f:
            json.dump(data, f)

    def _stateful_db(self):
        """An AsyncMock db whose get_setting/set_setting behave like a real
        single-key store, so a marker written by one run() call is read back
        by the next - exercising the actual persistence contract instead of
        just asserting call shapes."""
        db = _make_mock_db()
        store: dict[str, str] = {}

        async def _get_setting(key):
            return store.get(key)

        async def _set_setting(key, value):
            store[key] = value

        db.get_setting.side_effect = _get_setting
        db.set_setting.side_effect = _set_setting
        return db

    def _two_chat_export(self):
        return {
            "chats": {
                "list": [
                    {
                        "name": "Chat A",
                        "type": "personal_chat",
                        "id": 1,
                        "messages": [
                            {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "text": "A1"},
                        ],
                    },
                    {
                        "name": "Chat B",
                        "type": "personal_chat",
                        "id": 2,
                        "messages": [
                            {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "text": "B1"},
                        ],
                    },
                ]
            }
        }

    def test_retry_after_interruption_skips_only_the_completed_chat(self):
        """A crash between chats leaves a marker naming the completed chat and
        the one the run was inside; a retry must skip the former and replay
        the latter, not redo everything or skip everything."""
        self._write_export(self._two_chat_export())
        db = self._stateful_db()

        importer1 = TelegramImporter(db, os.path.join(self.temp_dir, "media"))
        original_import_chat = importer1._import_chat
        call_count = {"n": 0}

        async def _flaky_import_chat(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("simulated crash mid-import")
            return await original_import_chat(*args, **kwargs)

        importer1._import_chat = _flaky_import_chat

        with self.assertRaises(RuntimeError):
            self._run(importer1.run(self.export_dir))

        marker = self._run(importer1._load_import_marker())
        self.assertEqual(marker["completed"], [1])
        self.assertEqual(marker["started"], 2)

        db.insert_messages_batch.reset_mock()
        importer2 = TelegramImporter(db, os.path.join(self.temp_dir, "media"))
        summary2 = self._run(importer2.run(self.export_dir, merge=True))

        self.assertEqual(summary2["chats_skipped"], 1)
        self.assertEqual(summary2["chats_imported"], 1)
        self.assertEqual(summary2["details"][0]["chat_name"], "Chat B")

        # A clean finish clears the marker: a later, unrelated import never
        # inherits this run's skip set.
        self.assertIsNone(self._run(importer2._load_import_marker()))

    def test_marker_cleared_after_clean_completion(self):
        self._write_export(self._two_chat_export())
        db = self._stateful_db()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir))

        marker = self._run(importer._load_import_marker())
        self.assertIsNone(marker)

    def test_dry_run_never_reads_or_writes_the_marker(self):
        self._write_export(self._two_chat_export())
        db = self._stateful_db()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir, dry_run=True))

        db.set_setting.assert_not_called()

    def test_a_marker_from_a_different_export_file_is_not_inherited(self):
        """A REPLACED export (different date range, newer pull) must not skip
        chats a stale marker from a DIFFERENT file already marked complete -
        that file's 'completed' chats may hold newer messages here."""
        self._write_export(self._two_chat_export())
        db = self._stateful_db()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        # Simulate a leftover marker from an unrelated export file.
        self._run(importer._save_import_marker("stale-fingerprint-from-another-file", {1, 2}, started=None))

        summary = self._run(importer.run(self.export_dir, merge=True))

        self.assertEqual(summary["chats_skipped"], 0)
        self.assertEqual(summary["chats_imported"], 2)

    def test_interrupted_chat_replay_bypasses_the_merge_guard(self):
        """The chat a previous run crashed inside must be resumable WITHOUT
        --merge: its partial rows are the importer's own earlier output, not
        someone else's data that a plain retry should refuse to touch."""
        from src.telegram_import import _export_fingerprint

        self._write_export(self._two_chat_export())
        db = self._stateful_db()
        # Chat 1 already has a partial row from the simulated crash; chat 2 is
        # untouched. A plain (non-merge) run must still succeed on both.
        db.get_chat_stats.side_effect = lambda chat_id: {"messages": 1 if chat_id == 1 else 0}

        fingerprint = _export_fingerprint(Path(self.result_json_path))
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))
        self._run(importer._save_import_marker(fingerprint, set(), started=1))

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["chats_imported"], 2)
        self.assertEqual(summary["chats_skipped"], 0)


if __name__ == "__main__":
    unittest.main()
