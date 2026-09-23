"""A voice note that can never be transcribed must leave the pending queue.

The pending query returns every voice note whose ocr_text is NULL, newest first,
so without a permanent-failure marker one unprocessable file is re-sent to the
transcription service on every poll, forever.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.transcription_worker import (
    _MAX_TRANSCRIPTION_ATTEMPTS,
    _TRANSCRIPTION_FAILED_SENTINEL,
    TranscriptionWorker,
)


def _worker(tmp_path):
    db = SimpleNamespace(update_ocr_text=AsyncMock())
    config = SimpleNamespace(backup_path=str(tmp_path), ocr_poll_interval=60)
    return TranscriptionWorker(db, config), db


def _item(path: str) -> dict:
    return {"chat_id": 126, "message_id": 55, "file_path": path}


class TestPermanentFailures:
    @pytest.mark.asyncio
    async def test_zero_byte_file_is_marked_after_the_attempt_limit(self, tmp_path):
        # An interrupted download leaves an empty file the DB still calls complete.
        empty = tmp_path / "voice.ogg"
        empty.touch()
        worker, db = _worker(tmp_path)
        client = AsyncMock()

        for _ in range(_MAX_TRANSCRIPTION_ATTEMPTS):
            assert await worker._process_one(client, _item(str(empty)), "http://api") is False

        client.post.assert_not_awaited()
        db.update_ocr_text.assert_awaited_once_with(126, 55, _TRANSCRIPTION_FAILED_SENTINEL)

    @pytest.mark.asyncio
    async def test_a_rejected_audio_file_is_marked(self, tmp_path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"not really audio")
        worker, db = _worker(tmp_path)

        response = httpx.Response(400, request=httpx.Request("POST", "http://api/transcribe/file"))
        client = AsyncMock()
        client.post.return_value = response

        for _ in range(_MAX_TRANSCRIPTION_ATTEMPTS):
            assert await worker._process_one(client, _item(str(audio)), "http://api") is False

        db.update_ocr_text.assert_awaited_once_with(126, 55, _TRANSCRIPTION_FAILED_SENTINEL)

    @pytest.mark.asyncio
    async def test_a_service_outage_keeps_retrying(self, tmp_path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"not really audio")
        worker, db = _worker(tmp_path)

        response = httpx.Response(503, request=httpx.Request("POST", "http://api/transcribe/file"))
        client = AsyncMock()
        client.post.return_value = response

        for _ in range(_MAX_TRANSCRIPTION_ATTEMPTS + 2):
            assert await worker._process_one(client, _item(str(audio)), "http://api") is False

        # 5xx is transient: the row must stay in the queue for a later retry.
        db.update_ocr_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rate_limiting_keeps_retrying(self, tmp_path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"not really audio")
        worker, db = _worker(tmp_path)

        response = httpx.Response(429, request=httpx.Request("POST", "http://api/transcribe/file"))
        client = AsyncMock()
        client.post.return_value = response

        for _ in range(_MAX_TRANSCRIPTION_ATTEMPTS + 2):
            assert await worker._process_one(client, _item(str(audio)), "http://api") is False

        db.update_ocr_text.assert_not_awaited()


class TestSentinelIsNotShown:
    def test_failure_sentinels_are_hidden_from_the_viewer(self):
        from src.db.adapter_messages import _visible_ocr_text

        assert _visible_ocr_text("[transcription_failed]") is None
        assert _visible_ocr_text("[ocr_failed]") is None
        assert _visible_ocr_text("[Voice 12s, en] hello") == "[Voice 12s, en] hello"
        assert _visible_ocr_text(None) is None
