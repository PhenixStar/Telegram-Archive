"""Gap-fill remembers gaps it already found empty, so scheduled runs skip deleted ranges."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import RPCError

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.telegram_backup import TelegramBackup

CHAT = -100123


@pytest.fixture
async def adapter(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'gaps.db'}")
    await manager.init()
    try:
        yield DatabaseAdapter(manager)
    finally:
        await manager.close()


async def test_empty_gaps_round_trip_per_chat(adapter):
    await adapter.mark_gap_empty(CHAT, 10, 100)
    await adapter.mark_gap_empty(CHAT, 10, 100)  # recording twice is harmless
    await adapter.mark_gap_empty(CHAT, 200, 300)
    await adapter.mark_gap_empty(999, 1, 2)

    assert await adapter.get_empty_gaps(CHAT) == {(10, 100), (200, 300)}
    assert await adapter.get_empty_gaps(555) == set()


def _backup(gaps, known_empty=frozenset()):
    config = MagicMock()
    config.gap_threshold = 50
    backup = TelegramBackup(config, AsyncMock())
    backup._connection = None
    backup.db.get_chats_with_messages = AsyncMock(return_value=[CHAT])
    backup.db.detect_message_gaps = AsyncMock(return_value=gaps)
    backup.db.get_empty_gaps = AsyncMock(return_value=set(known_empty))
    backup.db.mark_gap_empty = AsyncMock()
    backup.client = MagicMock()
    backup.client.get_entity = AsyncMock(return_value=MagicMock())
    backup._get_chat_name = MagicMock(return_value="chat")
    backup._heal_or_end_run = AsyncMock()
    return backup


async def test_known_empty_gaps_are_not_fetched_again():
    backup = _backup([(1, 100, 99), (200, 400, 200)], known_empty={(1, 100)})
    backup._fill_gap_range = AsyncMock(return_value=0)

    result = await backup._fill_gaps()

    backup._fill_gap_range.assert_awaited_once()
    assert backup._fill_gap_range.await_args.args[2:] == (200, 400)
    assert result["known_empty_skipped"] == 1
    assert result["total_gaps"] == 1


async def test_chat_with_only_known_empty_gaps_skips_entity_lookup():
    backup = _backup([(1, 100, 99)], known_empty={(1, 100)})
    backup._fill_gap_range = AsyncMock()

    result = await backup._fill_gaps()

    backup.client.get_entity.assert_not_awaited()
    backup._fill_gap_range.assert_not_awaited()
    assert result["chats_with_gaps"] == 0


async def test_clean_empty_fetch_is_recorded_but_recovered_gap_is_not():
    backup = _backup([(1, 100, 99), (200, 400, 200)])
    backup._fill_gap_range = AsyncMock(side_effect=[0, 5])

    result = await backup._fill_gaps()

    backup.db.mark_gap_empty.assert_awaited_once_with(CHAT, 1, 100)
    assert result["total_recovered"] == 5


async def test_failed_fetch_is_not_recorded_as_empty(caplog):
    backup = _backup([(1, 100, 99)])
    backup._fill_gap_range = AsyncMock(side_effect=RPCError(None, "boom"))

    with caplog.at_level(logging.ERROR):
        await backup._fill_gaps()

    backup.db.mark_gap_empty.assert_not_awaited()


async def test_force_rechecks_known_empty_gaps():
    backup = _backup([(1, 100, 99)], known_empty={(1, 100)})
    backup._fill_gap_range = AsyncMock(return_value=0)

    result = await backup._fill_gaps(force=True)

    backup.db.get_empty_gaps.assert_not_awaited()
    backup._fill_gap_range.assert_awaited_once()
    assert result["known_empty_skipped"] == 0


def test_cli_fill_gaps_has_force_flag():
    from src.__main__ import create_parser

    parser = create_parser()
    assert parser.parse_args(["fill-gaps", "--force"]).force is True
    assert parser.parse_args(["fill-gaps"]).force is False
