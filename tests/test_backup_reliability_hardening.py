"""Backup and viewer hardening ported from upstream fixes.

Covers: deletion/edit sync pairing by id, unusable document references, ending a
run while Telegram is unreachable, the scheduler healthcheck heartbeat and misfire
grace, the monotonic sync cursor, and download-only serving of scriptable media.
"""

import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from telethon.tl.types import DocumentEmpty, MessageMediaDocument

from src.telegram_backup import TelegramBackup, TelegramUnreachableError

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _bare_backup(**attrs):
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.client = MagicMock()
    backup._connection = None
    backup.config = SimpleNamespace(deletion_mode="hard")
    for key, value in attrs.items():
        setattr(backup, key, value)
    return backup


# ---------------------------------------------------------------------------
# Deletion/edit sync pairs Telegram's response by id
# ---------------------------------------------------------------------------


class TestSyncDeletionsPairsById:
    def _backup(self, local_ids, remote):
        db = MagicMock()
        db.get_messages_sync_data = AsyncMock(return_value={mid: None for mid in local_ids})
        db.delete_message = AsyncMock()
        db.mark_message_deleted = AsyncMock()
        db.update_message_text = AsyncMock(return_value="applied")
        backup = _bare_backup(db=db)
        backup.client.get_messages = AsyncMock(return_value=remote)
        return backup, db

    async def test_aligned_none_is_deleted(self):
        backup, db = self._backup([1, 2, 3], [SimpleNamespace(id=1, edit_date=None), None, SimpleNamespace(id=3, edit_date=None)])
        await backup._sync_deletions_and_edits(-100, "entity")
        db.delete_message.assert_awaited_once_with(-100, 2)

    async def test_omitted_id_does_not_shift_or_delete(self):
        # Telegram omitted id 2 entirely: a positional zip would pair 2 with message 3
        # and treat id 3 as deleted.
        edited = datetime(2026, 9, 1, 12, 0)
        remote = [SimpleNamespace(id=1, edit_date=None), SimpleNamespace(id=3, edit_date=edited, message="new three")]
        backup, db = self._backup([1, 2, 3], remote)

        await backup._sync_deletions_and_edits(-100, "entity")

        db.delete_message.assert_not_awaited()
        db.mark_message_deleted.assert_not_awaited()
        db.update_message_text.assert_awaited_once_with(-100, 3, "new three", edited)


# ---------------------------------------------------------------------------
# Unusable document references are treated as missing
# ---------------------------------------------------------------------------


class TestUnusableDocumentReference:
    def test_backup_media_type_is_none_for_missing_document(self):
        backup = _bare_backup()
        assert backup._get_media_type(MessageMediaDocument(document=None)) is None

    def test_backup_media_type_is_none_for_document_empty(self):
        backup = _bare_backup()
        assert backup._get_media_type(MessageMediaDocument(document=DocumentEmpty(id=1))) is None

    def test_backup_filename_tolerates_document_empty(self):
        backup = _bare_backup()
        message = SimpleNamespace(id=5, media=MessageMediaDocument(document=DocumentEmpty(id=1)))
        assert backup._get_media_filename(message, "document", "fid")

    def test_listener_media_type_is_none_for_document_empty(self):
        from src.listener import TelegramListener

        listener = TelegramListener.__new__(TelegramListener)
        assert listener._get_media_type(MessageMediaDocument(document=DocumentEmpty(id=1))) is None


# ---------------------------------------------------------------------------
# A run ends early while Telegram stays unreachable
# ---------------------------------------------------------------------------


class TestHealOrEndRun:
    async def test_heal_reports_success_without_connection(self):
        assert await _bare_backup()._heal_connection() is True

    async def test_heal_reports_failure(self):
        connection = MagicMock()
        connection.ensure_connected = AsyncMock(side_effect=ConnectionError("down"))
        assert await _bare_backup(_connection=connection)._heal_connection() is False

    async def test_end_run_raises_when_heal_fails(self):
        connection = MagicMock()
        connection.ensure_connected = AsyncMock(side_effect=ConnectionError("down"))
        with pytest.raises(TelegramUnreachableError):
            await _bare_backup(_connection=connection)._heal_or_end_run()

    async def test_end_run_continues_when_heal_succeeds(self):
        connection = MagicMock()
        connection.ensure_connected = AsyncMock()
        connection.client = MagicMock()
        await _bare_backup(_connection=connection)._heal_or_end_run()

    async def test_gap_fill_stops_after_failed_heal(self):
        connection = MagicMock()
        connection.ensure_connected = AsyncMock(side_effect=ConnectionError("down"))
        db = MagicMock()
        db.get_chats_with_messages = AsyncMock(return_value=[-1001, -1002])
        db.detect_message_gaps = AsyncMock(return_value=[(1, 5, 3)])
        backup = _bare_backup(_connection=connection, db=db)
        backup.config = SimpleNamespace(gap_threshold=1, deletion_mode="hard")
        backup.client.get_entity = AsyncMock(side_effect=ConnectionError("Cannot send requests while disconnected"))

        with pytest.raises(TelegramUnreachableError):
            await backup._fill_gaps()

        assert backup.client.get_entity.await_count == 1  # did not grind on to the second chat


# ---------------------------------------------------------------------------
# Scheduler: heartbeat for the healthcheck, misfire grace
# ---------------------------------------------------------------------------


@pytest.fixture
def scheduler():
    with patch("src.scheduler.signal.signal"):
        from src.scheduler import BackupScheduler

        config = MagicMock()
        config.fill_gaps = False
        config.schedule = "0 */6 * * *"
        return BackupScheduler(config)


