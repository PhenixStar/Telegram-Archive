"""Unit tests for transient media-download recovery (issue #203, fork port).

Covers ``TelegramBackup._download_media_to_path`` /
``_refresh_message_for_media`` and the ``media_errors`` classifier. These
methods harden the scheduled-backup download path: an expired file reference or
an unavailable/invalid media *location* triggers a bounded message re-fetch +
retry, and a partial file is never left behind on failure.

Written fresh for the fork's modular layout rather than merged from the base's
diverged ``test_telegram_backup_extended.py`` diff.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.errors import FileReferenceExpiredError, RPCError

from src.media_errors import MEDIA_LOCATION_ERROR_MESSAGES, is_media_location_error
from src.telegram_backup import TelegramBackup


def _location_error(message: str = "LOCATION_NOT_AVAILABLE") -> RPCError:
    """Build a bare RPCError carrying a server message, without running
    Telethon's __init__ (which expects a live request object)."""
    err = RPCError.__new__(RPCError)
    err.message = message
    err.code = 400
    return err


def _make_backup(*, timeout: int = 3600) -> TelegramBackup:
    backup = TelegramBackup.__new__(TelegramBackup)
    cfg = MagicMock()
    cfg.download_timeout_seconds = timeout
    backup.config = cfg
    backup.client = MagicMock()
    backup._parallel_downloader = None
    backup._parallel_download_disabled = False
    return backup


def _make_message(msg_id: int = 42) -> Any:
    msg = MagicMock()
    msg.id = msg_id
    return msg


class TestMediaErrorClassifier(TestCase):
    def test_location_not_available_matched_by_message(self) -> None:
        self.assertTrue(is_media_location_error(_location_error("LOCATION_NOT_AVAILABLE")))

    def test_location_invalid_matched_by_message(self) -> None:
        self.assertTrue(is_media_location_error(_location_error("LOCATION_INVALID")))

    def test_message_is_case_insensitive(self) -> None:
        self.assertTrue(is_media_location_error(_location_error("location_not_available")))

    def test_unrelated_rpc_error_not_matched(self) -> None:
        self.assertFalse(is_media_location_error(_location_error("SOMETHING_ELSE")))

    def test_non_rpc_error_not_matched(self) -> None:
        self.assertFalse(is_media_location_error(ValueError("nope")))
        self.assertFalse(is_media_location_error(TimeoutError()))

    def test_known_codes_are_upper_case(self) -> None:
        for code in MEDIA_LOCATION_ERROR_MESSAGES:
            self.assertEqual(code, code.upper())


