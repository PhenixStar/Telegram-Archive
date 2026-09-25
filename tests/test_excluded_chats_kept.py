"""Excluded chats stop being captured but keep what the archive already holds."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.telegram_backup import TelegramBackup


def _backup(exclude_delete_existing: bool) -> TelegramBackup:
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.config = SimpleNamespace(exclude_delete_existing=exclude_delete_existing, media_path="/data/backups/media")
    backup.db = SimpleNamespace(delete_chat_and_related_data=AsyncMock())
    return backup


@pytest.mark.asyncio
async def test_excluded_chats_are_kept_by_default():
    backup = _backup(exclude_delete_existing=False)

    await backup._handle_excluded_chats({-100, -200})

    backup.db.delete_chat_and_related_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_excluded_chats_are_deleted_only_when_asked():
    backup = _backup(exclude_delete_existing=True)

    await backup._handle_excluded_chats({-100, -200})

    deleted = {call.args[0] for call in backup.db.delete_chat_and_related_data.await_args_list}
    assert deleted == {-100, -200}


@pytest.mark.asyncio
async def test_one_failed_deletion_does_not_stop_the_rest():
    backup = _backup(exclude_delete_existing=True)
    backup.db.delete_chat_and_related_data.side_effect = [RuntimeError("locked"), None]

    await backup._handle_excluded_chats({-100, -200})

    assert backup.db.delete_chat_and_related_data.await_count == 2
