"""Tests for forward-source resolution caching (#383) and raw provenance
capture (#400) in ``_process_message``.

#383: resolve a forward's source name from our local users/chats tables
first, then at most one ``get_entity`` API call per distinct source for the
lifetime of a backup run, with a negative cache so an unresolvable source
isn't retried (and doesn't keep risking FloodWait) within the same run.

#400: capture the raw ``fwd_from`` structure (from_id/from_name/
channel_post/post_author/date/saved_from_peer) into ``raw_data.fwd_from`` at
no extra API cost, independent of whether the name resolves.
"""

import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from telethon.tl.types import PeerChannel, PeerUser

from src.telegram_backup import TelegramBackup


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_fwd_from(from_name=None, from_id=None, channel_post=None, post_author=None, date=None):
    fwd = MagicMock(spec=["from_name", "from_id", "channel_post", "post_author", "date", "saved_from_peer"])
    fwd.from_name = from_name
    fwd.from_id = from_id
    fwd.channel_post = channel_post
    fwd.post_author = post_author
    fwd.date = date
    fwd.saved_from_peer = None
    return fwd


def _make_message(fwd_from=None, msg_id=100):
    msg = MagicMock()
    msg.id = msg_id
    msg.sender = None
    msg.sender_id = 42
    msg.date = datetime(2024, 1, 15, 12, 0, 0)
    msg.text = "fwd"
    msg.reply_to_msg_id = None
    msg.reply_to = None
    msg.edit_date = None
    msg.out = False
    msg.pinned = False
    msg.grouped_id = None
    msg.fwd_from = fwd_from
    msg.media = None
    msg.reactions = None
    msg.post_author = None
    msg.action = None
    msg.entities = None
    return msg


def _make_backup(db=None, client=None):
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.config = MagicMock()
    backup.db = db if db is not None else AsyncMock()
    backup.client = client if client is not None else AsyncMock()
    return backup


class TestForwardFromNameHiddenAccounts(unittest.TestCase):
    def test_from_name_needs_no_lookup(self):
        """Hidden/deleted-account forwards carry the name directly, no API/DB call."""
        backup = _make_backup()
        fwd = _make_fwd_from(from_name="Hidden User")

        result = _run(backup._process_message(_make_message(fwd_from=fwd), -100))

        self.assertEqual(result["raw_data"]["forward_from_name"], "Hidden User")
        backup.db.get_user_by_id.assert_not_called()
        backup.client.get_entity.assert_not_called()


class TestForwardSourceLocalResolution(unittest.TestCase):
    def test_resolves_user_from_local_table_without_api_call(self):
        db = AsyncMock()
        db.get_user_by_id = AsyncMock(return_value={"first_name": "Ann", "last_name": "Lee", "username": None})
        client = AsyncMock()
        backup = _make_backup(db=db, client=client)
        fwd = _make_fwd_from(from_id=PeerUser(user_id=555))

        result = _run(backup._process_message(_make_message(fwd_from=fwd), -100))

        self.assertEqual(result["raw_data"]["forward_from_name"], "Ann Lee")
        db.get_user_by_id.assert_awaited_once_with(555)
        client.get_entity.assert_not_called()

    def test_local_lookup_error_falls_back_to_api_without_raising(self):
        """A SQLite lock blip on the local lookup must not abort the whole message."""
        db = AsyncMock()
        db.get_user_by_id = AsyncMock(side_effect=RuntimeError("database is locked"))
        client = AsyncMock()
        entity = MagicMock(spec=["first_name", "last_name"])
        entity.first_name = "Cara"
        entity.last_name = None
        client.get_entity = AsyncMock(return_value=entity)
        backup = _make_backup(db=db, client=client)
        fwd = _make_fwd_from(from_id=PeerUser(user_id=555))

        result = _run(backup._process_message(_make_message(fwd_from=fwd), -100))

        self.assertEqual(result["raw_data"]["forward_from_name"], "Cara")
        client.get_entity.assert_awaited_once()

    def test_resolves_channel_from_local_table_without_api_call(self):
        db = AsyncMock()
        db.get_chat_by_id = AsyncMock(return_value={"title": "News Channel", "username": None})
        client = AsyncMock()
        backup = _make_backup(db=db, client=client)
        fwd = _make_fwd_from(from_id=PeerChannel(channel_id=777))

        result = _run(backup._process_message(_make_message(fwd_from=fwd), -100))

        self.assertEqual(result["raw_data"]["forward_from_name"], "News Channel")
        client.get_entity.assert_not_called()


