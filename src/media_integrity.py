"""Whether a media file on disk is whole, and whether the media disk is there at all.

Two questions every path that writes or judges a media file has to ask first.

A download can stop short. Telethon ends a single-stream download at the first
answer shorter than its request (an empty one included) and never compares the
total with the size Telegram declared, so a dropped connection used to leave a
truncated file that was recorded as downloaded. ``check_complete_download``
compares the landed file with the declared size (``declared_document_size``)
and refuses a short one: the file is removed and ``ShortDownloadError`` raised,
so the caller's retry and not-downloaded handling take over.

Files already kept short are found without decoding them: an MP4-family file
whose top-level boxes run past the end of the file, or that holds no index
(``moov``), at a size that is a multiple of Telethon's smallest request size
(``cut_short_state``). The stored ``file_size`` cannot tell, since it was read
from the short file itself.

A media volume that is not mounted, or a share that dropped, makes every stored
path read as missing. ``visible_media_root`` answers None then, and no row may
be changed or re-downloaded on that evidence: a download would land beside the
volume, and a failure would mark a file not downloaded that is not gone.

Stdlib only: the backup, the listener and the maintenance scripts import it.
"""

from __future__ import annotations

import os
import struct

# A video or audio file whose download stopped early (``cut_short_state``).
TRUNCATED = "truncated"  # no index, at a size a stopped download leaves: repairable
SUSPICIOUS = "suspicious"  # no index, at any other size: reported, never repaired

# The ISO base media formats whose top-level boxes ``iso_bmff_incomplete``
# walks. The extension decides, not the media type: a .mp4 sent as a file is a
# document and has the same risk. Matroska and Ogg are not checked.
ISO_BMFF_EXTENSIONS = frozenset({".mp4", ".m4v", ".m4a", ".mov", ".3gp"})

# Telethon's smallest request size for a file of known size
# (``utils.get_appropriated_part_size``: 128 KiB up to 100 MB, then 256 and
# 512 KiB). A download that stopped at a short answer ends on a multiple of its
# request size, so every such cut is a multiple of this.
CUT_SHORT_GRAIN = 128 * 1024


class ShortDownloadError(Exception):
    """A download wrote fewer bytes than the size Telegram declared for the file.

    The message carries sizes only, never a path.
    """


class ShortFileMismatchError(Exception):
    """The bytes of a file cut short are not the start of its complete download."""


