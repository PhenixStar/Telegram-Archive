"""One bad message must not cost the rest of a chat, and the cursor must never
name a message the run did not actually commit.

The incremental sweep used to walk newest-first and break at the cursor, which
made the checkpointed cursor the newest id in the chat from the first message
onwards. A run that died after a mid-run checkpoint therefore skipped everything
between the old cursor and the oldest committed batch, permanently and silently,
because gap-fill only notices holes larger than its threshold.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import AuthKeyError, FloodWaitError

from src.telegram_backup import _MAX_SKIP_ATTEMPTS, TelegramBackup


def _backup(**attrs):
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.client = MagicMock()
    backup._connection = None
    backup.config = SimpleNamespace(deletion_mode="hard")
    for key, value in attrs.items():
        setattr(backup, key, value)
    return backup


def _metadata_db():
    """A database double whose metadata store behaves like the real key-value table."""
    store: dict[str, str] = {}
    db = SimpleNamespace(store=store)
    db.get_metadata = AsyncMock(side_effect=lambda key: store.get(key))

    async def _set(key, value):
        store[key] = value

    db.set_metadata = AsyncMock(side_effect=_set)
    return db


class TestPerMessageIsolation:
    @pytest.mark.asyncio
    async def test_a_broken_message_is_skipped_not_fatal(self):
        backup = _backup()
        backup._process_message = AsyncMock(side_effect=ValueError("malformed document"))

        assert await backup._process_message_isolated(SimpleNamespace(id=7), -100) is None

    @pytest.mark.asyncio
    async def test_a_good_message_is_returned_unchanged(self):
        backup = _backup()
        backup._process_message = AsyncMock(return_value={"id": 7})

        assert await backup._process_message_isolated(SimpleNamespace(id=7), -100) == {"id": 7}

    @pytest.mark.parametrize(
        "error",
        [
            FloodWaitError(request=None),
            AuthKeyError(request=None, message="auth key gone"),
            ConnectionError("dropped"),
            TimeoutError("stalled"),
            asyncio.CancelledError(),
        ],
    )
    @pytest.mark.asyncio
    async def test_run_level_failures_are_re_raised(self, error):
        # These say something about the run, not the message: swallowing them would
        # either hammer Telegram or silently skip healthy messages. CancelledError in
        # particular is how the stall guard stops a wedged run.
        backup = _backup()
        backup._process_message = AsyncMock(side_effect=error)

        with pytest.raises(type(error)):
            await backup._process_message_isolated(SimpleNamespace(id=7), -100)


class TestSkipRecords:
    @pytest.mark.asyncio
    async def test_skips_are_persisted_with_an_attempt_count(self):
        db = _metadata_db()
        backup = _backup(db=db)

        await backup._persist_skipped_messages(-100, [7, 9])
        await backup._persist_skipped_messages(-100, [7])

        assert json.loads(db.store["skipped_messages:-100"]) == {"7": 2, "9": 1}

    @pytest.mark.asyncio
    async def test_nothing_is_written_when_no_message_was_skipped(self):
        db = _metadata_db()
        backup = _backup(db=db)

        await backup._persist_skipped_messages(-100, [])

        db.set_metadata.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_attempts_stop_at_the_permanent_marker(self):
        db = _metadata_db()
        backup = _backup(db=db)

        for _ in range(_MAX_SKIP_ATTEMPTS + 4):
            await backup._persist_skipped_messages(-100, [7])

        assert json.loads(db.store["skipped_messages:-100"]) == {"7": _MAX_SKIP_ATTEMPTS}


class TestSkipRetry:
    @pytest.mark.asyncio
    async def test_a_recovered_message_is_committed_and_forgotten(self):
        db = _metadata_db()
        db.store["skipped_messages:-100"] = json.dumps({"7": 1})
        backup = _backup(db=db)
        backup.client.get_messages = AsyncMock(return_value=[SimpleNamespace(id=7)])
        backup._process_message = AsyncMock(return_value={"id": 7})
        backup._commit_batch = AsyncMock()

        recovered = await backup._retry_skipped_messages(-100, SimpleNamespace())

        assert recovered == 1
        backup._commit_batch.assert_awaited_once()
        assert json.loads(db.store["skipped_messages:-100"]) == {}

    @pytest.mark.asyncio
    async def test_a_message_deleted_upstream_stops_being_retried(self):
        db = _metadata_db()
        db.store["skipped_messages:-100"] = json.dumps({"7": _MAX_SKIP_ATTEMPTS - 1})
        backup = _backup(db=db)
        backup.client.get_messages = AsyncMock(return_value=[None])
        backup._commit_batch = AsyncMock()

        recovered = await backup._retry_skipped_messages(-100, SimpleNamespace())

        assert recovered == 0
        assert json.loads(db.store["skipped_messages:-100"]) == {"7": _MAX_SKIP_ATTEMPTS}
        backup._commit_batch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_omitted_id_is_not_confused_with_another_message(self):
        # Telegram omits ids it will not return instead of leaving a None slot, so a
        # positional pairing would store message 12's content as message 9 and then
        # forget 9 — and the cursor is already past it, so nothing would ever look
        # at it again.
        db = _metadata_db()
        db.store["skipped_messages:-100"] = json.dumps({"7": 1, "9": 1, "12": 1})
        backup = _backup(db=db)
        backup.client.get_messages = AsyncMock(
            return_value=[SimpleNamespace(id=7), SimpleNamespace(id=12)]
        )
        processed: list[int] = []

        async def _process(message, chat_id):
            processed.append(message.id)
            return {"id": message.id}

        backup._process_message = AsyncMock(side_effect=_process)
        backup._commit_batch = AsyncMock()

        recovered = await backup._retry_skipped_messages(-100, SimpleNamespace())

        assert recovered == 2
        assert processed == [7, 12]
        # 9 stays on the list with one more attempt spent, and is not dropped.
        assert json.loads(db.store["skipped_messages:-100"]) == {"9": 2}

    @pytest.mark.asyncio
    async def test_a_message_that_fails_again_keeps_counting_up(self):
        db = _metadata_db()
        db.store["skipped_messages:-100"] = json.dumps({"7": 1})
        backup = _backup(db=db)
        backup.client.get_messages = AsyncMock(return_value=[SimpleNamespace(id=7)])
        backup._process_message = AsyncMock(side_effect=ValueError("still broken"))
        backup._commit_batch = AsyncMock()

        assert await backup._retry_skipped_messages(-100, SimpleNamespace()) == 0
        assert json.loads(db.store["skipped_messages:-100"]) == {"7": 2}

    @pytest.mark.asyncio
    async def test_permanently_marked_messages_cost_no_api_call(self):
        db = _metadata_db()
        db.store["skipped_messages:-100"] = json.dumps({"7": _MAX_SKIP_ATTEMPTS})
        backup = _backup(db=db)
        backup.client.get_messages = AsyncMock()

        assert await backup._retry_skipped_messages(-100, SimpleNamespace()) == 0
        backup.client.get_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_record_means_no_work(self):
        db = _metadata_db()
        backup = _backup(db=db)
        backup.client.get_messages = AsyncMock()

        assert await backup._retry_skipped_messages(-100, SimpleNamespace()) == 0
        backup.client.get_messages.assert_not_awaited()


class TestDialogSweepOrder:
    """The whole point of the change: iterate ascending from the cursor, and never
    checkpoint past a skip that has not been persisted."""

    def _dialog_backup(self, db):
        backup = _backup(db=db)
        backup.config = SimpleNamespace(
            deletion_mode="hard",
            batch_size=2,
            checkpoint_interval=1,
            skip_media_chat_ids=set(),
            skip_media_delete_existing=False,
            sync_deletions_edits=False,
        )
        backup._cleaned_media_chats = set()
        backup._get_marked_id = MagicMock(return_value=-100)
        backup._extract_chat_data = MagicMock(return_value={"id": -100})
        backup._ensure_profile_photo = AsyncMock()
        backup._sync_pinned_messages = AsyncMock()
        backup._commit_batch = AsyncMock()
        return backup

    @pytest.mark.asyncio
    async def test_sweep_starts_at_the_cursor_and_runs_ascending(self, monkeypatch):
        db = _metadata_db()
        db.upsert_chat = AsyncMock()
        db.get_last_message_id = AsyncMock(return_value=500)
        db.update_sync_status = AsyncMock()

        backup = self._dialog_backup(db)
        backup._process_message = AsyncMock(side_effect=lambda m, c: {"id": m.id})

        seen_kwargs = {}

        async def fake_iter(client, entity, **kwargs):
            seen_kwargs.update(kwargs)
            for message_id in (501, 502, 503, 504):
                yield SimpleNamespace(id=message_id)

        monkeypatch.setattr("src.telegram_backup.iter_messages_with_flood_retry", fake_iter)

        total = await backup._backup_dialog(SimpleNamespace(entity=SimpleNamespace()))

        assert total == 4
        # Ascending from just after the cursor, which is what makes the checkpoint
        # value a true high-water mark of committed work.
        assert seen_kwargs == {"min_id": 500, "reverse": True}

    @pytest.mark.asyncio
    async def test_a_skipped_message_does_not_abort_the_chat(self, monkeypatch):
        db = _metadata_db()
        db.upsert_chat = AsyncMock()
        db.get_last_message_id = AsyncMock(return_value=0)
        db.update_sync_status = AsyncMock()

        backup = self._dialog_backup(db)

        async def flaky(message, chat_id):
            if message.id == 502:
                raise ValueError("malformed document")
            return {"id": message.id}

        backup._process_message = AsyncMock(side_effect=flaky)

        async def fake_iter(client, entity, **kwargs):
            for message_id in (501, 502, 503):
                yield SimpleNamespace(id=message_id)

        monkeypatch.setattr("src.telegram_backup.iter_messages_with_flood_retry", fake_iter)

        total = await backup._backup_dialog(SimpleNamespace(entity=SimpleNamespace()))

        # The two healthy messages are kept, and the bad one is recorded for retry.
        assert total == 2
        assert json.loads(db.store["skipped_messages:-100"]) == {"502": 1}

    @pytest.mark.asyncio
    async def test_the_cursor_never_moves_past_an_unpersisted_skip(self, monkeypatch):
        db = _metadata_db()
        db.upsert_chat = AsyncMock()
        db.get_last_message_id = AsyncMock(return_value=0)
        db.update_sync_status = AsyncMock()
        db.set_metadata = AsyncMock(side_effect=OSError("database is locked"))

        backup = self._dialog_backup(db)

        async def flaky(message, chat_id):
            if message.id == 501:
                raise ValueError("malformed document")
            return {"id": message.id}

        backup._process_message = AsyncMock(side_effect=flaky)

        async def fake_iter(client, entity, **kwargs):
            for message_id in (501, 502, 503):
                yield SimpleNamespace(id=message_id)

        monkeypatch.setattr("src.telegram_backup.iter_messages_with_flood_retry", fake_iter)

        # A skip record that cannot be written must abort the dialog rather than let
        # the cursor advance past a message nothing will ever look at again.
        with pytest.raises(OSError):
            await backup._backup_dialog(SimpleNamespace(entity=SimpleNamespace()))

        db.update_sync_status.assert_not_awaited()
