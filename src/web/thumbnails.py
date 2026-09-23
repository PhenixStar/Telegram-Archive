"""On-demand thumbnail generation with disk caching.

Generates WebP thumbnails at whitelisted sizes, stored under
{media_root}/.thumbs/{size}/{folder}/{stem}.webp.
Pillow runs in a thread executor to avoid blocking the async event loop.

Features:
- Multiple size tiers with per-size quality settings
- mtime validation to invalidate stale cached thumbnails
- LQIP (Low Quality Image Placeholder) generation for progressive loading
- Video thumbnail extraction via ffmpeg (best-effort)
- Concurrency limiter to bound parallel CPU/IO usage
"""

import asyncio
import base64
import logging
import os
import shutil
import subprocess
import tempfile
import time
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageFilter

logger = logging.getLogger(__name__)

# Limit decompression to prevent pixel-bomb OOM attacks (~50 megapixels)
Image.MAX_IMAGE_PIXELS = 50_000_000

ALLOWED_SIZES: set[int] = {64, 200, 400, 800}
QUALITY_MAP: dict[int, int] = {64: 55, 200: 72, 400: 80, 800: 85}
_MAX_SOURCE_BYTES = 50 * 1024 * 1024  # 50 MB
# Peak decode memory follows pixel count, not compressed size, so the byte gate
# above cannot bound it: a 12000x8000 PNG of flat colour is a few hundred KB on
# disk and still costs roughly 390 MB to decode. Image.MAX_IMAGE_PIXELS above is
# no help either, because Pillow only raises above TWICE that value, so anything
# up to 100 MP proceeds after a warning nobody reads. Any Telegram contact can
# send such a file, and the viewer decodes it on request.
_MAX_SOURCE_PIXELS = 25_000_000

_IMAGE_EXTENSIONS: set[str] = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff",
}
_VIDEO_EXTENSIONS: set[str] = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

# Concurrency limiter: max 4 simultaneous thumbnail generations. Together with
# _MAX_SOURCE_PIXELS this bounds peak thumbnail memory to a small multiple of one
# capped decode instead of letting it grow with the request count.
_THUMB_SEMAPHORE = asyncio.Semaphore(4)

# Remember recent generation failures. Nothing is written when generation fails,
# so without this an undecodable video re-runs ffmpeg (a 15s subprocess holding a
# semaphore slot) on every request from every viewer. Time-bounded rather than
# permanent, so a truncated download that later completes recovers on its own.
_FAILURE_TTL_SECONDS = 300.0
_MAX_FAILURE_ENTRIES = 1024
_recent_failures: dict[tuple[int, str], float] = {}


def _failure_cached(key: tuple[int, str]) -> bool:
    """True when this (size, source) failed recently enough to skip retrying."""
    expires_at = _recent_failures.get(key)
    if expires_at is None:
        return False
    if expires_at <= time.monotonic():
        _recent_failures.pop(key, None)
        return False
    return True


def _record_failure(key: tuple[int, str]) -> None:
    """Remember a failed generation for _FAILURE_TTL_SECONDS."""
    now = time.monotonic()
    if len(_recent_failures) >= _MAX_FAILURE_ENTRIES:
        for stale in [k for k, expires_at in _recent_failures.items() if expires_at <= now]:
            del _recent_failures[stale]
        if len(_recent_failures) >= _MAX_FAILURE_ENTRIES:
            # Dropping entries only costs a regeneration, never correctness.
            _recent_failures.clear()
    _recent_failures[key] = now + _FAILURE_TTL_SECONDS


def _exceeds_pixel_cap(img: Image.Image) -> bool:
    """True when the image Pillow is about to decode is over the pixel cap.

    Call this BEFORE ``draft()``: draft rewrites ``img.size`` to the reduced
    decode scale, so measuring afterwards reads the shrunken size and lets a
    progressive or multi-scan JPEG — which still decodes at full size — slip
    past the cap entirely.
    """
    width, height = img.size
    return bool(width and height and width * height > _MAX_SOURCE_PIXELS)


def _save_webp_atomic(img: Image.Image, dest: Path, quality: int) -> None:
    """Write the WebP to a temp file in dest's directory, then os.replace() it.

    Pillow streams straight into whatever path it is given, so writing the cache
    path directly makes a half-written file visible to a concurrent request —
    ``dest.exists()`` is the only completeness check there is, and the truncated
    result would then be served with a 24h Cache-Control.
    """
    # Short fixed prefix: media names are already near the filesystem's
    # per-component byte budget, so embedding one here could overflow it.
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".thumb-", suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        img.save(tmp_path, "WEBP", quality=quality)
        os.replace(tmp_path, dest)
    finally:
        tmp_path.unlink(missing_ok=True)


