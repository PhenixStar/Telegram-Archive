"""Tests for the ported chat export date-range filter (adapter + endpoint).

Covers:
  * DatabaseAdapter.get_messages_for_export — date_from/date_to (both
    inclusive, same contract as get_messages_paginated)
  * GET /api/chats/{id}/export — date_from/date_to forwarded, 400 on an
    inverted range, "filters" block only present when windowed, and the
    JSON export's message_versions are windowed at the route layer (the
    generator itself, iter_message_versions_for_export, is out of this
    lane's file-ownership scope and is left untouched)

Adapter tests use real in-memory SQLite. Endpoint tests override require_auth
so ACL / windowing logic is tested directly (same pattern as
test_gallery_backend.py).
"""

import json
import os
import sys
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Chat


@pytest_asyncio.fixture
async def adapter():
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

    db = DatabaseAdapter(db_manager)
    async with db_manager.async_session_factory() as session:
        session.add(Chat(id=-1001, type="channel", title="Chat A"))
        await session.commit()
    return db


class TestGetMessagesForExportDateRange:
    async def test_no_bounds_returns_everything(self, adapter):
        for day in range(1, 4):
            await adapter.insert_message(
                {"id": day, "chat_id": -1001, "date": datetime(2026, 1, day), "text": f"m{day}"}
            )
        msgs = [m async for m in adapter.get_messages_for_export(-1001)]
        assert len(msgs) == 3

    async def test_date_from_is_inclusive(self, adapter):
        for day in range(1, 4):
            await adapter.insert_message(
                {"id": day, "chat_id": -1001, "date": datetime(2026, 1, day), "text": f"m{day}"}
            )
        msgs = [m async for m in adapter.get_messages_for_export(-1001, date_from=datetime(2026, 1, 2))]
        assert [m["id"] for m in msgs] == [2, 3]

    async def test_date_to_is_inclusive(self, adapter):
        for day in range(1, 4):
            await adapter.insert_message(
                {"id": day, "chat_id": -1001, "date": datetime(2026, 1, day), "text": f"m{day}"}
            )
        msgs = [m async for m in adapter.get_messages_for_export(-1001, date_to=datetime(2026, 1, 2))]
        assert [m["id"] for m in msgs] == [1, 2]

    async def test_both_bounds_narrow_to_window(self, adapter):
        for day in range(1, 6):
            await adapter.insert_message(
                {"id": day, "chat_id": -1001, "date": datetime(2026, 1, day), "text": f"m{day}"}
            )
        msgs = [
            m
            async for m in adapter.get_messages_for_export(
                -1001, date_from=datetime(2026, 1, 2), date_to=datetime(2026, 1, 4)
            )
        ]
        assert [m["id"] for m in msgs] == [2, 3, 4]

    async def test_window_applies_with_include_media_too(self, adapter):
        for day in range(1, 4):
            await adapter.insert_message(
                {"id": day, "chat_id": -1001, "date": datetime(2026, 1, day), "text": f"m{day}"}
            )
        msgs = [
            m async for m in adapter.get_messages_for_export(-1001, include_media=True, date_from=datetime(2026, 1, 2))
        ]
        assert [m["id"] for m in msgs] == [2, 3]


# ---------------------------------------------------------------------------
# Endpoint tests (require_auth overridden for direct ACL/windowing coverage)
# ---------------------------------------------------------------------------


def _fake_messages(*rows):
    async def gen(chat_id, include_media=False, date_from=None, date_to=None):
        for row in rows:
            yield dict(row)

    return gen


def _fake_versions(*rows):
    async def gen(chat_id):
        for row in rows:
            yield dict(row)

    return gen


@pytest.fixture
def export_client():
    """TestClient with deps.db mocked and require_auth overridable per-test.

    Overrides through ``routes_chat.require_auth`` (the object export_chat's
    own ``Depends(require_auth)`` was bound to) rather than
    ``dependencies.require_auth`` directly, so the override matches even if
    another test module in the suite reloaded ``dependencies`` without also
    reloading ``routes_chat`` in the same step.
    """
    from unittest.mock import AsyncMock

    import src.web.dependencies as deps
    import src.web.main as main_mod
    import src.web.routes_chat as routes_chat
    from src.web.dependencies import UserContext

    db = AsyncMock()
    db.get_chat_by_id = AsyncMock(return_value={"id": -1001, "type": "channel", "title": "Chat A", "username": None})
    db.get_messages_for_export = _fake_messages({"id": 1, "date": "2026-01-01T00:00:00", "text": "hi"})
    db.iter_message_versions_for_export = _fake_versions()
    deps.db = db
    deps.config = main_mod.config
    main_mod.db = db

    app = main_mod.app

    def set_user(user: UserContext):
        app.dependency_overrides[routes_chat.require_auth] = lambda: user

    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, deps, set_user, UserContext
    finally:
        app.dependency_overrides.pop(routes_chat.require_auth, None)


