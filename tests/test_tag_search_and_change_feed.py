"""Tests for the ported tag search and what-changed feed (adapter + endpoints).

Covers:
  * DatabaseAdapter.search_messages_by_tag — word-boundary match, cashtag
    case-sensitivity, chat/outgoing scoping, allowed_chat_ids ACL
  * DatabaseAdapter.get_recent_changes     — deleted + edited streams,
    since/before window, allowed_chat_ids ACL
  * GET /api/tags/{tag}                    — 400 on bad tag, ACL 403/200
  * GET /api/changes                       — allowed_chat_ids forwarded

Adapter tests use real in-memory SQLite to exercise the actual SQL.
Endpoint tests override require_auth so ACL logic is tested directly without
coupling to the login internals (same pattern as test_gallery_backend.py).
"""

import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock

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

    db = DatabaseAdapter(db_manager)
    async with db_manager.async_session_factory() as session:
        session.add(Chat(id=-1001, type="channel", title="Chat A"))
        session.add(Chat(id=-1002, type="group", title="Chat B"))
        await session.commit()
    return db


# ---------------------------------------------------------------------------
# TagSearchMixin.search_messages_by_tag
# ---------------------------------------------------------------------------


class TestSearchMessagesByTag:
    async def test_matches_whole_hashtag_only(self, adapter):
        await adapter.insert_message({"id": 1, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "see #tag here"})
        await adapter.insert_message(
            {"id": 2, "chat_id": -1001, "date": datetime(2026, 1, 2), "text": "see #taglonger here"}
        )
        result = await adapter.search_messages_by_tag("#tag", allowed_chat_ids=None)
        ids = [r["id"] for r in result["results"]]
        assert ids == [1]

    async def test_hashtag_search_is_case_insensitive(self, adapter):
        await adapter.insert_message({"id": 1, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "#Tag here"})
        result = await adapter.search_messages_by_tag("#tag", allowed_chat_ids=None)
        assert [r["id"] for r in result["results"]] == [1]

    async def test_cashtag_search_is_case_sensitive(self, adapter):
        await adapter.insert_message({"id": 1, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "buying $TSLA"})
        await adapter.insert_message({"id": 2, "chat_id": -1001, "date": datetime(2026, 1, 2), "text": "not $tsla"})
        result = await adapter.search_messages_by_tag("$TSLA", allowed_chat_ids=None)
        assert [r["id"] for r in result["results"]] == [1]

    async def test_newest_first(self, adapter):
        await adapter.insert_message({"id": 1, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "#tag old"})
        await adapter.insert_message({"id": 2, "chat_id": -1001, "date": datetime(2026, 1, 5), "text": "#tag new"})
        result = await adapter.search_messages_by_tag("#tag", allowed_chat_ids=None)
        assert [r["id"] for r in result["results"]] == [2, 1]

    async def test_chat_scope_narrows_to_one_chat(self, adapter):
        await adapter.insert_message({"id": 1, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "#tag in A"})
        await adapter.insert_message({"id": 2, "chat_id": -1002, "date": datetime(2026, 1, 1), "text": "#tag in B"})
        result = await adapter.search_messages_by_tag("#tag", allowed_chat_ids=None, chat_id=-1001)
        assert [r["chat_id"] for r in result["results"]] == [-1001]

    async def test_outgoing_only_is_my_messages(self, adapter):
        await adapter.insert_message(
            {"id": 1, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "#tag mine", "is_outgoing": 1}
        )
        await adapter.insert_message(
            {"id": 2, "chat_id": -1001, "date": datetime(2026, 1, 2), "text": "#tag theirs", "is_outgoing": 0}
        )
        result = await adapter.search_messages_by_tag("#tag", allowed_chat_ids=None, outgoing_only=True)
        assert [r["id"] for r in result["results"]] == [1]

    async def test_allowed_chat_ids_restricts_results(self, adapter):
        await adapter.insert_message({"id": 1, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "#tag in A"})
        await adapter.insert_message({"id": 2, "chat_id": -1002, "date": datetime(2026, 1, 1), "text": "#tag in B"})
        result = await adapter.search_messages_by_tag("#tag", allowed_chat_ids={-1001})
        assert [r["chat_id"] for r in result["results"]] == [-1001]

    async def test_has_more_true_when_extra_row_exists(self, adapter):
        for i in range(3):
            await adapter.insert_message({"id": i, "chat_id": -1001, "date": datetime(2026, 1, 1 + i), "text": "#tag"})
        result = await adapter.search_messages_by_tag("#tag", allowed_chat_ids=None, limit=2)
        assert len(result["results"]) == 2
        assert result["has_more"] is True

    async def test_no_match_returns_empty(self, adapter):
        await adapter.insert_message({"id": 1, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "no tags here"})
        result = await adapter.search_messages_by_tag("#missing", allowed_chat_ids=None)
        assert result["results"] == []
        assert result["has_more"] is False
        assert result["truncated"] is False