def _is_image(filename: str) -> bool:
    """Check if filename has a recognised image extension."""
    return Path(filename).suffix.lower() in _IMAGE_EXTENSIONS


def _is_video(filename: str) -> bool:
    """Check if filename has a recognised video extension."""
    return Path(filename).suffix.lower() in _VIDEO_EXTENSIONS


def _thumb_path(
    media_root: Path, size: int, folder: str, filename: str, ext: str = ".webp",
) -> Path:
    """Build the cache path for a thumbnail."""
    stem = Path(filename).stem
    return media_root / ".thumbs" / str(size) / folder / f"{stem}{ext}"


def _validate_traversal(media_root: Path, folder: str, filename: str) -> tuple[Path, Path] | None:
    """Resolve source path and verify it stays inside media_root.

    Returns (media_root_resolved, source_resolved) or None on traversal attempt.
    """
    media_root_resolved = media_root.resolve()
    source = (media_root / folder / filename).resolve()
    if not source.is_relative_to(media_root_resolved):
        return None
    return media_root_resolved, source


# ---------------------------------------------------------------------------
# Image thumbnail generation
# ---------------------------------------------------------------------------

def _generate_sync(source: Path, dest: Path, size: int) -> bool:
    """Blocking thumbnail generation -- meant for run_in_executor."""
    try:
        if source.stat().st_size > _MAX_SOURCE_BYTES:
            logger.warning(
                "Source too large for thumbnail: %s (%d bytes)",
                source, source.stat().st_size,
            )
            return False
        quality = QUALITY_MAP.get(size, 80)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as img:
            if _exceeds_pixel_cap(img):
                logger.warning(
                    "Source exceeds the thumbnail pixel cap: %s (%sx%s)", source, img.size[0], img.size[1]
                )
                return False
            # Baseline JPEGs can be decoded straight at a reduced scale, which is
            # both faster and cheaper; every other format decodes in full.
            if img.format == "JPEG":
                img.draft("RGB", (size, size))
            img.thumbnail((size, size), Image.LANCZOS)
            _save_webp_atomic(img, dest, quality)
        return True
    except Exception as e:
        logger.warning("Thumbnail generation failed for %s: %s", source, e)
        return False


def _is_cache_fresh(source: Path, dest: Path) -> bool:
    """Return True if dest exists and is not older than source."""
    if not dest.exists():
        return False
    if source.exists() and source.stat().st_mtime > dest.stat().st_mtime:
        return False  # source is newer, need regeneration
    return True


async def ensure_thumbnail(
    media_root: Path, size: int, folder: str, filename: str,
) -> Path | None:
    """Return the path to a cached thumbnail, generating it if needed.

    Returns None when the request is invalid or generation fails.
    Includes path traversal protection and mtime-based cache invalidation.
    """
    if size not in ALLOWED_SIZES:
        return None
    if not _is_image(filename):
        return None

    resolved = _validate_traversal(media_root, folder, filename)
    if resolved is None:
        return None
    _, source = resolved

    dest = _thumb_path(media_root, size, folder, filename).resolve()
    thumbs_root = (media_root / ".thumbs").resolve()
    if not dest.is_relative_to(thumbs_root):
        return None

    if _is_cache_fresh(source, dest):
        return dest

    if not source.exists():
        return None

    loop = asyncio.get_running_loop()
    async with _THUMB_SEMAPHORE:
        ok = await loop.run_in_executor(None, _generate_sync, source, dest, size)
    return dest if ok else None


# ---------------------------------------------------------------------------
# Video thumbnail generation (best-effort, requires ffmpeg)
# ---------------------------------------------------------------------------

def _generate_video_sync(source: Path, dest: Path, size: int) -> bool:
    """Extract first frame from video via ffmpeg and save as WebP thumbnail."""
    try:
        quality = QUALITY_MAP.get(size, 80)
        dest.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "ffmpeg",
                # The lane is chosen by the sender-controlled filename extension
                # and the byte gate cannot see pixel cost, so a 96 MP still
                # renamed .mp4 would otherwise drive ffmpeg to allocate a frame
                # of several hundred MB. -max_pixels makes the decoder itself
                # refuse an oversized frame (non-zero exit, handled below).
                "-max_pixels", str(_MAX_SOURCE_PIXELS),
                "-i", str(source),
                "-vframes", "1",
                "-vf", f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                "-f", "image2pipe", "-vcodec", "webp",
                "-quality", str(quality),
                "pipe:1",
            ],
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0 or not result.stdout:
            logger.debug("ffmpeg failed for %s: %s", source, result.stderr[:200])
            return False
        # Atomic for the same reason as the image lane: a concurrent request
        # must never see (and then cache for 24h) a partially written file.
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".thumb-", suffix=".tmp")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_bytes(result.stdout)
            os.replace(tmp_path, dest)
        finally:
            tmp_path.unlink(missing_ok=True)
        return True
    except Exception as e:
        logger.debug("Video thumbnail generation failed for %s: %s", source, e)
        return False


