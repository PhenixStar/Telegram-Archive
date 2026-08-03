"""Tests for the ``sender_name`` capture-time snapshot column (#241 part B).

Covers ``message_utils.sender_display_name`` and the adapter-level
persistence/immutability contract: once a nonblank sender_name is archived,
later upserts (re-scans, imports, listener replays) may never overwrite it,
but a missing/blank snapshot may be hydrated exactly once.
"""

from datetime import datetime

import pytest
from sqlalchemy import select

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Message
from src.message_utils import sender_display_name


class TestSenderDisplayName:
    def test_first_and_last_name(self):
        sender = type("S", (), {"first_name": "Ada", "last_name": "Lovelace"})()
        assert sender_display_name(sender) == "Ada Lovelace"

    def test_first_name_only(self):
        sender = type("S", (), {"first_name": "Ada", "last_name": None})()
        assert sender_display_name(sender) == "Ada"

    def test_strips_whitespace(self):
        sender = type("S", (), {"first_name": "  Ada  ", "last_name": None})()
        assert sender_display_name(sender) == "Ada"

    def test_falls_back_to_title_for_channels(self):
        sender = type("S", (), {"first_name": None, "last_name": None, "title": "News Channel"})()
        assert sender_display_name(sender) == "News Channel"

    def test_falls_back_to_username(self):
        sender = type("S", (), {"first_name": None, "last_name": None, "username": "bot_account"})()
        assert sender_display_name(sender) == "bot_account"

    def test_blank_names_treated_as_absent(self):
        sender = type("S", (), {"first_name": "   ", "last_name": "", "title": None, "username": None})()
        assert sender_display_name(sender) is None

    def test_none_sender(self):
        assert sender_display_name(None) is None


@pytest.fixture
async def sqlite_adapter(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'telegram_archive.db'}")
    await manager.init()
    try:
        yield DatabaseAdapter(manager)
    finally:
        await manager.close()


async def _get_message(adapter: DatabaseAdapter, message_id: int, chat_id: int) -> Message:
    async with adapter.db_manager.async_session_factory() as session:
        result = await session.execute(select(Message).where(Message.id == message_id, Message.chat_id == chat_id))
        message = result.scalar_one()
        return message


@pytest.mark.asyncio
class TestSenderNamePersistence:
    async def test_insert_message_stores_trimmed_sender_name(self, sqlite_adapter):
        await sqlite_adapter.insert_message(
            {
                "id": 1,
                "chat_id": 100,
                "date": datetime(2026, 8, 1, 10, 0),
                "text": "hi",
                "sender_name": "  Alice  ",
            }
        )
        message = await _get_message(sqlite_adapter, 1, 100)
        assert message.sender_name == "Alice"

    async def test_insert_message_blank_sender_name_stored_as_none(self, sqlite_adapter):
        await sqlite_adapter.insert_message(
            {
                "id": 1,
                "chat_id": 100,
                "date": datetime(2026, 8, 1, 10, 0),
                "text": "hi",
                "sender_name": "   ",
            }
        )
        message = await _get_message(sqlite_adapter, 1, 100)
        assert message.sender_name is None

    async def test_upsert_hydrates_missing_sender_name(self, sqlite_adapter):
        """A row archived without a sender_name may be filled in once."""
        await sqlite_adapter.insert_message(
            {"id": 1, "chat_id": 100, "date": datetime(2026, 8, 1, 10, 0), "text": "hi"}
        )
        await sqlite_adapter.insert_message(
            {
                "id": 1,
                "chat_id": 100,
                "date": datetime(2026, 8, 1, 10, 0),
                "text": "hi",
                "sender_name": "Alice",
            }
        )
        message = await _get_message(sqlite_adapter, 1, 100)
        assert message.sender_name == "Alice"

    async def test_upsert_never_overwrites_archived_sender_name(self, sqlite_adapter):
        """A later upsert (re-scan/import replay) with a different name must
        not clobber the capture-time snapshot — it's immutable once nonblank."""
        await sqlite_adapter.insert_message(
            {
                "id": 1,
                "chat_id": 100,
                "date": datetime(2026, 8, 1, 10, 0),
                "text": "hi",
                "sender_name": "Alice",
            }
        )
        await sqlite_adapter.insert_message(
            {
                "id": 1,
                "chat_id": 100,
                "date": datetime(2026, 8, 1, 10, 0),
                "text": "hi",
                "sender_name": "Alice (renamed)",
            }
        )
        message = await _get_message(sqlite_adapter, 1, 100)
        assert message.sender_name == "Alice"

    async def test_upsert_blank_incoming_does_not_clear_archived_name(self, sqlite_adapter):
        await sqlite_adapter.insert_message(
            {
                "id": 1,
                "chat_id": 100,
                "date": datetime(2026, 8, 1, 10, 0),
                "text": "hi",
                "sender_name": "Alice",
            }
        )
        await sqlite_adapter.insert_message(
            {"id": 1, "chat_id": 100, "date": datetime(2026, 8, 1, 10, 0), "text": "hi", "sender_name": None}
        )
        message = await _get_message(sqlite_adapter, 1, 100)
        assert message.sender_name == "Alice"

    async def test_batch_upsert_respects_immutability(self, sqlite_adapter):
        await sqlite_adapter.insert_messages_batch(
            [
                {
                    "id": 1,
                    "chat_id": 100,
                    "date": datetime(2026, 8, 1, 10, 0),
                    "text": "hi",
                    "sender_name": "Alice",
                }
            ]
        )
        await sqlite_adapter.insert_messages_batch(
            [
                {
                    "id": 1,
                    "chat_id": 100,
                    "date": datetime(2026, 8, 1, 10, 0),
                    "text": "hi",
                    "sender_name": "Someone Else",
                }
            ]
        )
        message = await _get_message(sqlite_adapter, 1, 100)
        assert message.sender_name == "Alice"

    async def test_message_to_dict_includes_sender_name(self, sqlite_adapter):
        await sqlite_adapter.insert_message(
            {
                "id": 1,
                "chat_id": 100,
                "date": datetime(2026, 8, 1, 10, 0),
                "text": "hi",
                "sender_name": "Alice",
            }
        )
        message = await _get_message(sqlite_adapter, 1, 100)
        as_dict = sqlite_adapter._message_to_dict(message)
        assert as_dict["sender_name"] == "Alice"