# ---------------------------------------------------------------------------
# ChangeFeedMixin.get_recent_changes
# ---------------------------------------------------------------------------


class TestGetRecentChanges:
    async def test_deleted_message_appears_in_feed(self, adapter):
        await adapter.insert_message({"id": 1, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "gone"})
        await adapter.mark_message_deleted(-1001, 1, deleted_at=datetime(2026, 1, 2))
        changes = await adapter.get_recent_changes(allowed_chat_ids=None)
        assert len(changes) == 1
        assert changes[0]["kind"] == "deleted"
        assert changes[0]["text"] == "gone"
        assert changes[0]["chat"]["chat_id"] == -1001

    async def test_edited_message_appears_with_old_and_new_text(self, adapter):
        await adapter.insert_message({"id": 1, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "v1"})
        await adapter.update_message_text(-1001, 1, "v2", datetime(2026, 1, 2))
        changes = await adapter.get_recent_changes(allowed_chat_ids=None)
        assert len(changes) == 1
        assert changes[0]["kind"] == "edited"
        assert changes[0]["old_text"] == "v1"
        assert changes[0]["new_text"] == "v2"

    async def test_newest_first_across_both_streams(self, adapter):
        await adapter.insert_message({"id": 1, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "a"})
        await adapter.insert_message({"id": 2, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "b"})
        await adapter.mark_message_deleted(-1001, 1, deleted_at=datetime(2026, 1, 1))
        await adapter.update_message_text(-1001, 2, "b2", datetime(2026, 1, 10))
        changes = await adapter.get_recent_changes(allowed_chat_ids=None)
        assert [c["kind"] for c in changes] == ["edited", "deleted"]

    async def test_allowed_chat_ids_restricts_both_streams(self, adapter):
        await adapter.insert_message({"id": 1, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "a"})
        await adapter.insert_message({"id": 2, "chat_id": -1002, "date": datetime(2026, 1, 1), "text": "b"})
        await adapter.mark_message_deleted(-1001, 1, deleted_at=datetime(2026, 1, 1))
        await adapter.mark_message_deleted(-1002, 2, deleted_at=datetime(2026, 1, 1))
        changes = await adapter.get_recent_changes(allowed_chat_ids={-1001})
        assert len(changes) == 1
        assert changes[0]["chat"]["chat_id"] == -1001

    async def test_since_window_excludes_older_changes(self, adapter):
        await adapter.insert_message({"id": 1, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "a"})
        await adapter.mark_message_deleted(-1001, 1, deleted_at=datetime(2026, 1, 1))
        changes = await adapter.get_recent_changes(allowed_chat_ids=None, since=datetime(2026, 1, 5))
        assert changes == []

    async def test_before_cursor_pages_older_rows(self, adapter):
        await adapter.insert_message({"id": 1, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "a"})
        await adapter.insert_message({"id": 2, "chat_id": -1001, "date": datetime(2026, 1, 1), "text": "b"})
        await adapter.mark_message_deleted(-1001, 1, deleted_at=datetime(2026, 1, 1))
        await adapter.mark_message_deleted(-1001, 2, deleted_at=datetime(2026, 1, 10))
        first_page = await adapter.get_recent_changes(allowed_chat_ids=None, limit=1)
        assert len(first_page) == 1
        cursor = datetime.fromisoformat(first_page[0]["date"])
        second_page = await adapter.get_recent_changes(allowed_chat_ids=None, before=cursor)
        assert len(second_page) == 1
        assert second_page[0]["message_id"] != first_page[0]["message_id"]


# ---------------------------------------------------------------------------
# Endpoint tests (require_auth overridden for direct ACL coverage)
# ---------------------------------------------------------------------------