def _photo_size_bytes(size: object) -> int:
    """Byte count of a Telethon ``PhotoSize`` variant, 0 when it carries none.

    ``PhotoSize`` has a scalar ``size``; ``PhotoSizeProgressive`` has only
    ``sizes``, whose largest entry is the full rendition. Cached and stripped
    sizes carry their bytes inline and count 0 here, so they never raise the
    declared size above what Telethon writes.
    """
    value = getattr(size, "size", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    progressive = getattr(size, "sizes", None)
    if isinstance(progressive, (list, tuple)):
        candidates = [v for v in progressive if isinstance(v, int) and not isinstance(v, bool)]
        if candidates:
            return max(candidates)
    return 0


def declared_document_size(message: object) -> int | None:
    """The exact byte count Telegram declares for a message's file, or None.

    ``document.size`` is a document's size. For a photo Telethon downloads the
    largest rendition, whose size is the largest ``_photo_size_bytes``. A photo
    with ``video_sizes`` is left out: Telethon sorts a video size above every
    photo size and may fetch the animated version instead, which would make a
    good file read as short. Only a real positive int counts, so test doubles
    (MagicMock attributes) never trigger the check.
    """
    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    size = getattr(document, "size", None)
    if isinstance(size, int) and not isinstance(size, bool) and size > 0:
        return size
    photo = getattr(media, "photo", None)
    sizes = getattr(photo, "sizes", None)
    if document is None and isinstance(sizes, (list, tuple)) and sizes:
        video_sizes = getattr(photo, "video_sizes", None)
        if video_sizes is None or (isinstance(video_sizes, (list, tuple)) and not video_sizes):
            largest = max(_photo_size_bytes(s) for s in sizes)
            if largest > 0:
                return largest
    return None


def check_complete_download(path: str | None, declared: int | None) -> None:
    """Remove the file at ``path`` and raise ``ShortDownloadError`` when it is shorter than ``declared``.

    A short download is never left on disk: a later run would take the file
    at its path as already downloaded. Nothing is checked without a declared
    size or without a file; a download that produced no file is handled where
    its path is collected.
    """
    if not declared or not path:
        return
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size >= declared:
        return
    try:
        os.remove(path)
    except OSError:
        pass
    raise ShortDownloadError(f"download stopped at {size} of {declared} bytes")


def iso_bmff_incomplete(path: str) -> bool | None:
    """Whether an MP4-family file ends before its own boxes do.

    Walks the top-level boxes with seek and read, never reading a payload.
    True when a box's declared size (32-bit, or the 64-bit largesize when the
    size field is 1) runs past the end of the file, when the file ends inside a
    box header, or when the walk reaches the end with no ``moov`` box (the
    index a player needs). A size of 0 means "to the end of the file" and ends
    the walk. False when every box fits and a ``moov`` was seen. None when the
    file cannot be read, is empty, does not start with ``ftyp`` or holds a
    malformed box: such a file is not judged.
    """
    try:
        with open(path, "rb") as f:
            end = os.fstat(f.fileno()).st_size
            offset = 0
            seen_moov = False
            while offset < end:
                f.seek(offset)
                header = f.read(8)
                if len(header) < 8:
                    return None if offset == 0 else True
                size, kind = struct.unpack(">I4s", header)
                if offset == 0 and kind != b"ftyp":
                    return None
                header_len = 8
                if size == 1:
                    large = f.read(8)
                    if len(large) < 8:
                        return True
                    size = struct.unpack(">Q", large)[0]
                    header_len = 16
                if kind == b"moov":
                    seen_moov = True
                if size == 0:
                    break
                if size < header_len:
                    return None
                if offset + size > end:
                    return True
                offset += size
            if end == 0:
                return None
            return not seen_moov
    except OSError:
        return None


def cut_short_state(path: str | None) -> str | None:
    """TRUNCATED, SUSPICIOUS or None for the file at ``path``.

    Only an MP4-family extension (``ISO_BMFF_EXTENSIONS``) is looked at.
    TRUNCATED: ``iso_bmff_incomplete`` and a size that is a multiple of
    ``CUT_SHORT_GRAIN``, the shape a stopped Telethon download leaves.
    SUSPICIOUS: incomplete at any other size, which a stopped download does not
    explain, so it is reported and never repaired. None for everything else,
    a file that is not judged included.
    """
    if not path or os.path.splitext(path)[1].lower() not in ISO_BMFF_EXTENSIONS:
        return None
    if iso_bmff_incomplete(path) is not True:
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    return TRUNCATED if size % CUT_SHORT_GRAIN == 0 else SUSPICIOUS


def ensure_prefix_of(old_path: str, new_path: str, chunk_size: int = 1024 * 1024) -> None:
    """Raise ``ShortFileMismatchError`` unless ``old_path`` holds the first bytes of ``new_path``.

    The new file must be longer, and every byte of the old one must equal the
    byte at the same offset in the new one, so replacing the old file loses
    nothing the archive held.
    """
    if os.path.getsize(new_path) <= os.path.getsize(old_path):
        raise ShortFileMismatchError("the new download is not longer than the file it replaces")
    with open(old_path, "rb") as old, open(new_path, "rb") as new:
        while True:
            chunk = old.read(chunk_size)
            if not chunk:
                return
            if new.read(len(chunk)) != chunk:
                raise ShortFileMismatchError("the file cut short is not the start of the new download")


def visible_media_root(media_root: str | None) -> str | None:
    """The real path of the media root when the archive's disk is visibly there, else None.

    A missing, unreadable or empty media root means the process runs where the
    media volume is not mounted (a volume left out, a RAID that did not
    assemble, a share that dropped). Every stored path would then read as
    missing, so nothing may be re-downloaded or marked on that evidence.
    """
    if not media_root:
        return None
    try:
        if not os.path.isdir(media_root):
            return None
        with os.scandir(media_root) as entries:
            if next(entries, None) is None:
                return None
    except (OSError, TypeError, ValueError):
        return None
    return os.path.realpath(media_root)


__all__ = [
    "CUT_SHORT_GRAIN",
    "ISO_BMFF_EXTENSIONS",
    "SUSPICIOUS",
    "TRUNCATED",
    "ShortDownloadError",
    "ShortFileMismatchError",
    "check_complete_download",
    "cut_short_state",
    "declared_document_size",
    "ensure_prefix_of",
    "iso_bmff_incomplete",
    "visible_media_root",
]