async def ensure_video_thumbnail(
    media_root: Path, size: int, folder: str, filename: str,
) -> Path | None:
    """Generate a thumbnail from a video file's first frame.

    Returns None if ffmpeg is unavailable, the file is not a video,
    or generation fails. Never raises.
    """
    if size not in ALLOWED_SIZES:
        return None
    if not _is_video(filename):
        return None
    if not shutil.which("ffmpeg"):
        return None

    resolved = _validate_traversal(media_root, folder, filename)
    if resolved is None:
        return None
    _, source = resolved

    dest = _thumb_path(media_root, size, folder, filename).resolve()
    thumbs_root = (media_root / ".thumbs").resolve()
    if not dest.is_relative_to(thumbs_root):
        return None

    if _is_cache_fresh(source, dest):
        return dest
    if not source.exists():
        return None

    failure_key = (size, str(source))
    if _failure_cached(failure_key):
        return None

    loop = asyncio.get_running_loop()
    async with _THUMB_SEMAPHORE:
        ok = await loop.run_in_executor(None, _generate_video_sync, source, dest, size)
    if not ok:
        _record_failure(failure_key)
        return None
    return dest


# ---------------------------------------------------------------------------
# LQIP (Low Quality Image Placeholder) generation
# ---------------------------------------------------------------------------

def _generate_lqip_sync(source: Path, cache_file: Path) -> str | None:
    """Generate a tiny base64 blur placeholder and cache it to disk."""
    try:
        if source.stat().st_size > _MAX_SOURCE_BYTES:
            return None
        with Image.open(source) as img:
            if _exceeds_pixel_cap(img):
                return None
            if img.format == "JPEG":
                img.draft("RGB", (32, 32))
            img.thumbnail((32, 32), Image.LANCZOS)
            img = img.filter(ImageFilter.GaussianBlur(2))
            buf = BytesIO()
            img.save(buf, "WEBP", quality=20)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        data_uri = f"data:image/webp;base64,{b64}"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(data_uri, encoding="utf-8")
        return data_uri
    except Exception as e:
        logger.debug("LQIP generation failed for %s: %s", source, e)
        return None


async def generate_lqip_base64(
    media_root: Path, folder: str, filename: str,
) -> str | None:
    """Generate a tiny base64-encoded blur placeholder for progressive loading.

    Returns a data URI string like 'data:image/webp;base64,...' or None on failure.
    Results are cached to .thumbs/lqip/{folder}/{stem}.b64.
    """
    if not _is_image(filename):
        return None

    resolved = _validate_traversal(media_root, folder, filename)
    if resolved is None:
        return None
    _, source = resolved

    stem = Path(filename).stem
    cache_file = (media_root / ".thumbs" / "lqip" / folder / f"{stem}.b64").resolve()
    thumbs_root = (media_root / ".thumbs").resolve()
    if not cache_file.is_relative_to(thumbs_root):
        return None

    # Return cached result if fresh
    if _is_cache_fresh(source, cache_file):
        try:
            return cache_file.read_text(encoding="utf-8")
        except Exception:
            pass  # regenerate on read failure

    if not source.exists():
        return None

    loop = asyncio.get_running_loop()
    async with _THUMB_SEMAPHORE:
        return await loop.run_in_executor(None, _generate_lqip_sync, source, cache_file)


# ---------------------------------------------------------------------------
# Batch pre-generation (called after backup completes)
# ---------------------------------------------------------------------------

async def pregenerate_video_thumbnails(
    media_root: Path, size: int = 400, max_items: int = 200,
) -> int:
    """Scan media directories for videos without cached thumbnails and generate them.

    Returns count of newly generated thumbnails.
    Intended to be called post-backup so the viewer has poster images ready.
    """
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not found — skipping video thumbnail pre-generation")
        return 0

    generated = 0
    media_root_resolved = media_root.resolve()
    for ext in _VIDEO_EXTENSIONS:
        for video_file in media_root_resolved.rglob(f"*{ext}"):
            if generated >= max_items:
                break
            # Skip files inside .thumbs directory
            if ".thumbs" in video_file.parts:
                continue
            try:
                rel = video_file.relative_to(media_root_resolved)
                folder = str(rel.parent)
                filename = rel.name
            except ValueError:
                continue

            dest = _thumb_path(media_root, size, folder, filename)
            if dest.exists():
                continue  # already cached

            thumb = await ensure_video_thumbnail(media_root, size, folder, filename)
            if thumb:
                generated += 1

    if generated:
        logger.info(f"Pre-generated {generated} video thumbnails")
    return generated
