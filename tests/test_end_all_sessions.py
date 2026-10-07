"""Ending every viewer session at once, and dropping sessions ended by another viewer."""

import inspect
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.web import dependencies as deps
from src.web import routes_admin_sessions as admin_sessions


@pytest.fixture
async def adapter(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'sessions.db'}")
    await manager.init()
    try:
        yield DatabaseAdapter(manager)
    finally:
        await manager.close()


async def _login(adapter, token, username, role):
    """A session as _create_session leaves it: a database row plus the cache entry."""
    now = time.time()
    await adapter.save_session(
        token=token, username=username, role=role, allowed_chat_ids=None, created_at=now, last_accessed=now
    )
    deps._sessions[token] = deps.SessionData(username=username, role=role, created_at=now, last_accessed=now)


async def _tokens(adapter):
    return {row["token"] for row in await adapter.load_all_sessions()}


@pytest.fixture(autouse=True)
def _clean_cache():
    deps._sessions.clear()
    yield
    deps._sessions.clear()


@pytest.fixture
def sockets(monkeypatch):
    fake = SimpleNamespace(close_sessions=AsyncMock(return_value=0))
    monkeypatch.setattr(deps, "manager", fake)
    return fake


def _app(user):
    app = FastAPI()
    app.include_router(admin_sessions.router)
    # Override the require_auth that require_master actually depends on: other test
    # modules reload src.web.dependencies, which rebinds deps.require_auth.
    require_auth = inspect.signature(admin_sessions.require_master).parameters["user"].default.dependency
    app.dependency_overrides[require_auth] = lambda: user
    return app


def _client(app, cookie=None):
    cookies = {deps.AUTH_COOKIE_NAME: cookie} if cookie else None
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", cookies=cookies)


class TestDeleteAllSessions:
    @pytest.mark.asyncio
    async def test_deletes_every_row_and_reports_each(self, adapter):
        await _login(adapter, "t1", "boss", "super_admin")
        await _login(adapter, "t2", "guest", "viewer")
        deleted = await adapter.delete_all_sessions()
        assert sorted(deleted) == [("t1", "boss"), ("t2", "guest")]
        assert await _tokens(adapter) == set()

    @pytest.mark.asyncio
    async def test_keep_token_survives(self, adapter):
        await _login(adapter, "t1", "boss", "super_admin")
        await _login(adapter, "t2", "boss", "super_admin")
        deleted = await adapter.delete_all_sessions(keep_token="t1")
        assert deleted == [("t2", "boss")]
        assert await _tokens(adapter) == {"t1"}

    @pytest.mark.asyncio
    async def test_dialect_without_returning_reads_then_deletes_in_chunks(self, adapter, monkeypatch):
        monkeypatch.setattr(adapter.db_manager.engine.dialect, "delete_returning", False)
        monkeypatch.setattr(type(adapter), "_SESSION_DELETE_CHUNK", 2)
        for i in range(5):
            await _login(adapter, f"t{i}", "guest", "viewer")
        deleted = await adapter.delete_all_sessions(keep_token="t0")
        assert sorted(token for token, _ in deleted) == ["t1", "t2", "t3", "t4"]
        assert await _tokens(adapter) == {"t0"}


class TestEndAllRoute:
    @pytest.mark.asyncio
    async def test_ends_every_session_including_the_callers(self, adapter, sockets, monkeypatch):
        monkeypatch.setattr(deps, "db", adapter)
        await _login(adapter, "mine", "boss", "super_admin")
        await _login(adapter, "other", "guest", "viewer")
        user = deps.UserContext(username="boss", role="super_admin")

        async with _client(_app(user), cookie="mine") as client:
            response = await client.post("/api/admin/sessions/end-all")

        assert response.status_code == 200
        assert response.json() == {"success": True, "ended": 2, "current_session_ended": True}
        assert deps.AUTH_COOKIE_NAME in response.headers["set-cookie"]  # cleared
        assert await _tokens(adapter) == set()
        assert deps._sessions == {}
        sockets.close_sessions.assert_awaited_once_with({"mine", "other"})
        logs = await adapter.get_audit_logs(limit=5)
        assert logs[0]["action"] == "sessions_ended_all"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("how", ["query", "body"])
    async def test_keep_current_keeps_only_the_callers_session(self, adapter, sockets, monkeypatch, how):
        monkeypatch.setattr(deps, "db", adapter)
        await _login(adapter, "mine", "boss", "super_admin")
        await _login(adapter, "my-phone", "boss", "super_admin")
        await _login(adapter, "other", "guest", "viewer")
        user = deps.UserContext(username="boss", role="super_admin")

        async with _client(_app(user), cookie="mine") as client:
            if how == "query":
                response = await client.post("/api/admin/sessions/end-all?keep_current=true")
            else:
                response = await client.post("/api/admin/sessions/end-all", json={"keep_current": True})

        assert response.json() == {"success": True, "ended": 2, "current_session_ended": False}
        assert "set-cookie" not in response.headers
        assert await _tokens(adapter) == {"mine"}
        assert set(deps._sessions) == {"mine"}
        logs = await adapter.get_audit_logs(limit=5)
        assert logs[0]["action"] == "sessions_ended_all:kept_current"

    @pytest.mark.asyncio
    async def test_proxy_admin_without_a_session_ends_everything_else(self, adapter, sockets, monkeypatch):
        monkeypatch.setattr(deps, "db", adapter)
        await _login(adapter, "other", "guest", "viewer")
        user = deps.UserContext(username="proxy-boss", role="master")

        async with _client(_app(user)) as client:
            response = await client.post("/api/admin/sessions/end-all?keep_current=true")

        assert response.json() == {"success": True, "ended": 1, "current_session_ended": False}
        assert await _tokens(adapter) == set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", ["admin", "viewer", "token"])
    async def test_roles_below_master_are_refused(self, adapter, sockets, monkeypatch, role):
        monkeypatch.setattr(deps, "db", adapter)
        await _login(adapter, "other", "guest", "viewer")
        async with _client(_app(deps.UserContext(username="u", role=role))) as client:
            response = await client.post("/api/admin/sessions/end-all")
        assert response.status_code == 403
        assert await _tokens(adapter) == {"other"}

    @pytest.mark.asyncio
    async def test_viewer_only_header_is_refused(self, adapter, sockets, monkeypatch):
        monkeypatch.setattr(deps, "db", adapter)
        user = deps.UserContext(username="boss", role="super_admin")
        async with _client(_app(user)) as client:
            response = await client.post("/api/admin/sessions/end-all", headers={"X-Viewer-Only": "true"})
        assert response.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [b"not json", b"[1]", b'{"keep_current": "yes"}'])
    async def test_malformed_body_is_400_and_ends_nothing(self, adapter, sockets, monkeypatch, body):
        monkeypatch.setattr(deps, "db", adapter)
        await _login(adapter, "other", "guest", "viewer")
        user = deps.UserContext(username="boss", role="super_admin")
        async with _client(_app(user)) as client:
            response = await client.post("/api/admin/sessions/end-all", content=body)
        assert response.status_code == 400
        assert await _tokens(adapter) == {"other"}

    @pytest.mark.asyncio
    async def test_failed_audit_write_still_ends_the_sessions(self, adapter, sockets, monkeypatch):
        monkeypatch.setattr(deps, "db", adapter)
        monkeypatch.setattr(adapter, "create_audit_log", AsyncMock(side_effect=RuntimeError("disk full")))
        await _login(adapter, "mine", "boss", "super_admin")
        user = deps.UserContext(username="boss", role="super_admin")
        async with _client(_app(user), cookie="mine") as client:
            response = await client.post("/api/admin/sessions/end-all")
        assert response.status_code == 200
        assert response.json()["current_session_ended"] is True


