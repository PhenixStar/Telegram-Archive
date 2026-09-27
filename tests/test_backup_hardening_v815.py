"""Capture/DB hardening ported from upstream: forward ids, busy timeout, push cleanup."""

import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import insert, select
from telethon.tl.types import PeerChannel, PeerChat, PeerUser

from src.backup_extraction import BackupExtractionMixin
from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager, _busy_timeout_ms
from src.db.models import PushSubscription


def _forward(peer):
    return SimpleNamespace(fwd_from=SimpleNamespace(from_id=peer))


@pytest.mark.parametrize(
    ("peer", "expected"),
    [(PeerUser(42), 42), (PeerChat(7), -7), (PeerChannel(1234567890), -1001234567890)],
)
def test_forwarded_from_is_stored_as_the_marked_id(peer, expected):
    assert BackupExtractionMixin()._extract_forward_from_id(_forward(peer)) == expected


def test_forward_without_source_is_none():
    assert BackupExtractionMixin()._extract_forward_from_id(SimpleNamespace(fwd_from=None)) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("60.0", 60000), ("5", 5000), ("0.0001", 1), ("0", 60000), ("-3", 60000), ("nan", 60000), ("abc", 60000)],
)
def test_database_timeout_reaches_busy_timeout(raw, expected):
    with patch.dict(os.environ, {"DATABASE_TIMEOUT": raw}):
        assert _busy_timeout_ms() == expected


@pytest.fixture
async def adapter(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'push.db'}")
    await manager.init()
    try:
        yield DatabaseAdapter(manager)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_deleting_a_chat_removes_its_push_subscriptions_only(adapter, tmp_path):
    await adapter.upsert_chat({"id": 100, "type": "private"})
    await adapter.insert_message({"id": 1, "chat_id": 100, "date": datetime(2026, 1, 1), "text": "x"})
    columns = {c.name for c in PushSubscription.__table__.columns}
    base = {"endpoint": "https://push/", "p256dh": "k", "auth": "a", "created_at": datetime(2026, 1, 1)}
    rows = [
        {k: v for k, v in {**base, "endpoint": "https://push/chat", "chat_id": 100}.items() if k in columns},
        {k: v for k, v in {**base, "endpoint": "https://push/global", "chat_id": None}.items() if k in columns},
    ]
    async with adapter.db_manager.async_session_factory() as session:
        await session.execute(insert(PushSubscription), rows)
        await session.commit()

    await adapter.delete_chat_and_related_data(100, str(tmp_path))

    async with adapter.db_manager.async_session_factory() as session:
        left = [r[0] for r in await session.execute(select(PushSubscription.endpoint))]
    assert left == ["https://push/global"]
