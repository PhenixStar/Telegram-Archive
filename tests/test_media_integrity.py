"""Whole-file and mounted-volume checks in ``src.media_integrity``.

The helpers decide whether a download is kept, which kept files count as cut
short, and whether the media disk is there at all, so each rule is pinned here
on real files in a temporary directory.
"""

from __future__ import annotations

import os
import struct
from types import SimpleNamespace

import pytest

from src.media_integrity import (
    CUT_SHORT_GRAIN,
    SUSPICIOUS,
    TRUNCATED,
    ShortDownloadError,
    ShortFileMismatchError,
    check_complete_download,
    cut_short_state,
    declared_document_size,
    ensure_prefix_of,
    iso_bmff_incomplete,
    visible_media_root,
)


def _box(kind: bytes, payload: bytes = b"", size: int | None = None) -> bytes:
    return struct.pack(">I4s", size if size is not None else 8 + len(payload), kind) + payload


FTYP = _box(b"ftyp", b"isom\x00\x00\x02\x00")


def _write(path, data: bytes) -> str:
    path.write_bytes(data)
    return str(path)


def _cut_mp4(path, total: int) -> str:
    """An MP4 whose mdat declares far more bytes than the file holds, padded to ``total``."""
    head = FTYP + _box(b"mdat", size=50 * 1024 * 1024)
    return _write(path, head + b"\x00" * (total - len(head)))


class TestDeclaredDocumentSize:
    def test_document_size_is_declared(self):
        message = SimpleNamespace(media=SimpleNamespace(document=SimpleNamespace(size=12345)))
        assert declared_document_size(message) == 12345

    def test_largest_photo_rendition_is_declared(self):
        sizes = [SimpleNamespace(size=100), SimpleNamespace(sizes=[10, 900, 400]), SimpleNamespace(size=500)]
        photo = SimpleNamespace(sizes=sizes, video_sizes=None)
        message = SimpleNamespace(media=SimpleNamespace(document=None, photo=photo))
        assert declared_document_size(message) == 900

    def test_photo_with_a_video_version_is_not_judged(self):
        # Telethon may fetch the animated version, which has another size.
        photo = SimpleNamespace(sizes=[SimpleNamespace(size=100)], video_sizes=[SimpleNamespace(size=5000)])
        message = SimpleNamespace(media=SimpleNamespace(document=None, photo=photo))
        assert declared_document_size(message) is None

    def test_test_doubles_and_bools_are_inert(self):
        from unittest.mock import MagicMock

        assert declared_document_size(MagicMock()) is None
        message = SimpleNamespace(media=SimpleNamespace(document=SimpleNamespace(size=True)))
        assert declared_document_size(message) is None

    def test_no_media_declares_nothing(self):
        assert declared_document_size(SimpleNamespace(media=None)) is None


class TestCheckCompleteDownload:
    def test_short_file_is_removed_and_refused(self, tmp_path):
        path = _write(tmp_path / "f.mp4", b"x" * 10)
        with pytest.raises(ShortDownloadError, match="10 of 20"):
            check_complete_download(path, 20)
        assert not os.path.exists(path)

    def test_complete_file_is_kept(self, tmp_path):
        path = _write(tmp_path / "f.mp4", b"x" * 20)
        check_complete_download(path, 20)
        assert os.path.getsize(path) == 20

    def test_a_larger_file_is_kept(self, tmp_path):
        # A stripped photo inflated to a JPEG is larger than its declared bytes.
        path = _write(tmp_path / "f.jpg", b"x" * 30)
        check_complete_download(path, 20)
        assert os.path.exists(path)

    def test_nothing_to_check_without_a_size_or_a_file(self, tmp_path):
        path = _write(tmp_path / "f.bin", b"x")
        check_complete_download(path, None)
        check_complete_download(None, 100)
        check_complete_download(str(tmp_path / "missing"), 100)
        assert os.path.exists(path)