class TestExportChatDateRange:
    def test_unwindowed_export_has_no_filters_block(self, export_client):
        client, _, set_user, UserContext = export_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        resp = client.get("/api/chats/-1001/export")
        assert resp.status_code == 200
        assert "filters" not in json.loads(resp.text)

    def test_windowed_export_carries_filters_block(self, export_client):
        client, _, set_user, UserContext = export_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        resp = client.get("/api/chats/-1001/export?date_from=2026-01-01&date_to=2026-01-31")
        data = json.loads(resp.text)
        assert data["filters"] == {"date_from": "2026-01-01", "date_to": "2026-01-31"}

    def test_date_from_after_date_to_is_400(self, export_client):
        client, _, set_user, UserContext = export_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        resp = client.get("/api/chats/-1001/export?date_from=2026-02-01&date_to=2026-01-01")
        assert resp.status_code == 400

    def test_invalid_date_format_is_400(self, export_client):
        client, _, set_user, UserContext = export_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        resp = client.get("/api/chats/-1001/export?date_from=not-a-date")
        assert resp.status_code == 400

    def test_dates_forwarded_to_adapter_for_csv_too(self, export_client):
        client, deps, set_user, UserContext = export_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        calls = []

        def gen(chat_id, include_media=False, date_from=None, date_to=None):
            calls.append((chat_id, include_media, date_from, date_to))

            async def _empty():
                return
                yield  # pragma: no cover - makes this an async generator

            return _empty()

        deps.db.get_messages_for_export = gen
        resp = client.get("/api/chats/-1001/export?format=csv&date_from=2026-01-01&date_to=2026-01-31")
        assert resp.status_code == 200
        assert calls == [(-1001, True, datetime(2026, 1, 1), datetime(2026, 1, 31))]

    def test_no_download_still_blocks_windowed_export(self, export_client):
        client, _, set_user, UserContext = export_client
        set_user(UserContext(username="v", role="viewer", allowed_chat_ids=None, no_download=True))
        resp = client.get("/api/chats/-1001/export?date_from=2026-01-01")
        assert resp.status_code == 403

    def test_acl_forbidden_chat_returns_403(self, export_client):
        client, _, set_user, UserContext = export_client
        set_user(UserContext(username="v", role="viewer", allowed_chat_ids={-1002}))
        resp = client.get("/api/chats/-1001/export")
        assert resp.status_code == 403

    def test_message_versions_are_windowed_at_the_route_layer(self, export_client):
        """The generator this lane may not touch (iter_message_versions_for_export)
        stays unwindowed; export_chat filters its output by date instead, so the
        observable JSON is still correctly windowed end to end."""
        client, deps, set_user, UserContext = export_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        deps.db.iter_message_versions_for_export = _fake_versions(
            {"chat_id": -1001, "message_id": 1, "text": "too old", "date": datetime(2025, 12, 1)},
            {"chat_id": -1001, "message_id": 2, "text": "in window", "date": datetime(2026, 1, 15)},
            {"chat_id": -1001, "message_id": 3, "text": "too new", "date": datetime(2026, 2, 1)},
        )
        resp = client.get("/api/chats/-1001/export?date_from=2026-01-01&date_to=2026-01-31")
        data = json.loads(resp.text)
        assert [v["message_id"] for v in data["message_versions"]] == [2]

    def test_unwindowed_export_keeps_every_version(self, export_client):
        client, deps, set_user, UserContext = export_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        deps.db.iter_message_versions_for_export = _fake_versions(
            {"chat_id": -1001, "message_id": 1, "text": "a", "date": datetime(2025, 1, 1)},
            {"chat_id": -1001, "message_id": 2, "text": "b", "date": datetime(2026, 6, 1)},
        )
        resp = client.get("/api/chats/-1001/export")
        data = json.loads(resp.text)
        assert len(data["message_versions"]) == 2
