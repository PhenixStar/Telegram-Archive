"""Why a media row is not downloaded: recorded, reconciled, shown (port of upstream #474)."""

from datetime import datetime

import pytest
from sqlalchemy import insert, select

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Media

MB = 1024 * 1024


@pytest.fixture
async def adapter(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'skip.db'}")
    await manager.init()
    try:
        yield DatabaseAdapter(manager)
    finally:
        await manager.close()


async def _seed(adapter, rows):
    for row in rows:
        await adapter.insert_message(
            {"id": row["message_id"], "chat_id": row["chat_id"], "date": datetime(2026, 1, 1), "text": ""}
        )
    async with adapter.db_manager.async_session_factory() as session:
        await session.execute(insert(Media), rows)
        await session.commit()


async def _reasons(adapter) -> dict[str, str | None]:
    async with adapter.db_manager.async_session_factory() as session:
        return dict((await session.execute(select(Media.id, Media.skip_reason))).all())


def _row(media_id, message_id, size, downloaded=0, reason=None):
    return {
        "id": media_id,
        "chat_id": 1,
        "message_id": message_id,
        "type": "video",
        "file_size": size,
        "downloaded": downloaded,
        "skip_reason": reason,
    }


@pytest.mark.asyncio
async def test_reconcile_classifies_unclears_and_leaves_unavailable_alone(adapter):
    await _seed(
        adapter,
        [
            _row("big", 1, 900 * MB),  # legacy row over the cap
            _row("small", 2, 10 * MB),  # genuine pending download
            _row("relaxed", 3, 300 * MB, reason="oversize"),  # cap was raised since
            _row("gone", 4, 900 * MB, reason="unavailable"),
            _row("filtered", 5, 1 * MB, reason="filtered"),
            _row("done", 6, 1 * MB, downloaded=1, reason="oversize"),  # stale
        ],
    )

    counts = await adapter.reconcile_media_skip_reasons(500 * MB, filters_active=False)

    assert await _reasons(adapter) == {
        "big": "oversize",
        "small": None,
        "relaxed": None,
        "gone": "unavailable",
        "filtered": None,
        "done": None,
    }
    assert counts == {"stale": 1, "cleared": 1, "oversize": 1, "unfiltered": 1}


@pytest.mark.asyncio
async def test_filter_reasons_stay_while_a_filter_is_configured(adapter):
    await _seed(adapter, [_row("filtered", 5, 1 * MB, reason="filtered")])
    await adapter.reconcile_media_skip_reasons(500 * MB, filters_active=True)
    assert (await _reasons(adapter))["filtered"] == "filtered"


@pytest.mark.asyncio
async def test_mark_unavailable_touches_only_undownloaded_rows(adapter):
    await _seed(adapter, [_row("a", 1, 0), _row("b", 2, 5, downloaded=1)])
    marked = await adapter.mark_media_unavailable(1, [1, 2])
    assert marked == 1
    assert await _reasons(adapter) == {"a": "unavailable", "b": None}


@pytest.mark.asyncio
async def test_a_successful_download_clears_the_reason(adapter):
    await _seed(adapter, [_row("1_1_video", 1, 900 * MB, reason="oversize")])
    await adapter.insert_media(
        {"id": "1_1_video", "chat_id": 1, "message_id": 1, "type": "video", "file_path": "/x", "downloaded": True}
    )
    assert (await _reasons(adapter))["1_1_video"] is None


@pytest.mark.asyncio
async def test_the_viewer_payload_carries_the_reason(adapter):
    await _seed(adapter, [_row("1_1_video", 1, 900 * MB, reason="oversize")])
    messages = await adapter.get_messages_paginated(chat_id=1, limit=10)
    assert messages[0]["media"]["skip_reason"] == "oversize"


def test_catch_up_query_skips_rows_that_can_never_download():
    import importlib.util
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "refetch_incomplete_messages.py")
    spec = importlib.util.spec_from_file_location("refetch_for_skip_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "'unavailable'" in module.SKIPPED_MEDIA_SQL and "'filtered'" in module.SKIPPED_MEDIA_SQL


@pytest.mark.asyncio
async def test_row_unfiltered_while_still_over_the_cap_is_classified_in_the_same_pass(adapter):
    await _seed(adapter, [_row("big-filtered", 1, 900 * MB, reason="filtered")])
    await adapter.reconcile_media_skip_reasons(500 * MB, filters_active=False)
    assert (await _reasons(adapter))["big-filtered"] == "oversize"
