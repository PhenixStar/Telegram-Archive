"""The repair script's cut-short finder and its media-volume guard.

``--mode truncated`` lists video and audio files whose download stopped early
and, with --apply, replaces each one only with a complete download that starts
with the same bytes. ``--mode media`` and ``--mode truncated`` refuse to run
when the media folder is not visibly there.
"""

import importlib.util
import os
import struct
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "refetch_incomplete_messages.py")
if "refetch_incomplete_messages" in sys.modules:
    refetch = sys.modules["refetch_incomplete_messages"]
else:
    _spec = importlib.util.spec_from_file_location("refetch_incomplete_messages", _SCRIPT)
    refetch = importlib.util.module_from_spec(_spec)
    sys.modules["refetch_incomplete_messages"] = refetch
    _spec.loader.exec_module(refetch)

from src.media_integrity import CUT_SHORT_GRAIN, ShortDownloadError  # noqa: E402

FTYP = struct.pack(">I4s", 16, b"ftyp") + b"isom\x00\x00\x02\x00"


def _cut_mp4_bytes(total: int) -> bytes:
    head = FTYP + struct.pack(">I4s", 50 * 1024 * 1024, b"mdat")
    return head + b"\x01" * (total - len(head))


@pytest.fixture
def media_root(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def _clear_selected_paths():
    refetch.TRUNCATED_FILE_PATHS.clear()
    yield
    refetch.TRUNCATED_FILE_PATHS.clear()


def _rows_returning(rows):
    async def fake_rows(_db, sql):
        return rows

    return fake_rows


class TestTruncatedSelection:
    @pytest.mark.asyncio
    async def test_only_files_cut_on_the_download_grain_are_selected(self, monkeypatch, media_root):
        shared = media_root / "_shared"
        shared.mkdir()
        chat = media_root / "126"
        chat.mkdir()
        (shared / "cut.mp4").write_bytes(_cut_mp4_bytes(2 * CUT_SHORT_GRAIN))
        os.symlink(os.path.relpath(shared / "cut.mp4", chat), chat / "cut.mp4")
        (chat / "odd.mp4").write_bytes(_cut_mp4_bytes(2 * CUT_SHORT_GRAIN + 5))  # suspicious: never touched
        (chat / "photo.jpg").write_bytes(b"\x00" * CUT_SHORT_GRAIN)  # not an MP4-family file
        monkeypatch.setattr(
            refetch,
            "_rows",
            _rows_returning(
                [
                    (126, 1, "/data/backups/media/126/cut.mp4", "video"),
                    (126, 2, "/data/backups/media/126/odd.mp4", "video"),
                    (126, 3, "/data/backups/media/126/photo.jpg", "photo"),
                    (126, 4, "/data/backups/media/126/gone.mp4", "video"),
                ]
            ),
        )

        targets = await refetch._select_targets(object(), SimpleNamespace(media_path=str(media_root)), "truncated")

        assert targets == {126: [1]}
        # The real file behind the link is what gets replaced.
        assert refetch.TRUNCATED_FILE_PATHS[(126, 1)] == os.path.realpath(shared / "cut.mp4")

    @pytest.mark.asyncio
    async def test_a_link_out_of_the_archive_is_never_selected(self, monkeypatch, media_root, tmp_path):
        outside = tmp_path / "annex"
        outside.mkdir()
        (outside / "cut.mp4").write_bytes(_cut_mp4_bytes(CUT_SHORT_GRAIN))
        chat = media_root / "126"
        chat.mkdir()
        os.symlink(outside / "cut.mp4", chat / "cut.mp4")
        monkeypatch.setattr(refetch, "_rows", _rows_returning([(126, 1, "/data/backups/media/126/cut.mp4", "video")]))

        targets = await refetch._select_targets(object(), SimpleNamespace(media_path=str(media_root)), "truncated")

        assert targets == {}


class TestReplaceCutShortFile:
    @staticmethod
    def _backup(download):
        return SimpleNamespace(_download_media_to_path=AsyncMock(side_effect=download))

    @staticmethod
    def _message():
        return SimpleNamespace(id=1, media=SimpleNamespace(document=SimpleNamespace(size=3 * CUT_SHORT_GRAIN)))

    @pytest.mark.asyncio
    async def test_complete_download_starting_with_the_short_bytes_replaces_it(self, tmp_path):
        short = tmp_path / "cut.mp4"
        short_bytes = _cut_mp4_bytes(CUT_SHORT_GRAIN)
        short.write_bytes(short_bytes)
        link = tmp_path / "link.mp4"
        os.symlink(short, link)

        async def download(message, path, size, chat_id):
            with open(path, "wb") as f:
                f.write(short_bytes + b"\x02" * (2 * CUT_SHORT_GRAIN))
            return path

        assert await refetch._replace_cut_short_file(self._backup(download), self._message(), 126, str(short))
        assert os.path.getsize(short) == 3 * CUT_SHORT_GRAIN
        assert os.path.getsize(link) == 3 * CUT_SHORT_GRAIN  # every link reads the complete bytes
        assert sorted(os.listdir(tmp_path)) == ["cut.mp4", "link.mp4"]  # no temporary file left

    @pytest.mark.asyncio
    async def test_different_bytes_leave_the_short_file_as_it_was(self, tmp_path):
        short = tmp_path / "cut.mp4"
        short_bytes = _cut_mp4_bytes(CUT_SHORT_GRAIN)
        short.write_bytes(short_bytes)

        async def download(message, path, size, chat_id):
            with open(path, "wb") as f:
                f.write(b"\x09" * (3 * CUT_SHORT_GRAIN))
            return path

        assert not await refetch._replace_cut_short_file(self._backup(download), self._message(), 126, str(short))
        assert short.read_bytes() == short_bytes
        assert os.listdir(tmp_path) == ["cut.mp4"]

    @pytest.mark.asyncio
    async def test_failed_download_leaves_the_short_file_as_it_was(self, tmp_path):
        short = tmp_path / "cut.mp4"
        short_bytes = _cut_mp4_bytes(CUT_SHORT_GRAIN)
        short.write_bytes(short_bytes)

        async def download(message, path, size, chat_id):
            raise ShortDownloadError("download stopped at 1 of 2 bytes")

        assert not await refetch._replace_cut_short_file(self._backup(download), self._message(), 126, str(short))
        assert short.read_bytes() == short_bytes

    @pytest.mark.asyncio
    async def test_repair_chat_counts_only_replaced_files(self, tmp_path, monkeypatch):
        async def passthrough(fn, *args, **kwargs):
            return await fn(*args, **kwargs)

        monkeypatch.setattr(refetch, "call_with_flood_retry", passthrough)
        replaced = {1: True, 2: False}

        async def fake_replace(backup, message, chat_id, short_path):
            return replaced[message.id]

        monkeypatch.setattr(refetch, "_replace_cut_short_file", fake_replace)
        chat = tmp_path / "126"
        chat.mkdir()
        (chat / "v.mp4").write_bytes(b"complete")
        refetch.TRUNCATED_FILE_PATHS[(126, 1)] = str(chat / "v.mp4")
        refetch.TRUNCATED_FILE_PATHS[(126, 2)] = str(chat / "w.mp4")

        async def process(message, chat_id):
            return {"id": message.id, "_media_data": {"downloaded": True, "file_path": "/data/backups/media/126/v.mp4"}}

        backup = SimpleNamespace(
            client=SimpleNamespace(
                get_entity=AsyncMock(return_value=object()),
                get_messages=AsyncMock(return_value=[SimpleNamespace(id=1), SimpleNamespace(id=2)]),
            ),
            _process_message_isolated=AsyncMock(side_effect=process),
            _commit_batch=AsyncMock(),
            db=SimpleNamespace(mark_media_unavailable=AsyncMock()),
            config=SimpleNamespace(media_path=str(tmp_path)),
        )

        repaired, unavailable = await refetch._repair_chat(backup, 126, [1, 2], "truncated", 0)

        assert (repaired, unavailable) == (1, 1)
        # A file that was not replaced is never re-committed: its row stays as it was.
        assert backup._process_message_isolated.await_count == 1
        backup.db.mark_media_unavailable.assert_not_awaited()


class TestMediaVolumeGuard:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["media", "truncated"])
    async def test_file_modes_refuse_an_empty_media_root(self, monkeypatch, media_root, mode):
        config = SimpleNamespace(media_path=str(media_root), max_media_size_mb=0)
        monkeypatch.setattr(refetch, "Config", lambda: config)
        monkeypatch.setattr(refetch, "init_database", AsyncMock(return_value=object()))
        monkeypatch.setattr(refetch, "DatabaseAdapter", lambda manager: object())
        select = AsyncMock(return_value={})
        monkeypatch.setattr(refetch, "_select_targets", select)
        args = SimpleNamespace(mode=mode, max_media_size_mb=0, min_size_mb=0, max_size_mb=None)

        assert await refetch.run(args) == 1
        select.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_visible_media_root_is_checked(self, monkeypatch, media_root):
        (media_root / "126").mkdir()
        config = SimpleNamespace(media_path=str(media_root), max_media_size_mb=0)
        monkeypatch.setattr(refetch, "Config", lambda: config)
        monkeypatch.setattr(refetch, "init_database", AsyncMock(return_value=object()))
        monkeypatch.setattr(refetch, "DatabaseAdapter", lambda manager: object())
        select = AsyncMock(return_value={})
        monkeypatch.setattr(refetch, "_select_targets", select)
        args = SimpleNamespace(mode="truncated", max_media_size_mb=0, min_size_mb=0, max_size_mb=None)

        assert await refetch.run(args) == 0
        select.assert_awaited_once()