class TestDownloadMediaToPath(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Neutralise real backoff sleeps so retry tests run instantly.
        self._backoff_patch = patch(
            "src.telegram_backup._media_retry_backoff_seconds", return_value=0
        )
        self._backoff_patch.start()
        self.addCleanup(self._backoff_patch.stop)

    async def test_success_first_attempt_returns_path(self) -> None:
        backup = _make_backup()
        backup._fetch_media_bytes = AsyncMock(return_value="/tmp/out")
        backup._refresh_message_for_media = AsyncMock()
        result = await backup._download_media_to_path(_make_message(), "/tmp/out", 1024, 7)
        self.assertEqual(result, "/tmp/out")
        backup._refresh_message_for_media.assert_not_called()

    async def test_location_error_then_refresh_then_success(self) -> None:
        backup = _make_backup()
        backup._fetch_media_bytes = AsyncMock(side_effect=[_location_error(), "/tmp/out"])
        backup._refresh_message_for_media = AsyncMock(return_value=_make_message(99))
        result = await backup._download_media_to_path(_make_message(), "/tmp/out", 1024, 7)
        self.assertEqual(result, "/tmp/out")
        backup._refresh_message_for_media.assert_awaited_once()

    async def test_expired_reference_retries_without_backoff(self) -> None:
        backup = _make_backup()
        backup._fetch_media_bytes = AsyncMock(
            side_effect=[FileReferenceExpiredError(request=None), "/tmp/out"]
        )
        backup._refresh_message_for_media = AsyncMock(return_value=_make_message(99))
        with patch("src.telegram_backup.asyncio.sleep") as sleep_mock:
            result = await backup._download_media_to_path(_make_message(), "/tmp/out", 1024, 7)
        self.assertEqual(result, "/tmp/out")
        # Expired-reference path refreshes but must NOT back off (immediate retry).
        sleep_mock.assert_not_called()

    async def test_location_error_persists_raises_after_max_attempts(self) -> None:
        backup = _make_backup()
        backup._fetch_media_bytes = AsyncMock(side_effect=_location_error())
        backup._refresh_message_for_media = AsyncMock(return_value=_make_message(99))
        with self.assertRaises(RPCError):
            await backup._download_media_to_path(_make_message(), "/tmp/out", 1024, 7)
        # 3 attempts by default.
        self.assertEqual(backup._fetch_media_bytes.await_count, 3)

    async def test_non_refreshable_rpc_error_raises_immediately(self) -> None:
        backup = _make_backup()
        backup._fetch_media_bytes = AsyncMock(side_effect=_location_error("SOMETHING_ELSE"))
        backup._refresh_message_for_media = AsyncMock()
        with self.assertRaises(RPCError):
            await backup._download_media_to_path(_make_message(), "/tmp/out", 1024, 7)
        # Not refreshable → no refresh, only one download attempt.
        self.assertEqual(backup._fetch_media_bytes.await_count, 1)
        backup._refresh_message_for_media.assert_not_called()

    async def test_timeout_retries_then_raises(self) -> None:
        backup = _make_backup()
        backup._fetch_media_bytes = AsyncMock(side_effect=TimeoutError())
        backup._refresh_message_for_media = AsyncMock()
        with self.assertRaises(TimeoutError):
            await backup._download_media_to_path(_make_message(), "/tmp/out", 1024, 7)
        self.assertEqual(backup._fetch_media_bytes.await_count, 3)

    async def test_partial_file_removed_on_failure(self) -> None:
        backup = _make_backup()
        with tempfile.TemporaryDirectory() as d:
            tmp_path = os.path.join(d, "media.part")

            async def _fail(*_a: Any, **_k: Any) -> None:
                # Simulate a partial write, then fail.
                with open(tmp_path, "wb") as fh:
                    fh.write(b"partial")
                raise TimeoutError()

            backup._fetch_media_bytes = AsyncMock(side_effect=_fail)
            backup._refresh_message_for_media = AsyncMock()
            with self.assertRaises(TimeoutError):
                await backup._download_media_to_path(_make_message(), tmp_path, 1024, 7)
            self.assertFalse(os.path.exists(tmp_path), "partial file must be cleaned up")


class TestRefreshMessageForMedia(IsolatedAsyncioTestCase):
    async def test_returns_fresh_message(self) -> None:
        backup = _make_backup()
        fresh = _make_message(99)
        backup.client.get_messages = AsyncMock(return_value=[fresh])
        result = await backup._refresh_message_for_media(7, _make_message())
        self.assertIs(result, fresh)

    async def test_returns_none_on_empty(self) -> None:
        backup = _make_backup()
        backup.client.get_messages = AsyncMock(return_value=[])
        result = await backup._refresh_message_for_media(7, _make_message())
        self.assertIsNone(result)

    async def test_returns_none_on_deleted_message(self) -> None:
        backup = _make_backup()
        backup.client.get_messages = AsyncMock(return_value=[None])
        result = await backup._refresh_message_for_media(7, _make_message())
        self.assertIsNone(result)

    async def test_returns_none_on_transient_error(self) -> None:
        backup = _make_backup()
        backup.client.get_messages = AsyncMock(side_effect=ConnectionError("boom"))
        result = await backup._refresh_message_for_media(7, _make_message())
        self.assertIsNone(result)
