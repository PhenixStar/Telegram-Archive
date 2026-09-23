"""Tests for the opt-in DOWNLOAD_MEDIA_TYPES / DOWNLOAD_DOCUMENT_MIME_TYPES
download filters (semantic port of upstream #463).

Covers:
- Config parsing, validation, and the two predicates it exposes
- The default-off path is byte-for-byte unchanged (no filter configured)
- backup_media.media_download_allowed, the shared gate predicate
- _process_media records filtered media the same way it records over-size
  media (metadata only, downloaded=False), instead of dropping the row
- TelegramListener._download_media declines the same way (no media row)
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.backup_media import media_download_allowed
from src.config import Config
from src.telegram_backup import TelegramBackup


class TestConfigMediaTypeParsing(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _config(self, **extra_env):
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir, **extra_env}
        with patch.dict(os.environ, env_vars, clear=True):
            return Config()

    def test_defaults_download_every_type(self):
        """No env vars set -> empty filters, every media type/document allowed."""
        config = self._config()
        self.assertEqual(config.download_media_types, set())
        self.assertEqual(config.download_document_mime_types, set())
        self.assertFalse(config.has_folder_include_filters)  # unrelated flag stays off too
        self.assertTrue(config.should_download_media_type("photo"))
        self.assertTrue(config.should_download_media_type("document"))
        self.assertTrue(config.should_download_media_type(None))
        self.assertTrue(config.document_mime_allowed("application/octet-stream"))

    def test_valid_media_types_parsed_lowercased(self):
        config = self._config(DOWNLOAD_MEDIA_TYPES="Photo, VIDEO ,voice")
        self.assertEqual(config.download_media_types, {"photo", "video", "voice"})
        self.assertTrue(config.should_download_media_type("photo"))
        self.assertFalse(config.should_download_media_type("document"))
        self.assertFalse(config.should_download_media_type(None))

    def test_invalid_media_type_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._config(DOWNLOAD_MEDIA_TYPES="photo,not_a_type")
        self.assertIn("not_a_type", str(ctx.exception))

    def test_valid_document_mime_types_parsed_and_normalized(self):
        config = self._config(DOWNLOAD_DOCUMENT_MIME_TYPES="Application/PDF; charset=utf-8, text/plain")
        self.assertEqual(config.download_document_mime_types, {"application/pdf", "text/plain"})
        self.assertTrue(config.document_mime_allowed("application/pdf"))
        self.assertTrue(config.document_mime_allowed("APPLICATION/PDF"))
        self.assertFalse(config.document_mime_allowed("image/png"))

    def test_document_mime_wildcard_rejected(self):
        with self.assertRaises(ValueError):
            self._config(DOWNLOAD_DOCUMENT_MIME_TYPES="image/*")

    def test_document_mime_bare_extension_rejected(self):
        with self.assertRaises(ValueError):
            self._config(DOWNLOAD_DOCUMENT_MIME_TYPES="pdf")

    def test_document_mime_allows_via_extension_fallback(self):
        """A mislabeled document (generic MIME) still passes via its filename extension."""
        config = self._config(DOWNLOAD_DOCUMENT_MIME_TYPES="application/pdf")
        self.assertTrue(config.document_mime_allowed("application/octet-stream", "report.pdf"))
        self.assertFalse(config.document_mime_allowed("application/octet-stream", "report.exe"))
        self.assertFalse(config.document_mime_allowed("application/octet-stream", None))

    def test_document_mime_filter_has_no_opinion_without_document_type_filter(self):
        """DOWNLOAD_DOCUMENT_MIME_TYPES alone (no DOWNLOAD_MEDIA_TYPES) still narrows documents."""
        config = self._config(DOWNLOAD_DOCUMENT_MIME_TYPES="application/pdf")
        self.assertTrue(config.should_download_media_type("document"))  # type-level filter is empty
        self.assertFalse(config.document_mime_allowed("image/png"))  # MIME-level filter still applies


class TestMediaDownloadAllowedPredicate(unittest.TestCase):
    """Direct tests of the shared backup_media.media_download_allowed gate."""

    def _document_media(self, mime_type="application/pdf", file_name="report.pdf"):
        attr = MagicMock()
        attr.file_name = file_name
        document = MagicMock()
        document.mime_type = mime_type
        document.attributes = [attr]
        media = MagicMock()
        media.document = document
        return media

    def test_default_off_config_allows_everything(self):
        """A real Config with no filters set never blocks anything (default-off contract)."""
        temp_dir = tempfile.mkdtemp()
        try:
            with patch.dict(os.environ, {"CHAT_TYPES": "private", "BACKUP_PATH": temp_dir}, clear=True):
                config = Config()
            self.assertTrue(media_download_allowed(config, MagicMock(), "photo"))
            self.assertTrue(media_download_allowed(config, self._document_media(), "document"))
            self.assertTrue(media_download_allowed(config, MagicMock(), "contact"))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_metadata_only_types_always_pass(self):
        """contact/geo/poll have no file behind them; the filter has no opinion on them."""
        config = MagicMock()
        config.should_download_media_type.return_value = False  # would otherwise block everything
        for media_type in ("contact", "geo", "poll"):
            self.assertTrue(media_download_allowed(config, MagicMock(), media_type))

    def test_type_not_in_whitelist_is_blocked(self):
        config = MagicMock()
        config.should_download_media_type.side_effect = lambda t: t == "photo"
        self.assertFalse(media_download_allowed(config, MagicMock(), "video"))
        self.assertTrue(media_download_allowed(config, MagicMock(), "photo"))

    def test_document_mime_whitelist_blocks_non_matching_document(self):
        config = MagicMock()
        config.should_download_media_type.return_value = True
        config.download_document_mime_types = {"application/pdf"}
        config.document_mime_allowed.return_value = False

        media = self._document_media(mime_type="image/png", file_name="photo.png")
        self.assertFalse(media_download_allowed(config, media, "document"))
        config.document_mime_allowed.assert_called_once_with("image/png", "photo.png")

    def test_document_mime_whitelist_allows_matching_document(self):
        config = MagicMock()
        config.should_download_media_type.return_value = True
        config.download_document_mime_types = {"application/pdf"}
        config.document_mime_allowed.return_value = True

        media = self._document_media(mime_type="application/pdf", file_name="report.pdf")
        self.assertTrue(media_download_allowed(config, media, "document"))

    def test_document_without_mime_narrowing_is_not_gated_by_mime(self):
        """Empty download_document_mime_types skips the MIME sub-gate entirely."""
        config = MagicMock()
        config.should_download_media_type.return_value = True
        config.download_document_mime_types = set()

        media = self._document_media(mime_type="anything/whatever", file_name="x.bin")
        self.assertTrue(media_download_allowed(config, media, "document"))
        config.document_mime_allowed.assert_not_called()


class TestProcessMediaRecordsFilteredMedia(unittest.TestCase):
    """_process_media must record a filtered file exactly like an over-size one."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.media_path = os.path.join(self.temp_dir, "media")
        os.makedirs(self.media_path)

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.config = MagicMock()
        self.backup.config.media_path = self.media_path
        self.backup.config.deduplicate_media = True
        self.backup.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        self.backup.config.should_download_media_type = MagicMock(return_value=False)
        self.backup.config.download_document_mime_types = set()
        self.backup.client = AsyncMock()
        self.backup.db = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_message(self, msg_id=1, file_id="abc123"):
        msg = MagicMock()
        msg.id = msg_id
        msg.media = MagicMock()
        msg.media.photo = MagicMock()
        msg.media.photo.id = file_id
        msg.media.document = None
        return msg

    def test_filtered_media_recorded_like_oversize_not_downloaded(self):
        """Filtered media returns a metadata-only dict; download_media is never called."""
        self.backup._get_media_type = MagicMock(return_value="photo")
        self.backup._get_media_size = MagicMock(return_value=512)
        msg = self._make_message(msg_id=42, file_id="xyz")

        result = self._run(self.backup._process_media(msg, 900))

        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "photo")
        self.assertEqual(result["message_id"], 42)
        self.assertEqual(result["chat_id"], 900)
        self.assertEqual(result["file_size"], 512)
        self.assertFalse(result["downloaded"])
        # Same shape as the over-size branch: no file_name/file_path key.
        self.assertNotIn("file_path", result)
        self.backup.client.download_media.assert_not_awaited()

    def test_unfiltered_media_still_downloads(self):
        """Sanity check: when the predicate allows the type, download proceeds as before."""
        self.backup.config.should_download_media_type = MagicMock(return_value=True)
        self.backup.client.download_media = AsyncMock(return_value=os.path.join(self.media_path, "won.jpg"))
        self.backup.db.find_media_by_content_hash = AsyncMock(return_value=None)
        self.backup._get_media_type = MagicMock(return_value="photo")
        self.backup._get_media_filename = MagicMock(return_value="won.jpg")
        self.backup._get_media_size = MagicMock(return_value=512)

        msg = self._make_message(msg_id=43, file_id="won")
        result = self._run(self.backup._process_media(msg, 901))

        self.assertIsNotNone(result)
        self.assertTrue(result["downloaded"])


class TestListenerDownloadMediaFilter(unittest.TestCase):
    """TelegramListener._download_media declines a filtered type without a media row."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_filtered_type_returns_none_without_downloading(self):
        from src.listener import TelegramListener

        listener = TelegramListener.__new__(TelegramListener)
        listener.config = MagicMock()
        listener.config.should_download_media_type = MagicMock(return_value=False)
        listener.config.download_document_mime_types = set()
        listener.client = AsyncMock()

        message = MagicMock()
        message.media = MagicMock()
        message.media.photo = MagicMock()
        message.media.document = None
        listener._get_media_type = MagicMock(return_value="photo")

        result = self._run(listener._download_media(message, 700))

        self.assertIsNone(result)
        listener.client.download_media.assert_not_awaited()

    def test_metadata_only_type_still_returns_none_early(self):
        """contact/geo/poll short-circuit before the filter is even consulted."""
        from src.listener import TelegramListener

        listener = TelegramListener.__new__(TelegramListener)
        listener.config = MagicMock()
        listener.config.should_download_media_type = MagicMock(side_effect=AssertionError("should not be called"))
        listener._get_media_type = MagicMock(return_value="contact")

        result = self._run(listener._download_media(MagicMock(), 700))

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
