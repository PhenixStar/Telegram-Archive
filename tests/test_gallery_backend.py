"""Tests for the ported media gallery backend (adapter + endpoints).

Covers:
  * DatabaseAdapter.get_media_paginated  — pagination, type filter, sender name
  * DatabaseAdapter.get_media_counts     — grouped counts excluding undownloaded
  * GET /api/chats/{id}/media            — ACL 403, thumb_url for image+video,
                                           no_download strips file_path
  * GET /api/chats/{id}/media/counts     — ACL 403

Adapter tests use real in-memory SQLite to exercise the actual SQL.
Endpoint tests override require_auth so ACL / no_download / path logic is
tested directly without coupling to the login internals.
"""

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
from src.db.models import Base, Chat, Media, Message, User


# ---------------------------------------------------------------------------
# Adapter fixture (real in-memory SQLite)
# ---------------------------------------------------------------------------


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

    chat_id = -1001
    async with db_manager.async_session_factory() as session:
        session.add(Chat(id=chat_id, type="channel", title="Test Chat"))
        session.add(User(id=100, first_name="Alice", last_name="Smith", username="alice"))
        session.add(User(id=200, first_name="Bob", last_name=None, username="bob"))

        session.add(Message(id=1, chat_id=chat_id, sender_id=100, date=datetime(2026, 1, 1, 10), text="photo"))
        session.add(Message(id=2, chat_id=chat_id, sender_id=100, date=datetime(2026, 1, 2, 10), text="album"))
        session.add(Message(id=3, chat_id=chat_id, sender_id=200, date=datetime(2026, 1, 3, 10), text="video"))
        session.add(Message(id=4, chat_id=chat_id, sender_id=None, date=datetime(2026, 1, 4, 10), text="doc"))

        # photo_1 (msg 1), album photo_2a + photo_2b (msg 2), video_3 (msg 3), doc_4 (msg 4)
        session.add(Media(id="photo_1", message_id=1, chat_id=chat_id, type="photo",
                          file_path="-1001/photo_1.jpg", file_name="photo_1.jpg", file_size=100000,
                          mime_type="image/jpeg", width=1920, height=1080, downloaded=1))
        session.add(Media(id="photo_2a", message_id=2, chat_id=chat_id, type="photo",
                          file_path="-1001/photo_2a.jpg", file_name="photo_2a.jpg", file_size=200000,
                          mime_type="image/jpeg", width=1920, height=1080, downloaded=1))
        session.add(Media(id="photo_2b", message_id=2, chat_id=chat_id, type="photo",
                          file_path="-1001/photo_2b.jpg", file_name="photo_2b.jpg", file_size=150000,
                          mime_type="image/jpeg", width=1920, height=1080, downloaded=1))
        session.add(Media(id="video_3", message_id=3, chat_id=chat_id, type="video",
                          file_path="-1001/video_3.mp4", file_name="video_3.mp4", file_size=5000000,
                          mime_type="video/mp4", width=1280, height=720, duration=30, downloaded=1))
        session.add(Media(id="doc_4", message_id=4, chat_id=chat_id, type="document",
                          file_path="-1001/report.pdf", file_name="report.pdf", file_size=50000,
                          mime_type="application/pdf", downloaded=1))
        # Undownloaded — must never appear
        session.add(Media(id="photo_hidden", message_id=1, chat_id=chat_id, type="photo",
                          file_path=None, file_name=None, file_size=None, mime_type="image/jpeg", downloaded=0))
        await session.commit()

    return DatabaseAdapter(db_manager)


