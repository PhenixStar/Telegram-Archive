"""
Background transcription worker for voice notes.

Uses Whisper (via Voicebox) to transcribe voice messages.
Stores results in messages.ocr_text — same field as OCR, so FTS5 search
and embedding pipeline pick up transcriptions automatically.
"""

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Audio types that Whisper can handle
SUPPORTED_EXTENSIONS = {".ogg", ".mp3", ".wav", ".m4a", ".flac", ".opus", ".webm"}

# The pending query selects every voice note whose ocr_text is still NULL, newest
# first, so a note that can never be transcribed — a zero-byte download, or audio
# the service rejects outright — stays at the head of the batch and is re-sent on
# every poll forever. Mirrors the OCR worker: count failures, and after a few give
# the row a sentinel so the queue moves on. A transient outage still retries,
# because the count is in memory and resets when the worker restarts.
_TRANSCRIPTION_FAILED_SENTINEL = "[transcription_failed]"
_MAX_TRANSCRIPTION_ATTEMPTS = 3


class TranscriptionWorker:
    """Async background worker that transcribes voice notes via Whisper."""

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self._task: asyncio.Task | None = None
        self._running = False
        self._failure_counts: dict[tuple[int, int], int] = {}

    async def start(self):
        """Start the background worker loop."""
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Transcription worker started (poll=%ds)", self.config.ocr_poll_interval)

    async def stop(self):
        """Stop the background worker."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Transcription worker stopped")

    async def _get_config(self) -> dict:
        """Read transcription config from app_settings."""
        settings = await self.db.get_all_settings()
        return {
            "api_url": settings.get("ai.transcription.api_url", "") or "http://host.docker.internal:8080",
            "enabled": (settings.get("ai.transcription.enabled", "") or "true") == "true",
            "rate_limit": float(settings.get("ai.transcription.rate_limit", "") or "2"),
            "batch_size": int(settings.get("ai.transcription.batch_size", "") or "50"),
        }

    async def _loop(self):
        """Main worker loop — polls for pending voice notes."""
        while self._running:
            try:
                await self._process_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Transcription worker cycle error: {e}")
            await asyncio.sleep(self.config.ocr_poll_interval)

    async def _process_cycle(self):
        """One cycle: find voice notes needing transcription, process them."""
        # Check global feature flag from DB (None/missing → treat as disabled)
        if await self.db.get_setting("feature.transcription") != "true":
            return
        cfg = await self._get_config()
        if not cfg["enabled"]:
            return

        api_url = cfg["api_url"].rstrip("/")
        if not api_url:
            return

        delay = 1.0 / cfg["rate_limit"] if cfg["rate_limit"] > 0 else 1.0

        pending = await self.db.get_messages_needing_transcription(limit=cfg["batch_size"])
        if not pending:
            return

        processed = 0
        async with httpx.AsyncClient(timeout=120.0) as client:
            for item in pending:
                if not self._running:
                    break
                ok = await self._process_one(client, item, api_url)
                if ok:
                    processed += 1
                await asyncio.sleep(delay)

        if processed:
            logger.info(f"Transcription worker: processed {processed}/{len(pending)} voice notes")

    def _resolve_path(self, file_path: str) -> str | None:
        """Resolve media file path, handling old /home/dgx/ paths."""
        if not file_path:
            return None
        if not os.path.isabs(file_path):
            abs_path = os.path.join(self.config.backup_path, file_path)
        else:
            abs_path = file_path
        # Fallback: extract relative media/ subpath from old absolute paths
        if not os.path.exists(abs_path) and "/media/" in file_path:
            rel = file_path[file_path.index("/media/") + 1:]
            abs_path = os.path.join(self.config.backup_path, rel)
        return abs_path if os.path.exists(abs_path) else None

    async def _mark_failed(self, item: dict, reason: str) -> None:
        """Count a failure and, once it is clearly permanent, stop re-queuing the row."""
        key = (item["chat_id"], item["message_id"])
        self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
        if self._failure_counts[key] >= _MAX_TRANSCRIPTION_ATTEMPTS:
            await self.db.update_ocr_text(item["chat_id"], item["message_id"], _TRANSCRIPTION_FAILED_SENTINEL)
            self._failure_counts.pop(key, None)
            logger.info(
                "Transcription: marked as failed after %d attempts (%s): chat=%s msg=%s",
                _MAX_TRANSCRIPTION_ATTEMPTS, reason, key[0], key[1],
            )

    async def _process_one(self, client: httpx.AsyncClient, item: dict, api_url: str) -> bool:
        """Transcribe a single voice note. Returns True on success."""
        abs_path = self._resolve_path(item["file_path"])
        if not abs_path:
            await self._mark_failed(item, "file missing")
            return False

        # Verify it's an audio file
        ext = os.path.splitext(abs_path)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            return False

        # An interrupted download can leave a zero-byte file that the DB still
        # reports as complete; the service can only answer 400 to it.
        if os.path.getsize(abs_path) == 0:
            await self._mark_failed(item, "empty file")
            return False

        try:
            with open(abs_path, "rb") as f:
                files = {"file": (os.path.basename(abs_path), f, "audio/ogg")}
                resp = await client.post(f"{api_url}/transcribe/file", files=files)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.warning(f"Transcription API error ({api_url}): {e}")
            # 4xx means this audio will be rejected again no matter how often it
            # is retried; 429 and 5xx are worth retrying, so leave them alone.
            status = e.response.status_code if e.response is not None else 0
            if 400 <= status < 500 and status != 429:
                await self._mark_failed(item, f"HTTP {status}")
            return False
        except Exception as e:
            logger.warning(f"Transcription request failed: {e}")
            return False

        data = resp.json()
        text = data.get("text", "").strip()

        if not text:
            # Mark as processed with empty marker to avoid re-processing
            await self.db.update_ocr_text(item["chat_id"], item["message_id"], "[Voice: no speech detected]")
            return True

        # Store with metadata prefix for clarity in search results
        lang = data.get("language", "unknown")
        duration = data.get("duration", 0)
        transcript = f"[Voice {duration:.0f}s, {lang}] {text}"
        await self.db.update_ocr_text(item["chat_id"], item["message_id"], transcript)
        return True
