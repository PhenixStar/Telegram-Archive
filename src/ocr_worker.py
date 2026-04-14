"""
Background OCR worker for processing chat images.

Reads vision model config from app_settings (Phase 2 AI Configuration Panel).
Processes images for chats with OCR enabled, rate-limited to avoid GPU saturation.
"""

import asyncio
import base64
import logging
import mimetypes
import os
import re

import httpx

# Strip <think>...</think> blocks from LLM responses (Qwen3, etc.)
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

logger = logging.getLogger(__name__)

_MAX_OCR_ATTEMPTS = 3
_BACKOFF_BASE = 2.0  # seconds
_OCR_FAILED_SENTINEL = "[ocr_failed]"


class _TransientOcrError(Exception):
    """Retryable OCR API error (5xx, timeout, connection)."""


class _PermanentOcrError(Exception):
    """Non-retryable OCR API error (4xx, parse failure)."""


class OcrWorker:
    """Async background worker that processes pending OCR jobs."""

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self._task: asyncio.Task | None = None
        self._running = False
        self._failure_counts: dict[tuple[int, int], int] = {}  # (chat_id, msg_id) -> count

    async def start(self):
        """Start the background worker loop."""
        if not self.config.ocr_enabled:
            logger.info("OCR worker disabled (OCR_ENABLED=false)")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"OCR worker started (rate={self.config.ocr_rate_limit}/s, "
            f"batch={self.config.ocr_batch_size}, poll={self.config.ocr_poll_interval}s)"
        )

    async def stop(self):
        """Stop the background worker."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("OCR worker stopped")

    async def _get_vision_config(self) -> dict:
        """Read vision model config from app_settings.

        Uses `or` to handle empty-string values stored in the DB
        (keys exist but are blank) by falling back to sensible defaults.
        """
        settings = await self.db.get_all_settings()
        return {
            "api_url": settings.get("ai.vision.api_url", "") or "http://host.docker.internal:8081/v1",
            "api_key": settings.get("ai.vision.api_key", "") or "",
            "model_name": settings.get("ai.vision.model_name", "") or "glm-ocr",
            "fallback_url": settings.get("ai.vision.fallback_url", "") or "http://host.docker.internal:11434/v1",
            "fallback_model": settings.get("ai.vision.fallback_model", "") or "gemma3:27b",
        }

    async def _get_enabled_chats(self) -> list[int]:
        """Get list of chat IDs with OCR enabled."""
        settings = await self.db.get_all_settings()
        enabled = []
        for key, value in settings.items():
            if key.startswith("ocr_enabled:") and value == "true":
                try:
                    chat_id = int(key.split(":", 1)[1])
                    enabled.append(chat_id)
                except (ValueError, IndexError):
                    continue
        return enabled

    async def _loop(self):
        """Main worker loop — polls for pending work."""
        while self._running:
            try:
                await self._process_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"OCR worker cycle error: {e}")
            await asyncio.sleep(self.config.ocr_poll_interval)

    async def _process_cycle(self):
        """One cycle: find enabled chats, process pending images."""
        if not self.config.ocr_enabled:
            return
        # Check global feature flag from DB (None/missing → treat as disabled)
        if await self.db.get_setting("feature.ocr") != "true":
            return

        enabled_chats = await self._get_enabled_chats()
        if not enabled_chats:
            return

        vision_cfg = await self._get_vision_config()
        api_url = vision_cfg["api_url"].rstrip("/")
        if not api_url:
            return

        delay = 1.0 / self.config.ocr_rate_limit if self.config.ocr_rate_limit > 0 else 2.0

        async with httpx.AsyncClient(timeout=90.0) as client:
            for chat_id in enabled_chats:
                if not self._running:
                    break
                pending = await self.db.get_messages_needing_ocr(
                    chat_id, limit=self.config.ocr_batch_size
                )
                if not pending:
                    continue

                processed = 0
                for item in pending:
                    if not self._running:
                        break
                    ok = await self._process_one(
                        client, item, vision_cfg, api_url
                    )
                    if ok:
                        processed += 1
                    await asyncio.sleep(delay)

                if processed:
                    logger.info(
                        f"OCR worker: processed {processed}/{len(pending)} images for chat {chat_id}"
                    )

    async def _process_one(
        self, client: httpx.AsyncClient, item: dict, cfg: dict, api_url: str
    ) -> bool:
        """Process a single image. Returns True on success."""
        file_path = item["file_path"]
        # Resolve path: relative → join with backup_path, absolute → use directly
        if not os.path.isabs(file_path):
            abs_path = os.path.join(self.config.backup_path, file_path)
        else:
            abs_path = file_path
        # Fallback: extract relative media/ subpath from old absolute paths
        if not os.path.exists(abs_path) and "/media/" in file_path:
            rel = file_path[file_path.index("/media/") + 1:]
            abs_path = os.path.join(self.config.backup_path, rel)
        if not os.path.exists(abs_path):
            return False

        # Detect MIME from file extension when DB value is missing
        mime = item.get("mime_type") or ""
        if not mime:
            mime, _ = mimetypes.guess_type(abs_path)
            mime = mime or ""
        # Only process raster image formats Ollama can handle
        supported = {"image/jpeg", "image/png", "image/gif", "image/bmp", "image/tiff"}
        if mime not in supported:
            if mime.startswith("image/"):
                logger.debug(f"OCR: skipping unsupported image format {mime}: {abs_path}")
            return False

        try:
            with open(abs_path, "rb") as f:
                img_data = base64.b64encode(f.read()).decode()
        except Exception as e:
            logger.warning(f"OCR: cannot read {abs_path}: {e}")
            return False

        headers = {"Content-Type": "application/json"}
        if cfg["api_key"]:
            headers["Authorization"] = f"Bearer {cfg['api_key']}"

        payload = {
            "model": cfg["model_name"],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Extract ALL text from this image. Return only the extracted text, nothing else. If no text, describe the image briefly.",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{img_data}"},
                        },
                    ],
                }
            ],
            "max_tokens": 2048,
        }

        key = (item["chat_id"], item["message_id"])

        # Build list of (url, payload) attempts: primary, then fallback
        endpoints = [(f"{api_url}/chat/completions", payload)]
        if cfg["fallback_url"]:
            fallback_url = cfg["fallback_url"].rstrip("/")
            fallback_payload = {**payload, "model": cfg["fallback_model"] or cfg["model_name"]}
            endpoints.append((f"{fallback_url}/chat/completions", fallback_payload))

        for url, ep_payload in endpoints:
            for attempt in range(2):  # max 2 tries per endpoint (initial + 1 retry)
                try:
                    ocr_text = await self._call_api(client, url, headers, ep_payload)
                    if ocr_text is not None:
                        ocr_text = _THINK_RE.sub("", ocr_text).strip()
                        await self.db.update_ocr_text(item["chat_id"], item["message_id"], ocr_text)
                        self._failure_counts.pop(key, None)
                        return True
                except _TransientOcrError:
                    if attempt == 0:
                        await asyncio.sleep(_BACKOFF_BASE * (2 ** attempt))
                        continue
                    break  # exhausted retries, try next endpoint
                except _PermanentOcrError:
                    break  # skip retries, try next endpoint

        # All endpoints failed — track cumulative failures
        self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
        if self._failure_counts[key] >= _MAX_OCR_ATTEMPTS:
            await self.db.update_ocr_text(item["chat_id"], item["message_id"], _OCR_FAILED_SENTINEL)
            self._failure_counts.pop(key, None)
            logger.info(f"OCR: marked as failed after {_MAX_OCR_ATTEMPTS} attempts: chat={key[0]} msg={key[1]}")
        return False

    async def _call_api(
        self, client: httpx.AsyncClient, url: str, headers: dict, payload: dict
    ) -> str | None:
        """Call an OpenAI-compatible chat/completions endpoint.

        Returns extracted text on success.
        Raises _TransientOcrError for retryable failures (5xx, timeout).
        Raises _PermanentOcrError for non-retryable failures (4xx, parse).
        """
        try:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                logger.error(f"OCR API 5xx ({url}): {e.response.status_code}")
                raise _TransientOcrError() from e
            logger.warning(f"OCR API {e.response.status_code} (permanent, {url})")
            raise _PermanentOcrError() from e
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            logger.warning(f"OCR API connection/timeout ({url}): {e}")
            raise _TransientOcrError() from e
        except Exception as e:
            logger.warning(f"OCR API unexpected error ({url}): {e}")
            raise _PermanentOcrError() from e
