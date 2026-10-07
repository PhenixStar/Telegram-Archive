"""A download that stopped short is never kept, and a missing media volume changes nothing.

Covers the scheduled backup (``_download_media_to_path``, ``_process_media``,
``_verify_and_redownload_media``) and the real-time listener
(``_download_media``) on real files in a temporary media root.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from src.media_integrity import ShortDownloadError
from src.telegram_backup import TelegramBackup

DECLARED = 1000


def _document_message(msg_id: int = 42, size: int = DECLARED, doc_id: int = 777):
    """A message whose document declares ``size`` bytes."""
    msg = MagicMock()
    msg.id = msg_id
    msg.media = SimpleNamespace(
        document=SimpleNamespace(id=doc_id, size=size, mime_type="video/mp4", attributes=[])
    )
    msg.date = MagicMock()
    msg.date.strftime = MagicMock(return_value="20260101_120000")
    return msg


def _writer(*sizes: int):
    """A ``_fetch_media_bytes`` double that writes ``sizes[n]`` bytes on call n."""
    calls = iter(sizes)

    async def fetch(message, path, file_size):
        with open(path, "wb") as f:
            f.write(b"v" * next(calls))
        return path

    return AsyncMock(side_effect=fetch)


class _MediaRootCase(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.media_path = os.path.join(self.temp_dir, "media")
        os.makedirs(self.media_path)
        self.addCleanup(shutil.rmtree, self.temp_dir, True)
        backoff = patch("src.telegram_backup._media_retry_backoff_seconds", return_value=0)
        backoff.start()
        self.addCleanup(backoff.stop)

    def _backup(self) -> TelegramBackup:
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.config = MagicMock()
        backup.config.media_path = self.media_path
        backup.config.download_timeout_seconds = 3600
        backup.config.deduplicate_media = True
        backup.config.skip_media_chat_ids = set()
        backup.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        backup.config.download_document_mime_types = set()
        backup.client = AsyncMock()
        backup.db = AsyncMock()
        backup._connection = None
        backup._parallel_downloader = None
        backup._parallel_download_disabled = False
        backup._get_media_type = MagicMock(return_value="video")
        backup._get_media_filename = MagicMock(return_value="777.mp4")
        return backup


class TestShortDownloadRefused(_MediaRootCase):
    async def test_short_attempt_is_retried_and_complete_one_kept(self) -> None:
        backup = self._backup()
        backup._fetch_media_bytes = _writer(400, DECLARED)
        target = os.path.join(self.media_path, "out.mp4")

        result = await backup._download_media_to_path(_document_message(), target, DECLARED, 7)

        self.assertEqual(result, target)
        self.assertEqual(os.path.getsize(target), DECLARED)
        self.assertEqual(backup._fetch_media_bytes.await_count, 2)

    async def test_short_on_every_attempt_raises_and_leaves_no_file(self) -> None:
        backup = self._backup()
        backup._fetch_media_bytes = _writer(400, 400, 400)
        target = os.path.join(self.media_path, "out.mp4")

        with self.assertRaises(ShortDownloadError):
            await backup._download_media_to_path(_document_message(), target, DECLARED, 7)

        self.assertFalse(os.path.exists(target))
        self.assertEqual(backup._fetch_media_bytes.await_count, 3)

    async def test_file_written_under_another_name_is_removed_when_short(self) -> None:
        # Telethon can append an extension and report that path instead.
        backup = self._backup()
        target = os.path.join(self.media_path, "out")
        other = target + ".mp4"

        async def fetch(message, path, file_size):
            with open(other, "wb") as f:
                f.write(b"v" * 10)
            return other

        backup._fetch_media_bytes = AsyncMock(side_effect=fetch)
        with self.assertRaises(ShortDownloadError):
            await backup._download_media_to_path(_document_message(), target, DECLARED, 7)
        self.assertFalse(os.path.exists(other))

    async def test_process_media_records_a_short_download_as_not_downloaded(self) -> None:
        backup = self._backup()
        backup._fetch_media_bytes = _writer(400, 400, 400)

        result = await backup._process_media(_document_message(), 200)

        self.assertFalse(result["downloaded"])
        self.assertNotIn("file_path", result)
        chat_link = os.path.join(self.media_path, "200", "777.mp4")
        self.assertFalse(os.path.lexists(chat_link))
        self.assertFalse(os.path.exists(os.path.join(self.media_path, "_shared", "777.mp4")))

    async def test_process_media_keeps_a_complete_download(self) -> None:
        backup = self._backup()
        backup._fetch_media_bytes = _writer(DECLARED)

        result = await backup._process_media(_document_message(), 200)

        self.assertTrue(result["downloaded"])
        self.assertEqual(result["file_size"], DECLARED)


class TestUnmountedMediaRoot(_MediaRootCase):
    async def test_archived_row_is_not_downloaded_or_unmarked_when_root_is_empty(self) -> None:
        backup = self._backup()  # the media root exists but is empty: not mounted
        backup.db.get_media_for_message = AsyncMock(return_value={"id": "200_42_video", "downloaded": 1})
        backup._fetch_media_bytes = _writer(DECLARED)

        result = await backup._process_media(_document_message(), 200)

        self.assertIsNone(result)  # nothing is written back to the row
        backup._fetch_media_bytes.assert_not_awaited()
        self.assertEqual(os.listdir(self.media_path), [])  # no folder created beside the volume

    async def test_archived_row_is_kept_when_its_chat_folder_is_not_there(self) -> None:
        os.makedirs(os.path.join(self.media_path, "999"))  # root visible, other chat only
        backup = self._backup()
        backup.db.get_media_for_message = AsyncMock(return_value={"id": "200_42_video", "downloaded": True})
        backup._fetch_media_bytes = _writer(DECLARED)

        self.assertIsNone(await backup._process_media(_document_message(), 200))
        backup._fetch_media_bytes.assert_not_awaited()

    async def test_new_media_still_downloads_into_an_empty_root(self) -> None:
        # A first run starts from an empty media folder: no archived row, so the
        # download goes ahead as before.
        backup = self._backup()
        backup.db.get_media_for_message = AsyncMock(return_value=None)
        backup._fetch_media_bytes = _writer(DECLARED)

        result = await backup._process_media(_document_message(), 200)

        self.assertTrue(result["downloaded"])

    async def test_missing_file_in_a_visible_chat_folder_is_downloaded_without_a_lookup(self) -> None:
        os.makedirs(os.path.join(self.media_path, "200"))
        backup = self._backup()
        backup.db.get_media_for_message = AsyncMock(return_value={"downloaded": 1})
        backup._fetch_media_bytes = _writer(DECLARED)

        result = await backup._process_media(_document_message(), 200)

        self.assertTrue(result["downloaded"])
        backup.db.get_media_for_message.assert_not_awaited()

    async def test_verify_media_changes_nothing_when_root_is_empty(self) -> None:
        backup = self._backup()
        backup.db.get_media_for_verification = AsyncMock(
            return_value=[{"file_path": os.path.join(self.media_path, "200", "777.mp4"), "chat_id": 200, "message_id": 42}]
        )
        backup._process_media = AsyncMock()

        await backup._verify_and_redownload_media()

        backup.db.get_media_for_verification.assert_not_awaited()
        backup._process_media.assert_not_awaited()
        backup.db.insert_media.assert_not_awaited()

    async def test_verify_media_still_runs_on_a_visible_root(self) -> None:
        os.makedirs(os.path.join(self.media_path, "200"))
        backup = self._backup()
        backup.db.get_media_for_verification = AsyncMock(return_value=[])

        await backup._verify_and_redownload_media()

        backup.db.get_media_for_verification.assert_awaited_once()


class TestListenerRefusesShortDownload(_MediaRootCase):
    def _listener(self, *, dedup: bool):
        from src.listener import TelegramListener

        listener = TelegramListener.__new__(TelegramListener)
        listener.config = MagicMock()
        listener.config.media_path = self.media_path
        listener.config.deduplicate_media = dedup
        listener.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        listener.config.download_document_mime_types = set()
        listener.client = AsyncMock()
        listener.db = AsyncMock()
        listener.db.find_media_by_content_hash = AsyncMock(return_value=None)
        listener._get_media_type = MagicMock(return_value="video")
        listener._get_media_filename = MagicMock(return_value="777.mp4")
        return listener

    @staticmethod
    def _short_download(size: int):
        async def download(message, path, *args, **kwargs):
            with open(path, "wb") as f:
                f.write(b"v" * size)
            return path

        return AsyncMock(side_effect=download)

    def _files(self) -> list[str]:
        return [os.path.join(d, f) for d, _, files in os.walk(self.media_path) for f in files]

    async def test_dedup_path_returns_nothing_and_leaves_no_file(self) -> None:
        listener = self._listener(dedup=True)
        listener.client.download_media = self._short_download(400)

        self.assertIsNone(await listener._download_media(_document_message(), 200))
        self.assertEqual(self._files(), [])

    async def test_direct_path_returns_nothing_and_leaves_no_file(self) -> None:
        listener = self._listener(dedup=False)
        listener.client.download_media = self._short_download(400)

        self.assertIsNone(await listener._download_media(_document_message(), 200))
        self.assertEqual(self._files(), [])

    async def test_complete_download_is_kept(self) -> None:
        listener = self._listener(dedup=False)
        listener.client.download_media = self._short_download(DECLARED)

        result = await listener._download_media(_document_message(), 200)

        self.assertIsNotNone(result)
        self.assertEqual(os.path.getsize(os.path.join(self.media_path, "200", "777.mp4")), DECLARED)