class TestIsoBmffIncomplete:
    def test_complete_file_with_index(self, tmp_path):
        path = _write(tmp_path / "a.mp4", FTYP + _box(b"moov", b"m" * 16) + _box(b"mdat", b"d" * 32))
        assert iso_bmff_incomplete(path) is False

    def test_box_running_past_the_end(self, tmp_path):
        assert iso_bmff_incomplete(_cut_mp4(tmp_path / "a.mp4", 4096)) is True

    def test_faststart_file_cut_inside_media_data(self, tmp_path):
        path = _write(tmp_path / "a.mp4", FTYP + _box(b"moov", b"m" * 16) + _box(b"mdat", size=1000) + b"d" * 10)
        assert iso_bmff_incomplete(path) is True

    def test_no_index_at_all(self, tmp_path):
        path = _write(tmp_path / "a.mp4", FTYP + _box(b"mdat", b"d" * 32))
        assert iso_bmff_incomplete(path) is True

    def test_file_ending_inside_a_box_header(self, tmp_path):
        path = _write(tmp_path / "a.mp4", FTYP + _box(b"moov", b"m" * 8) + b"\x00\x00")
        assert iso_bmff_incomplete(path) is True

    def test_largesize_box(self, tmp_path):
        mdat = struct.pack(">I4sQ", 1, b"mdat", 16 + 8) + b"d" * 8
        path = _write(tmp_path / "a.mp4", FTYP + _box(b"moov", b"m" * 8) + mdat)
        assert iso_bmff_incomplete(path) is False

    def test_size_zero_box_runs_to_the_end(self, tmp_path):
        path = _write(tmp_path / "a.mp4", FTYP + _box(b"moov", b"m" * 8) + _box(b"mdat", size=0) + b"d" * 64)
        assert iso_bmff_incomplete(path) is False

    def test_not_judged(self, tmp_path):
        assert iso_bmff_incomplete(_write(tmp_path / "empty.mp4", b"")) is None
        assert iso_bmff_incomplete(_write(tmp_path / "other.mp4", _box(b"free", b"x" * 8))) is None
        assert iso_bmff_incomplete(_write(tmp_path / "bad.mp4", FTYP + _box(b"moov", size=4))) is None
        assert iso_bmff_incomplete(str(tmp_path / "missing.mp4")) is None


class TestCutShortState:
    def test_cut_on_the_download_grain_is_truncated(self, tmp_path):
        assert cut_short_state(_cut_mp4(tmp_path / "v.mp4", 3 * CUT_SHORT_GRAIN)) == TRUNCATED

    def test_cut_off_the_grain_is_only_suspicious(self, tmp_path):
        assert cut_short_state(_cut_mp4(tmp_path / "v.mp4", 3 * CUT_SHORT_GRAIN + 7)) == SUSPICIOUS

    def test_extension_decides_what_is_looked_at(self, tmp_path):
        assert cut_short_state(_cut_mp4(tmp_path / "v.M4A", CUT_SHORT_GRAIN)) == TRUNCATED
        assert cut_short_state(_cut_mp4(tmp_path / "v.mkv", CUT_SHORT_GRAIN)) is None
        assert cut_short_state(None) is None

    def test_complete_file_is_not_judged_cut(self, tmp_path):
        data = FTYP + _box(b"moov", b"m" * 16)
        data += _box(b"mdat", b"d" * (CUT_SHORT_GRAIN - len(data) - 8))
        path = _write(tmp_path / "v.mp4", data)
        assert os.path.getsize(path) == CUT_SHORT_GRAIN
        assert cut_short_state(path) is None


class TestEnsurePrefixOf:
    def test_short_file_that_starts_the_new_one(self, tmp_path):
        old = _write(tmp_path / "old", b"abc" * 1000)
        new = _write(tmp_path / "new", b"abc" * 1000 + b"rest")
        ensure_prefix_of(old, new, chunk_size=7)

    def test_different_bytes_are_refused(self, tmp_path):
        old = _write(tmp_path / "old", b"abd")
        new = _write(tmp_path / "new", b"abc-longer")
        with pytest.raises(ShortFileMismatchError):
            ensure_prefix_of(old, new)

    def test_new_file_must_be_longer(self, tmp_path):
        old = _write(tmp_path / "old", b"abc")
        new = _write(tmp_path / "new", b"abc")
        with pytest.raises(ShortFileMismatchError):
            ensure_prefix_of(old, new)


class TestVisibleMediaRoot:
    def test_root_with_entries_is_visible(self, tmp_path):
        (tmp_path / "123").mkdir()
        assert visible_media_root(str(tmp_path)) == os.path.realpath(tmp_path)

    def test_empty_root_is_not_visible(self, tmp_path):
        # An unmounted volume leaves its empty mount point behind.
        assert visible_media_root(str(tmp_path)) is None

    def test_missing_or_unset_root_is_not_visible(self, tmp_path):
        assert visible_media_root(str(tmp_path / "nope")) is None
        assert visible_media_root(None) is None
        assert visible_media_root("") is None

    def test_a_file_is_not_a_root(self, tmp_path):
        assert visible_media_root(_write(tmp_path / "f", b"x")) is None
