"""Selection logic for the incomplete-message repair script.

The script re-fetches from Telegram, so what it selects decides how much API
traffic a repair costs and which rows it touches. These tests cover that
selection without contacting Telegram.
"""

import importlib.util
import os
import sys

import pytest

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "refetch_incomplete_messages.py")
_spec = importlib.util.spec_from_file_location("refetch_incomplete_messages", _SCRIPT)
refetch = importlib.util.module_from_spec(_spec)
sys.modules["refetch_incomplete_messages"] = refetch
_spec.loader.exec_module(refetch)


@pytest.fixture
def media_root(tmp_path):
    """A media root named like the real one.

    Config builds it as <BACKUP_PATH>/media, and the path normalizer anchors a
    stored path on that directory's own name, so a root called anything else
    would not resolve rows written under an older root.
    """
    root = tmp_path / "media"
    root.mkdir()
    return root


class TestEmptyOnDisk:
    """A row claiming a downloaded file whose file is empty or gone."""

    def test_zero_byte_file_is_repairable(self, media_root):
        chat_dir = media_root / "126"
        chat_dir.mkdir()
        (chat_dir / "voice.ogg").touch()

        assert refetch._empty_on_disk("/data/backups/media/126/voice.ogg", str(media_root)) is True

    def test_missing_file_is_repairable(self, media_root):
        assert refetch._empty_on_disk("/data/backups/media/126/gone.jpg", str(media_root)) is True

    def test_healthy_file_is_left_alone(self, media_root):
        chat_dir = media_root / "126"
        chat_dir.mkdir()
        (chat_dir / "photo.jpg").write_bytes(b"real bytes")

        assert refetch._empty_on_disk("/data/backups/media/126/photo.jpg", str(media_root)) is False

    def test_a_file_stored_under_an_old_root_is_still_found(self, media_root):
        # About 29% of this archive's rows point at a root the archive no longer
        # lives at; they must not be mistaken for missing files and re-downloaded.
        chat_dir = media_root / "126"
        chat_dir.mkdir()
        (chat_dir / "photo.jpg").write_bytes(b"real bytes")

        stored = "/home/dgx/Desktop/tele-private/database/backups/media/126/photo.jpg"
        assert refetch._empty_on_disk(stored, str(media_root)) is False

    def test_an_unresolvable_path_is_not_claimed(self, media_root):
        # A path that cannot be anchored to any media root is a different problem;
        # re-downloading on that basis would be guesswork.
        assert refetch._empty_on_disk("/etc/passwd", str(media_root)) is False


class TestQueries:
    def test_blank_message_query_excludes_service_messages(self):
        # Service messages legitimately have no text and no media; they render
        # from their own marker and must not be re-fetched.
        assert "service_type" in refetch.BLANK_MESSAGES_SQL
        assert "NOT EXISTS" in refetch.BLANK_MESSAGES_SQL

    def test_media_query_only_considers_rows_claiming_a_download(self):
        assert "downloaded = 1" in refetch.DOWNLOADED_MEDIA_SQL
        assert "file_path IS NOT NULL" in refetch.DOWNLOADED_MEDIA_SQL


class TestSelection:
    @pytest.mark.asyncio
    async def test_targets_are_grouped_by_chat(self, monkeypatch, tmp_path):
        # Grouping decides the API cost: one entity resolution and one batched
        # fetch per chat rather than per message.
        from types import SimpleNamespace

        async def fake_rows(_db, sql):
            return [(-100, 1), (-100, 2), (-200, 9)]

        monkeypatch.setattr(refetch, "_rows", fake_rows)
        config = SimpleNamespace(media_path=str(tmp_path))

        targets = await refetch._select_targets(object(), config, "blank")

        assert targets == {-100: [1, 2], -200: [9]}

    @pytest.mark.asyncio
    async def test_media_mode_skips_files_that_are_fine(self, monkeypatch, media_root):
        from types import SimpleNamespace

        chat_dir = media_root / "126"
        chat_dir.mkdir()
        (chat_dir / "good.jpg").write_bytes(b"bytes")
        (chat_dir / "empty.jpg").touch()

        async def fake_rows(_db, sql):
            return [
                (126, 1, "/data/backups/media/126/good.jpg"),
                (126, 2, "/data/backups/media/126/empty.jpg"),
            ]

        monkeypatch.setattr(refetch, "_rows", fake_rows)
        config = SimpleNamespace(media_path=str(media_root))

        targets = await refetch._select_targets(object(), config, "media")

        assert targets == {126: [2]}
