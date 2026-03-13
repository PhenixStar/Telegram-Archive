"""
Background OCR worker for processing chat images.

Reads vision model config from app_settings (Phase 2 AI Configuration Panel).
Processes images for chats with OCR enabled, rate-limited to avoid GPU saturation.
"""

import asyncio
import base64
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)


class OcrWorker:
    """Async background worker that processes pending OCR jobs."""

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self._task: asyncio.Task | None = None
        self._running = False

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
        """Read vision model config from app_settings."""
        settings = await self.db.get_all_settings()
        return {
            "api_url": settings.get("ai.vision.api_url", "http://localhost:8080/v1"),
            "api_key": settings.get("ai.vision.api_key", ""),
            "model_name": settings.get("ai.vision.model_name", "glm-ocr"),
            "fallback_url": settings.get("ai.vision.fallback_url", ""),
            "fallback_model": settings.get("ai.vision.fallback_model", ""),
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
        abs_path = (
            os.path.join(self.config.backup_path, file_path)
            if not os.path.isabs(file_path)
            else file_path
        )
        if not os.path.exists(abs_path):
            return False

        mime = item.get("mime_type", "image/jpeg") or "image/jpeg"
        if not mime.startswith("image"):
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

        # Try primary endpoint
        ocr_text = await self._call_api(client, f"{api_url}/chat/completions", headers, payload)

        # Try fallback if primary fails
        if ocr_text is None and cfg["fallback_url"]:
            fallback_url = cfg["fallback_url"].rstrip("/")
            fallback_payload = {**payload, "model": cfg["fallback_model"] or cfg["model_name"]}
            ocr_text = await self._call_api(
                client, f"{fallback_url}/chat/completions", headers, fallback_payload
            )

        if ocr_text is not None:
            await self.db.update_ocr_text(item["chat_id"], item["message_id"], ocr_text)
            return True
        return False

    async def _call_api(
        self, client: httpx.AsyncClient, url: str, headers: dict, payload: dict
    ) -> str | None:
        """Call an OpenAI-compatible chat/completions endpoint. Returns text or None."""
        try:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.warning(f"OCR API call failed ({url}): {e}")
            return None
