"""/api/stats never leaks per-chat maps and scopes a restricted viewer's figures."""

from src.db.adapter_sync import PER_CHAT_STATS_KEYS
from src.web.routes_chat import _scope_stats_to_user


def _blob() -> dict:
    return {
        "chats": 3,
        "messages": 600,
        "media_files": 30,
        "total_size_mb": 90.0,
        "per_chat_message_counts": {"1": 100, "2": 200, "-100": 300},
        "per_chat_media_counts": {"1": 10, "-100": 20},
        "per_chat_media_bytes": {"1": 1048576 * 3, "-100": 1048576 * 7},
    }


def test_full_access_keeps_totals_but_never_the_maps():
    stats = _blob()
    _scope_stats_to_user(stats, None)
    assert (stats["chats"], stats["messages"], stats["media_files"], stats["total_size_mb"]) == (3, 600, 30, 90.0)
    assert not any(key in stats for key in PER_CHAT_STATS_KEYS)


def test_restricted_viewer_sees_only_its_own_chats():
    stats = _blob()
    _scope_stats_to_user(stats, {1, 2})
    assert stats["chats"] == 2
    assert stats["messages"] == 300
    assert stats["media_files"] == 10
    assert stats["total_size_mb"] == 3.0
    assert not any(key in stats for key in PER_CHAT_STATS_KEYS)


def test_blob_without_media_maps_hides_media_figures_instead_of_leaking_totals():
    stats = _blob()
    del stats["per_chat_media_counts"], stats["per_chat_media_bytes"]
    _scope_stats_to_user(stats, {1})
    assert stats["messages"] == 100
    assert "media_files" not in stats and "total_size_mb" not in stats


def test_malformed_entries_are_skipped_fail_closed():
    stats = _blob()
    stats["per_chat_message_counts"] = {"1": 100, "not-a-chat": 5, "2": True, "3": "7"}
    _scope_stats_to_user(stats, {1, 2, 3})
    assert (stats["chats"], stats["messages"]) == (1, 100)


def test_missing_message_map_scopes_to_nothing():
    stats = _blob()
    del stats["per_chat_message_counts"]
    _scope_stats_to_user(stats, {1})
    assert (stats["chats"], stats["messages"]) == (0, 0)


# --- the calculation that feeds it (real SQLite) -----------------------------

import pytest  # noqa: E402
from sqlalchemy import insert  # noqa: E402

from src.db.adapter import DatabaseAdapter  # noqa: E402
from src.db.base import DatabaseManager  # noqa: E402
from src.db.models import Media  # noqa: E402


@pytest.fixture
async def sqlite_adapter(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'stats.db'}")
    await manager.init()
    try:
        yield DatabaseAdapter(manager)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_one_scan_yields_totals_and_per_chat_media_maps(sqlite_adapter):
    from datetime import datetime

    for chat_id, message_id in ((1, 1), (1, 2), (2, 1)):
        await sqlite_adapter.insert_message(
            {"id": message_id, "chat_id": chat_id, "date": datetime(2026, 1, 1), "text": "x"}
        )
    rows = [
        {"id": "1_1_photo", "chat_id": 1, "message_id": 1, "type": "photo", "file_size": 1000, "downloaded": 1},
        {"id": "1_2_video", "chat_id": 1, "message_id": 2, "type": "video", "file_size": 3000, "downloaded": 1},
        {"id": "2_1_video", "chat_id": 2, "message_id": 1, "type": "video", "file_size": 9999, "downloaded": 0},
        {"id": "orphan", "chat_id": None, "message_id": None, "type": "photo", "file_size": 500, "downloaded": 1},
    ]
    async with sqlite_adapter.db_manager.async_session_factory() as session:
        await session.execute(insert(Media), rows)
        await session.commit()

    stats = await sqlite_adapter.calculate_and_store_statistics()

    # Totals include the row with no chat id; undownloaded rows are not counted.
    assert stats["media_files"] == 3
    assert stats["per_chat_media_counts"] == {"1": 2}
    assert stats["per_chat_media_bytes"] == {"1": 4000}
    assert stats["per_chat_message_counts"] == {1: 2, 2: 1}