class TestForwardSourceApiFallbackAndCache(unittest.TestCase):
    def test_falls_back_to_api_when_not_found_locally(self):
        db = AsyncMock()
        db.get_user_by_id = AsyncMock(return_value=None)
        client = AsyncMock()
        entity = MagicMock(spec=["first_name", "last_name"])
        entity.first_name = "Bob"
        entity.last_name = None
        client.get_entity = AsyncMock(return_value=entity)
        backup = _make_backup(db=db, client=client)
        fwd = _make_fwd_from(from_id=PeerUser(user_id=555))

        result = _run(backup._process_message(_make_message(fwd_from=fwd), -100))

        self.assertEqual(result["raw_data"]["forward_from_name"], "Bob")
        client.get_entity.assert_awaited_once()

    def test_at_most_one_api_call_per_distinct_source_per_run(self):
        db = AsyncMock()
        db.get_user_by_id = AsyncMock(return_value=None)
        client = AsyncMock()
        entity = MagicMock(spec=["first_name", "last_name"])
        entity.first_name = "Bob"
        entity.last_name = None
        client.get_entity = AsyncMock(return_value=entity)
        backup = _make_backup(db=db, client=client)

        async def process_two():
            await backup._process_message(_make_message(fwd_from=_make_fwd_from(from_id=PeerUser(user_id=555)), msg_id=1), -100)
            return await backup._process_message(
                _make_message(fwd_from=_make_fwd_from(from_id=PeerUser(user_id=555)), msg_id=2), -100
            )

        result2 = _run(process_two())

        self.assertEqual(client.get_entity.await_count, 1)
        self.assertEqual(result2["raw_data"]["forward_from_name"], "Bob")

    def test_negative_cache_skips_retry_within_same_run(self):
        """An unresolvable source is not retried in the same run (no FloodWait re-risk)."""
        db = AsyncMock()
        db.get_user_by_id = AsyncMock(return_value=None)
        client = AsyncMock()
        client.get_entity = AsyncMock(side_effect=RuntimeError("boom"))
        backup = _make_backup(db=db, client=client)

        async def process_two():
            await backup._process_message(_make_message(fwd_from=_make_fwd_from(from_id=PeerUser(user_id=999)), msg_id=1), -100)
            return await backup._process_message(
                _make_message(fwd_from=_make_fwd_from(from_id=PeerUser(user_id=999)), msg_id=2), -100
            )

        result2 = _run(process_two())

        self.assertEqual(client.get_entity.await_count, 1)
        self.assertNotIn("forward_from_name", result2["raw_data"])


class TestForwardProvenanceRawData(unittest.TestCase):
    def test_captures_fwd_from_structure(self):
        backup = _make_backup()
        fwd_date = datetime(2024, 3, 1, 8, 30, 0)
        fwd = _make_fwd_from(
            from_name="Hidden User",
            channel_post=42,
            post_author="Editor",
            date=fwd_date,
        )

        result = _run(backup._process_message(_make_message(fwd_from=fwd), -100))

        self.assertEqual(
            result["raw_data"]["fwd_from"],
            {
                "from_name": "Hidden User",
                "channel_post": 42,
                "post_author": "Editor",
                "date": fwd_date.isoformat(),
            },
        )

    def test_omits_absent_keys(self):
        backup = _make_backup()
        fwd = _make_fwd_from(from_name="Hidden User")

        result = _run(backup._process_message(_make_message(fwd_from=fwd), -100))

        fwd_from_data = result["raw_data"]["fwd_from"]
        self.assertNotIn("channel_post", fwd_from_data)
        self.assertNotIn("post_author", fwd_from_data)
        self.assertNotIn("saved_from_peer", fwd_from_data)
        self.assertNotIn("from_id", fwd_from_data)

    def test_includes_from_id_for_peer_forwards(self):
        db = AsyncMock()
        db.get_user_by_id = AsyncMock(return_value={"first_name": "Ann", "last_name": None, "username": None})
        backup = _make_backup(db=db)
        fwd = _make_fwd_from(from_id=PeerUser(user_id=555))

        result = _run(backup._process_message(_make_message(fwd_from=fwd), -100))

        self.assertEqual(result["raw_data"]["fwd_from"]["from_id"], 555)

    def test_no_forward_omits_fwd_from_and_forward_from_name(self):
        backup = _make_backup()

        result = _run(backup._process_message(_make_message(fwd_from=None), -100))

        self.assertNotIn("fwd_from", result["raw_data"])
        self.assertNotIn("forward_from_name", result["raw_data"])


if __name__ == "__main__":
    unittest.main()