class TestGetMediaPaginated:
    async def test_returns_all_downloaded(self, adapter):
        result = await adapter.get_media_paginated(-1001)
        assert len(result["items"]) == 5
        assert result["has_more"] is False

    async def test_excludes_undownloaded(self, adapter):
        ids = [i["id"] for i in (await adapter.get_media_paginated(-1001))["items"]]
        assert "photo_hidden" not in ids

    async def test_filters_single_type(self, adapter):
        result = await adapter.get_media_paginated(-1001, media_types=["photo"])
        assert len(result["items"]) == 3
        assert all(i["type"] == "photo" for i in result["items"])

    async def test_filters_multiple_types(self, adapter):
        result = await adapter.get_media_paginated(-1001, media_types=["photo", "video"])
        assert {i["type"] for i in result["items"]} == {"photo", "video"}
        assert len(result["items"]) == 4

    async def test_ordered_date_desc(self, adapter):
        dates = [i["message_date"] for i in (await adapter.get_media_paginated(-1001))["items"]]
        assert dates == sorted(dates, reverse=True)

    async def test_limit_sets_has_more(self, adapter):
        result = await adapter.get_media_paginated(-1001, limit=2)
        assert len(result["items"]) == 2
        assert result["has_more"] is True

    async def test_cursor_no_overlap(self, adapter):
        page1 = await adapter.get_media_paginated(-1001, limit=3)
        assert page1["has_more"] is True
        page2 = await adapter.get_media_paginated(-1001, limit=3, before_id=page1["items"][-1]["id"])
        all_ids = [i["id"] for i in page1["items"]] + [i["id"] for i in page2["items"]]
        assert len(all_ids) == len(set(all_ids)) == 5

    async def test_cursor_album_paginates_uniquely(self, adapter):
        # Walk one item at a time through the album-bearing message_id=2.
        p1 = await adapter.get_media_paginated(-1001, limit=1)
        p2 = await adapter.get_media_paginated(-1001, limit=1, before_id=p1["items"][0]["id"])
        p3 = await adapter.get_media_paginated(-1001, limit=1, before_id=p2["items"][0]["id"])
        ids = [p1["items"][0]["id"], p2["items"][0]["id"], p3["items"][0]["id"]]
        assert len(ids) == len(set(ids))

    async def test_cursor_missing_returns_empty(self, adapter):
        result = await adapter.get_media_paginated(-1001, before_id="nope")
        assert result == {"items": [], "has_more": False}

    async def test_empty_chat(self, adapter):
        assert await adapter.get_media_paginated(-9999) == {"items": [], "has_more": False}

    async def test_sender_name_full(self, adapter):
        result = await adapter.get_media_paginated(-1001, media_types=["photo"])
        assert all(i["sender_name"] == "Alice Smith" for i in result["items"])

    async def test_sender_name_first_only(self, adapter):
        result = await adapter.get_media_paginated(-1001, media_types=["video"])
        assert result["items"][0]["sender_name"] == "Bob"

    async def test_sender_name_null(self, adapter):
        result = await adapter.get_media_paginated(-1001, media_types=["document"])
        assert result["items"][0]["sender_name"] is None

    async def test_item_fields(self, adapter):
        item = (await adapter.get_media_paginated(-1001, media_types=["video"]))["items"][0]
        assert item["id"] == "video_3"
        assert item["message_id"] == 3
        assert item["chat_id"] == -1001
        assert item["type"] == "video"
        assert item["file_path"] == "-1001/video_3.mp4"
        assert item["file_name"] == "video_3.mp4"
        assert item["file_size"] == 5000000
        assert item["mime_type"] == "video/mp4"
        assert item["width"] == 1280
        assert item["height"] == 720
        assert item["duration"] == 30
        assert item["message_date"] is not None


class TestGetMediaCounts:
    async def test_counts_by_type(self, adapter):
        counts = await adapter.get_media_counts(-1001)
        assert counts == {"photo": 3, "video": 1, "document": 1}

    async def test_excludes_undownloaded(self, adapter):
        assert sum((await adapter.get_media_counts(-1001)).values()) == 5

    async def test_empty_chat(self, adapter):
        assert await adapter.get_media_counts(-9999) == {}


# ---------------------------------------------------------------------------
# Endpoint tests (require_auth overridden for direct ACL/path-logic coverage)
# ---------------------------------------------------------------------------


def _gallery_items():
    """Two valid items (image + video) plus a traversal item."""
    return {
        "items": [
            {"id": "img1", "message_id": 1, "chat_id": -1001, "type": "photo",
             "file_path": "-1001/photo_1.jpg", "file_name": "photo_1.jpg", "file_size": 100,
             "mime_type": "image/jpeg", "width": 1, "height": 1, "duration": None,
             "message_date": "2026-01-01T00:00:00", "sender_name": "Alice"},
            {"id": "vid1", "message_id": 2, "chat_id": -1001, "type": "video",
             "file_path": "-1001/clip.mp4", "file_name": "clip.mp4", "file_size": 200,
             "mime_type": "video/mp4", "width": 2, "height": 2, "duration": 5,
             "message_date": "2026-01-02T00:00:00", "sender_name": "Bob"},
            {"id": "evil", "message_id": 3, "chat_id": -1001, "type": "photo",
             "file_path": "../../etc/passwd", "file_name": "passwd", "file_size": 1,
             "mime_type": "image/jpeg", "width": None, "height": None, "duration": None,
             "message_date": "2026-01-03T00:00:00", "sender_name": None},
        ],
        "has_more": False,
    }


