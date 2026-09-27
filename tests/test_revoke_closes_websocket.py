"""Revoking a share token (or a user's sessions) closes the live sockets they opened."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.web import dependencies as deps


class _Socket:
    def __init__(self):
        self.closed_with = None
        self.accept = AsyncMock()
        self.headers = {}
        self.client = SimpleNamespace(host="10.0.0.1")

    async def close(self, code=1000, reason=""):
        self.closed_with = code


@pytest.mark.asyncio
async def test_revoking_a_token_closes_only_its_sockets(monkeypatch):
    manager = deps.ConnectionManager()
    monkeypatch.setattr(deps, "manager", manager)
    monkeypatch.setattr(deps, "db", None)
    revoked, other = _Socket(), _Socket()
    await manager.connect(revoked, allowed_chat_ids={1}, session_token="tok-revoked")
    await manager.connect(other, allowed_chat_ids=None, session_token="tok-other")
    deps._sessions["tok-revoked"] = deps.SessionData(username="share", role="token", source_token_id=7)
    deps._sessions["tok-other"] = deps.SessionData(username="alaa", role="super_admin")
    try:
        await deps._invalidate_token_sessions(7)
    finally:
        deps._sessions.pop("tok-revoked", None)
        deps._sessions.pop("tok-other", None)

    assert revoked.closed_with == 4001
    assert other.closed_with is None
    assert revoked not in manager.active_connections and other in manager.active_connections


@pytest.mark.asyncio
async def test_invalidating_a_user_closes_its_sockets(monkeypatch):
    manager = deps.ConnectionManager()
    monkeypatch.setattr(deps, "manager", manager)
    monkeypatch.setattr(deps, "db", None)
    socket = _Socket()
    await manager.connect(socket, session_token="tok-viewer")
    deps._sessions["tok-viewer"] = deps.SessionData(username="viewer1", role="viewer")
    try:
        await deps._invalidate_user_sessions("viewer1")
    finally:
        deps._sessions.pop("tok-viewer", None)
    assert socket.closed_with == 4001 and not manager.active_connections
