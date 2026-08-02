"""Tests for service-action metadata capture during historical backfill (#191).

Regular messages carry ``action=None``; only service messages (joins, photo
changes, forum topic create/edit, …) carry a ``MessageAction``. The backfill
must preserve that metadata in ``raw_data`` so archived service events are not
reduced to blank bubbles.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl.types import MessageActionChatEditTitle

from src.backup_extraction import _service_action_type
from src.telegram_backup import TelegramBackup


def test_service_action_type_normalizes_class_names():
    assert _service_action_type(MessageActionChatEditTitle(title="x")) == "chat_edit_title"
    # A bare object whose class name has no MessageAction prefix still normalizes.
    class MessageActionTopicCreate:  # noqa: N801 - mimic telethon class name
        pass

    assert _service_action_type(MessageActionTopicCreate()) == "topic_create"


def _mock_message(**overrides):
    """A minimal message that skips sender/media/reactions/poll branches."""
    base = dict(
        id=42,
        sender=None,
        sender_id=1,
        date=datetime(2026, 1, 1),
        text="",
        reply_to=None,
        reply_to_msg_id=None,
        edit_date=None,
        out=False,
        pinned=False,
        grouped_id=None,
        media=None,
        reactions=None,
        action=None,
        fwd_from=None,
        post_author=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _backup():
    config = MagicMock()
    return TelegramBackup(config, AsyncMock())


@pytest.mark.asyncio
async def test_process_message_captures_service_action():
    backup = _backup()
    msg = _mock_message(action=MessageActionChatEditTitle(title="New Room Name"))

    data = await backup._process_message(msg, -100123)

    assert data["raw_data"]["service_type"] == "service"
    assert data["raw_data"]["action_type"] == "chat_edit_title"
    assert data["raw_data"]["new_title"] == "New Room Name"


@pytest.mark.asyncio
async def test_process_message_regular_message_has_no_service_metadata():
    backup = _backup()
    msg = _mock_message(action=None, text="hello")

    data = await backup._process_message(msg, -100123)

    assert "service_type" not in data["raw_data"]
    assert "action_type" not in data["raw_data"]
