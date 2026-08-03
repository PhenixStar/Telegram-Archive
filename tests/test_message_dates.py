"""Tests for MessageMixin.get_message_dates (calendar day-availability)."""

import os
import sys
from datetime import datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Chat, Message


@pytest_asyncio.fixture
async def dates_adapter():
    """In-memory DB with messages on a few UTC days, some in a forum topic."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    db_manager = DatabaseManager.__new__(DatabaseManager)
    db_manager.engine = engine
    db_manager.database_url = "sqlite+aiosqlite://"
    db_manager._is_sqlite = True
    db_manager.async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    chat_id = -100777
    async with db_manager.async_session_factory() as session:
        session.add(Chat(id=chat_id, type="group", title="Chat", is_forum=1))
        rows = [
            (1, datetime(2026, 2, 1, 9, 0), None),   # Feb 1
            (2, datetime(2026, 2, 1, 23, 0), None),  # Feb 1 (same day, 2nd msg)
            (3, datetime(2026, 2, 3, 12, 0), 7),     # Feb 3, topic 7
            (4, datetime(2026, 2, 15, 0, 30), 1),    # Feb 15, general
        ]
        for mid, dt, top in rows:
            session.add(Message(id=mid, chat_id=chat_id, date=dt, text="x", reply_to_top_id=top))
        await session.commit()

    yield DatabaseAdapter(db_manager), chat_id
    await engine.dispose()


def _utc_day_ranges(year, month, days):
    """Build (date_str, utc_start, utc_end) tuples for the given day numbers (UTC)."""
    ranges = []
    for d in days:
        start = datetime(year, month, d)
        end = datetime(year, month, d + 1) if d < 28 else datetime(year, month + 1, 1)
        ranges.append((f"{year:04d}-{month:02d}-{d:02d}", start, end))
    return ranges


async def test_get_message_dates_returns_days_with_messages(dates_adapter):
    adapter, chat_id = dates_adapter
    ranges = _utc_day_ranges(2026, 2, [1, 2, 3, 15])
    dates = await adapter.get_message_dates(chat_id, ranges)
    assert dates == ["2026-02-01", "2026-02-03", "2026-02-15"]  # not Feb 2 (empty)


async def test_get_message_dates_topic_scoped(dates_adapter):
    adapter, chat_id = dates_adapter
    ranges = _utc_day_ranges(2026, 2, [1, 3, 15])
    # topic 7 only has the Feb 3 message
    assert await adapter.get_message_dates(chat_id, ranges, topic_id=7) == ["2026-02-03"]
    # General (topic_id=1) matches NULL rows (Feb 1) AND explicit-1 rows (Feb 15)
    # via coalesce(reply_to_top_id, 1), but not the topic-7 row (Feb 3).
    assert await adapter.get_message_dates(chat_id, ranges, topic_id=1) == ["2026-02-01", "2026-02-15"]


async def test_get_message_dates_empty_ranges(dates_adapter):
    adapter, chat_id = dates_adapter
    assert await adapter.get_message_dates(chat_id, []) == []