@pytest.fixture
def changes_client():
    """TestClient with deps.db mocked and require_auth overridable per-test.

    Overrides through ``routes_changes.require_auth`` rather than
    ``dependencies.require_auth`` directly: another test module elsewhere in
    the suite reloads ``dependencies`` (rebinding its ``require_auth`` to a
    new function object), but never reloads ``routes_changes``, so the two
    names can drift apart depending on test execution order.
    ``routes_changes.require_auth`` is exactly the object its own
    ``Depends(require_auth)`` calls were bound to, so the override always
    matches regardless of what else ran first.
    """
    from unittest.mock import AsyncMock

    import src.web.dependencies as deps
    import src.web.main as main_mod
    import src.web.routes_changes as routes_changes
    from src.web.dependencies import UserContext

    db = AsyncMock()
    db.search_messages_by_tag = AsyncMock(return_value={"results": [], "has_more": False, "truncated": False})
    db.get_recent_changes = AsyncMock(return_value=[])
    deps.db = db
    deps.config = main_mod.config
    main_mod.db = db

    app = main_mod.app

    def set_user(user: UserContext):
        app.dependency_overrides[routes_changes.require_auth] = lambda: user

    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, deps, set_user, UserContext
    finally:
        app.dependency_overrides.pop(routes_changes.require_auth, None)


class TestTagSearchEndpoint:
    def test_rejects_unrecognizable_tag(self, changes_client):
        client, _, set_user, UserContext = changes_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        resp = client.get("/api/tags/notatag")
        assert resp.status_code == 400

    def test_accepts_hashtag_and_cashtag(self, changes_client):
        client, _, set_user, UserContext = changes_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        assert client.get("/api/tags/%23tag").status_code == 200
        assert client.get("/api/tags/%24TSLA").status_code == 200

    def test_scope_chat_requires_chat_id(self, changes_client):
        client, _, set_user, UserContext = changes_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        resp = client.get("/api/tags/%23tag?scope=chat")
        assert resp.status_code == 400

    def test_scope_chat_acl_forbidden(self, changes_client):
        client, _, set_user, UserContext = changes_client
        set_user(UserContext(username="v", role="viewer", allowed_chat_ids={-1002}))
        resp = client.get("/api/tags/%23tag?scope=chat&chat_id=-1001")
        assert resp.status_code == 403

    def test_scope_chat_acl_allowed(self, changes_client):
        client, _, set_user, UserContext = changes_client
        set_user(UserContext(username="v", role="viewer", allowed_chat_ids={-1001}))
        resp = client.get("/api/tags/%23tag?scope=chat&chat_id=-1001")
        assert resp.status_code == 200

    def test_restricted_viewer_search_scoped_in_adapter_call(self, changes_client):
        client, deps, set_user, UserContext = changes_client
        set_user(UserContext(username="v", role="viewer", allowed_chat_ids={-1001}))
        client.get("/api/tags/%23tag?scope=all")
        deps.db.search_messages_by_tag.assert_called_once_with("#tag", allowed_chat_ids={-1001}, limit=50, offset=0)

    def test_mine_scope_passes_outgoing_only(self, changes_client):
        client, deps, set_user, UserContext = changes_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        client.get("/api/tags/%23tag?scope=mine")
        deps.db.search_messages_by_tag.assert_called_once_with(
            "#tag", allowed_chat_ids=None, limit=50, offset=0, outgoing_only=True
        )


class TestChangesFeedEndpoint:
    def test_master_sees_unrestricted_scope(self, changes_client):
        client, deps, set_user, UserContext = changes_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        resp = client.get("/api/changes")
        assert resp.status_code == 200
        deps.db.get_recent_changes.assert_called_once_with(since=None, before=None, limit=50, allowed_chat_ids=None)

    def test_restricted_viewer_scope_forwarded(self, changes_client):
        client, deps, set_user, UserContext = changes_client
        set_user(UserContext(username="v", role="viewer", allowed_chat_ids={-1001}))
        resp = client.get("/api/changes")
        assert resp.status_code == 200
        deps.db.get_recent_changes.assert_called_once_with(since=None, before=None, limit=50, allowed_chat_ids={-1001})

    def test_invalid_since_is_400(self, changes_client):
        client, _, set_user, UserContext = changes_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        resp = client.get("/api/changes?since=not-a-date")
        assert resp.status_code == 400

    def test_next_before_cursor_set_when_page_full(self, changes_client):
        client, deps, set_user, UserContext = changes_client
        set_user(UserContext(username="m", role="master", allowed_chat_ids=None))
        deps.db.get_recent_changes = AsyncMock(
            return_value=[{"kind": "deleted", "date": "2026-01-01T00:00:00", "chat": {}, "message_id": 1}]
        )
        resp = client.get("/api/changes?limit=1")
        assert resp.json()["next_before"] == "2026-01-01T00:00:00"
