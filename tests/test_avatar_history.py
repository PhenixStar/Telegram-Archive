"""Profile photo history (port of upstream #469/#479/#481 for one account per archive)."""

import os
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.web import dependencies as deps
from src.web import routes_chat


@pytest.fixture
async def adapter(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'avatars.db'}")
    await manager.init()
    try:
        yield DatabaseAdapter(manager)
    finally:
        await manager.close()


def _chat(photo_id="absent", **extra):
    data = {"id": 42, "type": "private", "first_name": "Ann", **extra}
    if photo_id != "absent":
        data["avatar_photo_id"] = photo_id
    return data


@pytest.mark.asyncio
async def test_history_records_changes_and_removals_only(adapter):
    await adapter.upsert_chat(_chat(photo_id=None))  # new chat, no photo: nothing to record
    assert await adapter.get_avatar_history(42) == []

    await adapter.upsert_chat(_chat(photo_id=111))
    await adapter.upsert_chat(_chat(photo_id=111))  # unchanged: no new row
    await adapter.upsert_chat(_chat(photo_id=222))
    await adapter.upsert_chat(_chat(photo_id=None))  # removal
    await adapter.upsert_chat(_chat())  # a caller that says nothing about the photo

    history = await adapter.get_avatar_history(42)
    assert [h["photo_id"] for h in history] == [None, 222, 111]
    assert (await adapter.get_chat_by_id(42))["avatar_photo_id"] is None
    assert await adapter.get_chat_ids_with_avatar_history([42, 7]) == {42}


@pytest.fixture
def avatar_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "config", SimpleNamespace(media_path=str(tmp_path), display_chat_ids=None), raising=False)
    routes_chat._avatar_cache.clear()
    folder = tmp_path / "avatars" / "users"
    folder.mkdir(parents=True)
    now = time.time()
    for offset, name in enumerate(["42_111.jpg", "42_222.jpg", "42_333.jpg"]):
        path = folder / name
        path.write_bytes(b"jpg")
        os.utime(path, (now + offset, now + offset))  # 42_333 is the newest file
    return folder


def test_recorded_photo_wins_over_the_newest_file(avatar_dir):
    assert routes_chat._find_avatar_path(42, "private", photo_id=111) == "avatars/users/42_111.jpg"
    assert routes_chat._find_avatar_path(42, "private") == "avatars/users/42_333.jpg"
    # Recorded but not downloaded yet: fall back to the newest file.
    assert routes_chat._find_avatar_path(42, "private", photo_id=999) == "avatars/users/42_333.jpg"


def test_a_recorded_removal_shows_no_avatar(avatar_dir):
    assert routes_chat._find_avatar_path(42, "private", removed=True) is None


@pytest.mark.asyncio
async def test_avatars_endpoint_lists_previous_photos_newest_first(avatar_dir, adapter, monkeypatch):
    monkeypatch.setattr(deps, "db", adapter, raising=False)
    await adapter.upsert_chat(_chat(photo_id=111))
    await adapter.upsert_chat(_chat(photo_id=222))
    user = SimpleNamespace(role="master", allowed_chat_ids=None)

    result = await routes_chat.get_chat_avatars(42, user=user)

    assert result["current"] == "/media/avatars/users/42_222.jpg"
    assert result["removed"] is False
    urls = [p["url"] for p in result["previous"]]
    assert "/media/avatars/users/42_222.jpg" not in urls
    assert set(urls) == {"/media/avatars/users/42_111.jpg", "/media/avatars/users/42_333.jpg"}
    dated = {p["photo_id"]: p["seen_at"] for p in result["previous"]}
    assert dated["111"] is not None and dated["333"] is None  # 333 was never sighted, only on disk


@pytest.mark.asyncio
async def test_avatars_endpoint_hides_chats_outside_the_viewers_scope(avatar_dir, adapter, monkeypatch):
    monkeypatch.setattr(deps, "db", adapter, raising=False)
    await adapter.upsert_chat(_chat(photo_id=111))
    restricted = SimpleNamespace(role="viewer", allowed_chat_ids={7})
    with pytest.raises(HTTPException) as exc:
        await routes_chat.get_chat_avatars(42, user=restricted)
    assert exc.value.status_code == 404
