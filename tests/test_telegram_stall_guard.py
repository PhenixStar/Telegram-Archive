"""Tests for bounded Telegram waits: a dead connection must never hang a backup run."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.telegram_stall_guard import iter_with_stall_timeout, with_call_timeout


class _StallingIter:
    """Async iterator that yields ``items`` then blocks forever, like a lost Telethon request."""

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._items:
            return self._items.pop(0)
        await asyncio.Event().wait()


class _FiniteIter:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class TestWithCallTimeout:
    async def test_returns_result_within_timeout(self):
        async def ok():
            return 42

        assert await with_call_timeout(ok(), 1) == 42

    async def test_raises_timeout_when_call_never_resolves(self):
        with pytest.raises(TimeoutError):
            await with_call_timeout(asyncio.Event().wait(), 0.05)

    async def test_none_timeout_is_unbounded(self):
        async def slow():
            await asyncio.sleep(0.05)
            return "done"

        assert await with_call_timeout(slow(), None) == "done"


class TestIterWithStallTimeout:
    async def test_yields_all_items_and_stops(self):
        assert [x async for x in iter_with_stall_timeout(_FiniteIter([1, 2, 3]), 1)] == [1, 2, 3]

    async def test_raises_after_items_when_fetch_stalls(self):
        seen = []
        with pytest.raises(TimeoutError):
            async for item in iter_with_stall_timeout(_StallingIter([1, 2]), 0.05):
                seen.append(item)
        assert seen == [1, 2]

    async def test_consumer_work_between_items_is_not_timed(self):
        seen = []
        async for item in iter_with_stall_timeout(_FiniteIter([1, 2]), 0.05):
            await asyncio.sleep(0.1)  # e.g. a media download longer than the stall timeout
            seen.append(item)
        assert seen == [1, 2]


class TestFloodRetryHelpersAreBounded:
    async def test_call_with_flood_retry_times_out_hung_call(self):
        from src.telegram_backup import call_with_flood_retry

        async def hung():
            await asyncio.Event().wait()

        with pytest.raises(TimeoutError):
            await call_with_flood_retry(hung, call_timeout=0.05)

    async def test_call_with_flood_retry_opt_out_for_downloads(self):
        from src.telegram_backup import call_with_flood_retry

        async def slow_download(path):
            await asyncio.sleep(0.1)
            return path

        assert await call_with_flood_retry(slow_download, "/tmp/x", call_timeout=None) == "/tmp/x"

    async def test_call_with_flood_retry_does_not_forward_call_timeout(self):
        from src.telegram_backup import call_with_flood_retry

        async def fn(**kwargs):
            return kwargs

        assert await call_with_flood_retry(fn, limit=5, call_timeout=1) == {"limit": 5}

    async def test_iter_messages_with_flood_retry_times_out_stalled_page(self):
        from src import telegram_backup

        client = MagicMock()
        client.iter_messages = MagicMock(return_value=_StallingIter([SimpleNamespace(id=1)]))

        async def fast_stall_guard(iterable):
            async for item in iter_with_stall_timeout(iterable, 0.05):
                yield item

        seen = []
        with (
            patch.object(telegram_backup, "iter_with_stall_timeout", fast_stall_guard),
            pytest.raises(TimeoutError),
        ):
            async for msg in telegram_backup.iter_messages_with_flood_retry(client, "chat", reverse=True):
                seen.append(msg.id)
        assert seen == [1]


class TestSchedulerJobWatchdog:
    @pytest.fixture
    def scheduler(self):
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = False
            return BackupScheduler(config)

    async def test_hung_job_is_cancelled_and_lock_released(self, scheduler, monkeypatch, caplog):
        monkeypatch.setenv("BACKUP_JOB_TIMEOUT_HOURS", str(0.05 / 3600))

        async def hang():
            await asyncio.Event().wait()

        scheduler._run_backup_job_body = hang
        with caplog.at_level(logging.ERROR, logger="src.scheduler"):
            await scheduler._run_backup_job()

        assert not scheduler._backup_lock.locked()
        assert scheduler._backup_started_at is None
        assert "BACKUP_JOB_TIMEOUT_HOURS" in caplog.text

    def test_timeout_zero_disables_watchdog(self, monkeypatch):
        from src.scheduler import _backup_job_timeout_seconds

        monkeypatch.setenv("BACKUP_JOB_TIMEOUT_HOURS", "0")
        assert _backup_job_timeout_seconds() is None
        monkeypatch.setenv("BACKUP_JOB_TIMEOUT_HOURS", "bogus")
        assert _backup_job_timeout_seconds() == 12 * 3600

    def test_skipped_run_logs_error_with_running_time(self, scheduler, caplog):
        from datetime import datetime, timedelta

        scheduler._backup_started_at = datetime.now() - timedelta(hours=3)
        with caplog.at_level(logging.ERROR, logger="src.scheduler"):
            scheduler._on_job_max_instances(SimpleNamespace(job_id="telegram_backup"))
        assert "still running" in caplog.text
        assert "running for 3:" in caplog.text

    def test_skip_listener_ignores_other_jobs(self, scheduler, caplog):
        with caplog.at_level(logging.ERROR, logger="src.scheduler"):
            scheduler._on_job_max_instances(SimpleNamespace(job_id="something_else"))
        assert caplog.text == ""