class TestSchedulerHealth:
    def test_backup_job_starts_late_instead_of_skipping(self, scheduler):
        scheduler.scheduler = MagicMock()
        scheduler.start()
        kwargs = scheduler.scheduler.add_job.call_args.kwargs
        assert kwargs["misfire_grace_time"] == 3600
        assert kwargs["coalesce"] is True

    def test_not_stuck_when_idle_or_recent(self, scheduler):
        assert scheduler._backup_looks_stuck() is False
        scheduler._backup_started_at = datetime.now() - timedelta(hours=1)
        assert scheduler._backup_looks_stuck() is False

    def test_stuck_after_alert_threshold(self, scheduler, monkeypatch):
        monkeypatch.setenv("BACKUP_STUCK_ALERT_HOURS", "6")
        scheduler._backup_started_at = datetime.now() - timedelta(hours=7)
        assert scheduler._backup_looks_stuck() is True

    async def _run_heartbeat_once(self, scheduler, monkeypatch, path):
        monkeypatch.setenv("HEARTBEAT_FILE", str(path))
        task = asyncio.create_task(scheduler._heartbeat_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_heartbeat_written_while_healthy(self, scheduler, monkeypatch, tmp_path):
        path = tmp_path / "hb"
        await self._run_heartbeat_once(scheduler, monkeypatch, path)
        assert path.exists()

    async def test_heartbeat_withheld_while_stuck(self, scheduler, monkeypatch, tmp_path):
        path = tmp_path / "hb"
        scheduler._backup_started_at = datetime.now() - timedelta(days=30)
        await self._run_heartbeat_once(scheduler, monkeypatch, path)
        assert not path.exists()

    async def test_initial_backup_goes_through_guarded_job(self, scheduler):
        scheduler._connect = AsyncMock()
        scheduler.start = MagicMock()
        scheduler._start_listener = AsyncMock()
        scheduler._init_db = AsyncMock()
        scheduler._stop_listener = AsyncMock()
        scheduler._disconnect = AsyncMock()
        scheduler.running = False

        with patch.object(type(scheduler), "_run_backup_job", new=AsyncMock()) as job:
            await scheduler.run_forever()

        job.assert_awaited_once()


class TestHealthcheckScript:
    def _run(self, env):
        script = os.path.join(REPO_ROOT, "scripts", "healthcheck_backup.py")
        return subprocess.run([sys.executable, script], env={**os.environ, **env}).returncode

    def test_missing_heartbeat_is_unhealthy(self, tmp_path):
        assert self._run({"HEARTBEAT_FILE": str(tmp_path / "none")}) == 1

    def test_fresh_heartbeat_is_healthy(self, tmp_path):
        path = tmp_path / "hb"
        path.write_text("x")
        assert self._run({"HEARTBEAT_FILE": str(path)}) == 0

    def test_stale_heartbeat_is_unhealthy(self, tmp_path):
        path = tmp_path / "hb"
        path.write_text("x")
        old = time.time() - 3600
        os.utime(path, (old, old))
        assert self._run({"HEARTBEAT_FILE": str(path)}) == 1


# ---------------------------------------------------------------------------
# Sync cursor is a high-water mark
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def adapter():
    from src.db.adapter import DatabaseAdapter
    from src.db.base import DatabaseManager
    from src.db.models import Base, Chat

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db_manager = DatabaseManager.__new__(DatabaseManager)
    db_manager.engine = engine
    db_manager.database_url = "sqlite+aiosqlite://"
    db_manager._is_sqlite = True
    db_manager.async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with db_manager.async_session_factory() as session:
        session.add(Chat(id=-100, type="group", title="Chat"))
        await session.commit()
    yield DatabaseAdapter(db_manager)
    await engine.dispose()


class TestSyncCursorMonotonic:
    async def test_cursor_never_moves_backwards(self, adapter):
        from src.db.models import SyncStatus

        await adapter.update_sync_status(-100, 500, 10)
        await adapter.update_sync_status(-100, 200, 5)  # e.g. an older export import

        async with adapter.db_manager.async_session_factory() as session:
            row = (await session.execute(select(SyncStatus).where(SyncStatus.chat_id == -100))).scalar_one()
        assert row.last_message_id == 500
        assert row.message_count == 15

    async def test_cursor_still_advances(self, adapter):
        from src.db.models import SyncStatus

        await adapter.update_sync_status(-100, 500, 1)
        await adapter.update_sync_status(-100, 900, 1)

        async with adapter.db_manager.async_session_factory() as session:
            row = (await session.execute(select(SyncStatus).where(SyncStatus.chat_id == -100))).scalar_one()
        assert row.last_message_id == 900


# ---------------------------------------------------------------------------
# Scriptable archived files download instead of rendering on the viewer origin
# ---------------------------------------------------------------------------


class TestInlineSafeMedia:
    @pytest.mark.parametrize("content_type", ["image/jpeg", "image/webp", "video/mp4", "audio/ogg", "application/pdf"])
    def test_viewable_media_stays_inline(self, content_type):
        from src.web.routes_media import _is_inline_safe

        assert _is_inline_safe(content_type) is True

    @pytest.mark.parametrize("content_type", ["text/html", "image/svg+xml", "application/xhtml+xml", "text/javascript", None])
    def test_scriptable_or_unknown_downloads(self, content_type):
        from src.web.routes_media import _is_inline_safe

        assert _is_inline_safe(content_type) is False

    def test_debug_page_is_not_shipped(self):
        assert not os.path.exists(os.path.join(REPO_ROOT, "src", "web", "static", "test-auth.html"))