@pytest.fixture
def gallery_client():
    """TestClient with deps.db mocked and require_auth overridable per-test.

    Yields (client, main_mod, deps, set_user) where set_user(UserContext) installs
    a dependency override for require_auth.
    """
    from unittest.mock import AsyncMock

    import src.web.dependencies as deps
    import src.web.main as main_mod
    from src.web.dependencies import UserContext, require_auth

    db = AsyncMock()
    db.get_media_paginated = AsyncMock(return_value=_gallery_items())
    db.get_media_counts = AsyncMock(return_value={"photo": 2, "video": 1})
    deps.db = db
    deps.config = main_mod.config
    main_mod.db = db
    deps._media_root = None

    app = main_mod.app

    def set_user(user: UserContext):
        app.dependency_overrides[require_auth] = lambda: user

    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, main_mod, deps, set_user, UserContext
    finally:
        app.dependency_overrides.pop(require_auth, None)


class TestMediaEndpoint:
    def test_master_gets_items_with_urls(self, gallery_client):
        client, _, _, set_user, UserContext = gallery_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        resp = client.get("/api/chats/-1001/media")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data and "has_more" in data
        # image extension -> thumb_url
        img = data["items"][0]
        assert img["thumb_url"] == "/media/thumb/200/-1001/photo_1.jpg"
        assert img["media_url"] == "/media/-1001/photo_1.jpg"
        # video extension also gets a thumb_url (THUMBNAIL_EXTENSIONS includes video)
        vid = data["items"][1]
        assert vid["thumb_url"] == "/media/thumb/200/-1001/clip.mp4"
        assert vid["media_url"] == "/media/-1001/clip.mp4"

    def test_traversal_item_stripped(self, gallery_client):
        client, _, _, set_user, UserContext = gallery_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        evil = client.get("/api/chats/-1001/media").json()["items"][2]
        assert evil["thumb_url"] is None
        assert "file_path" not in evil
        assert "media_url" not in evil

    def test_types_filter_forwarded(self, gallery_client):
        client, _, deps, set_user, UserContext = gallery_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        client.get("/api/chats/-1001/media?types=photo,video&limit=20&before_id=abc")
        deps.db.get_media_paginated.assert_called_once_with(
            -1001, media_types=["photo", "video"], limit=20, before_id="abc"
        )

    def test_empty_types_means_all(self, gallery_client):
        client, _, deps, set_user, UserContext = gallery_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        client.get("/api/chats/-1001/media")
        deps.db.get_media_paginated.assert_called_once_with(
            -1001, media_types=None, limit=50, before_id=None
        )

    def test_acl_forbidden_returns_403(self, gallery_client):
        client, _, _, set_user, UserContext = gallery_client
        set_user(UserContext(username="v", role="viewer", allowed_chat_ids={-1002}))
        resp = client.get("/api/chats/-1001/media")
        assert resp.status_code == 403

    def test_acl_allowed_chat_ok(self, gallery_client):
        client, _, _, set_user, UserContext = gallery_client
        set_user(UserContext(username="v", role="viewer", allowed_chat_ids={-1001}))
        resp = client.get("/api/chats/-1001/media")
        assert resp.status_code == 200

    def test_no_download_strips_file_path(self, gallery_client):
        client, _, _, set_user, UserContext = gallery_client
        set_user(UserContext(username="v", role="viewer", allowed_chat_ids={-1001}, no_download=True))
        items = client.get("/api/chats/-1001/media").json()["items"]
        # valid items keep thumb_url but lose file_path and never get media_url
        assert items[0]["thumb_url"] == "/media/thumb/200/-1001/photo_1.jpg"
        assert "file_path" not in items[0]
        assert "media_url" not in items[0]


class TestMediaCountsEndpoint:
    def test_returns_counts(self, gallery_client):
        client, _, _, set_user, UserContext = gallery_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        resp = client.get("/api/chats/-1001/media/counts")
        assert resp.status_code == 200
        assert resp.json() == {"photo": 2, "video": 1}

    def test_acl_forbidden_returns_403(self, gallery_client):
        client, _, _, set_user, UserContext = gallery_client
        set_user(UserContext(username="v", role="viewer", allowed_chat_ids={-1002}))
        resp = client.get("/api/chats/-1001/media/counts")
        assert resp.status_code == 403
