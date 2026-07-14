"""Tests for on-disk storage sizing (issue #202, fork port).

Covers ``message_utils.compute_directory_size`` (du semantics, symlinks counted
once) — the primitive behind the viewer's Storage stat switching from
``SUM(file_size)`` to actual disk usage.
"""

from __future__ import annotations

import os
from unittest import TestCase

from src.message_utils import compute_directory_size


class TestComputeDirectorySize(TestCase):
    def _write(self, path: str, size: int) -> None:
        with open(path, "wb") as fh:
            fh.write(b"x" * size)

    def test_sums_regular_files_recursively(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            self._write(os.path.join(d, "a.bin"), 100)
            os.makedirs(os.path.join(d, "sub"))
            self._write(os.path.join(d, "sub", "b.bin"), 250)
            self.assertEqual(compute_directory_size(d), 350)

    def test_symlinks_not_followed_so_shared_blob_counted_once(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            shared = os.path.join(d, "_shared")
            os.makedirs(shared)
            self._write(os.path.join(shared, "blob.mp4"), 1000)
            # A chat-dir symlink to the shared blob must not double-count.
            chat = os.path.join(d, "123")
            os.makedirs(chat)
            os.symlink(os.path.join(shared, "blob.mp4"), os.path.join(chat, "blob.mp4"))
            self.assertEqual(compute_directory_size(d), 1000)

    def test_missing_path_returns_zero(self) -> None:
        self.assertEqual(compute_directory_size("/does/not/exist/anywhere"), 0)

    def test_empty_or_none_path_returns_zero(self) -> None:
        self.assertEqual(compute_directory_size(""), 0)

    def test_broken_symlink_ignored(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            os.symlink(os.path.join(d, "missing-target"), os.path.join(d, "dangling"))
            self._write(os.path.join(d, "real.bin"), 42)
            self.assertEqual(compute_directory_size(d), 42)