class TestRevalidation:
    """A session ended by another viewer process stops working here too."""

    @pytest.mark.asyncio
    async def test_fresh_cache_entry_is_not_reread(self, monkeypatch):
        db = SimpleNamespace(get_session=AsyncMock(return_value=None))
        monkeypatch.setattr(deps, "db", db)
        deps._sessions["t"] = deps.SessionData(username="u", role="viewer")
        assert await deps._resolve_session("t") is deps._sessions["t"]
        db.get_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_entry_whose_row_is_gone_is_dropped(self, adapter, sockets, monkeypatch):
        monkeypatch.setattr(deps, "db", adapter)
        await _login(adapter, "t", "guest", "viewer")
        deps._sessions["t"].validated_at = 0
        await adapter.delete_session("t")  # ended by the other viewer

        assert await deps._resolve_session("t") is None
        assert "t" not in deps._sessions
        sockets.close_sessions.assert_awaited_once_with({"t"})

    @pytest.mark.asyncio
    async def test_stale_entry_whose_row_exists_is_kept_and_refreshed(self, adapter, sockets, monkeypatch):
        monkeypatch.setattr(deps, "db", adapter)
        await _login(adapter, "t", "guest", "viewer")
        deps._sessions["t"].validated_at = 0
        session = await deps._resolve_session("t")
        assert session is deps._sessions["t"]
        assert time.time() - session.validated_at < 5

    @pytest.mark.asyncio
    async def test_database_error_keeps_the_session_and_retries_soon(self, monkeypatch):
        db = SimpleNamespace(get_session=AsyncMock(side_effect=RuntimeError("locked")))
        monkeypatch.setattr(deps, "db", db)
        deps._sessions["t"] = deps.SessionData(username="u", role="viewer", validated_at=0)

        session = await deps._resolve_session("t")

        assert session is deps._sessions["t"]
        due_in = session.validated_at + deps._SESSION_REVALIDATE_SECONDS - time.time()
        assert 0 < due_in <= deps._SESSION_REVALIDATE_RETRY_SECONDS

    @pytest.mark.asyncio
    async def test_sweep_drops_socket_only_sessions_whose_rows_are_gone(self, adapter, sockets, monkeypatch):
        monkeypatch.setattr(deps, "db", adapter)
        await _login(adapter, "alive", "boss", "super_admin")
        await _login(adapter, "ended", "guest", "viewer")
        await _login(adapter, "fresh", "guest", "viewer")
        deps._sessions["alive"].validated_at = 0
        deps._sessions["ended"].validated_at = 0
        await adapter.delete_session("ended")
        await adapter.delete_session("fresh")  # not stale yet: left for its next check

        await deps._revalidate_all_cached_sessions()

        assert set(deps._sessions) == {"alive", "fresh"}
        assert time.time() - deps._sessions["alive"].validated_at < 5
        sockets.close_sessions.assert_awaited_once_with({"ended"})

    @pytest.mark.asyncio
    async def test_sweep_keeps_everything_when_the_table_cannot_be_read(self, monkeypatch):
        db = SimpleNamespace(load_all_sessions=AsyncMock(side_effect=RuntimeError("down")))
        monkeypatch.setattr(deps, "db", db)
        deps._sessions["t"] = deps.SessionData(username="u", role="viewer", validated_at=0)
        await deps._revalidate_all_cached_sessions()
        assert "t" in deps._sessions
