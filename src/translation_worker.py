"""
Background translation worker for auto-translating text messages.

Reads translation config from app_settings (AI Configuration Panel).
Processes text messages for chats with translation enabled, rate-limited.
Stores results in messages.ocr_text with [Translation -> lang] prefix.
"""

import asyncio
import logging
import re

import httpx

# Strip <think>...</think> blocks from LLM responses (Qwen3, etc.)
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

logger = logging.getLogger(__name__)


class TranslationWorker:
    """Async background worker that translates pending text messages."""

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        """Start the background worker loop."""
        if not self.config.translation_enabled:
            logger.info("Translation worker disabled (TRANSLATION_ENABLED=false)")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"Translation worker started (rate={self.config.translation_rate_limit}/s, "
            f"batch={self.config.translation_batch_size}, "
            f"poll={self.config.translation_poll_interval}s)"
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
        logger.info("Translation worker stopped")

    async def _get_translation_config(self) -> dict:
        """Read translation config from app_settings."""
        settings = await self.db.get_all_settings()
        return {
            "api_url": settings.get("ai.translation.api_url", "") or "http://host.docker.internal:11434/v1",
            "api_key": settings.get("ai.translation.api_key", "") or "",
            "model_name": settings.get("ai.translation.model_name", "") or "qwen3-next-80b",
            "target_lang": settings.get("ai.translation.target_lang", "") or self.config.translation_target_lang,
            "enabled": (settings.get("ai.translation.enabled", "") or "true") == "true",
            "rate_limit": float(settings.get("ai.translation.rate_limit", "") or "2"),
            "batch_size": int(settings.get("ai.translation.batch_size", "") or "20"),
            "fallback_url": settings.get("ai.translation.fallback_url", "") or "",
            "fallback_model": settings.get("ai.translation.fallback_model", "") or "",
        }

    async def _get_enabled_chats(self) -> list[int]:
        """Get list of chat IDs with translation enabled."""
        settings = await self.db.get_all_settings()
        enabled = []
        for key, value in settings.items():
            if key.startswith("translation_enabled:") and value == "true":
                try:
                    chat_id = int(key.split(":", 1)[1])
                    enabled.append(chat_id)
                except (ValueError, IndexError):
                    continue
        return enabled

    async def _loop(self):
        """Main worker loop -- polls for pending work."""
        while self._running:
            try:
                await self._process_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Translation worker cycle error: {e}")
            await asyncio.sleep(self.config.translation_poll_interval)

    async def _process_cycle(self):
        """One cycle: find enabled chats, translate pending messages."""
        if not self.config.translation_enabled:
            return
        # Check global feature flag from DB (None/missing → treat as disabled)
        if await self.db.get_setting("feature.translation") != "true":
            return

        cfg = await self._get_translation_config()
        if not cfg["enabled"]:
            return

        enabled_chats = await self._get_enabled_chats()
        if not enabled_chats:
            return

        api_url = cfg["api_url"].rstrip("/")
        if not api_url:
            return

        delay = 1.0 / cfg["rate_limit"] if cfg["rate_limit"] > 0 else 2.0

        async with httpx.AsyncClient(timeout=120.0) as client:
            for chat_id in enabled_chats:
                if not self._running:
                    break
                pending = await self.db.get_messages_needing_translation(
                    chat_id, limit=cfg["batch_size"]
                )
                if not pending:
                    continue

                processed = 0
                for item in pending:
                    if not self._running:
                        break
                    ok = await self._process_one(client, item, cfg, api_url)
                    if ok:
                        processed += 1
                    await asyncio.sleep(delay)

                if processed:
                    logger.info(
                        f"Translation worker: translated {processed}/{len(pending)} "
                        f"messages for chat {chat_id}"
                    )

    async def _process_one(
        self, client: httpx.AsyncClient, item: dict, cfg: dict, api_url: str
    ) -> bool:
        """Translate a single message. Returns True on success."""
        text = (item.get("text") or "").strip()
        if not text:
            return False

        # Skip very short messages (emojis, single words unlikely to need translation)
        if len(text) < 3:
            return False

        target_lang = cfg["target_lang"]
        headers = {"Content-Type": "application/json"}
        if cfg["api_key"]:
            headers["Authorization"] = f"Bearer {cfg['api_key']}"

        payload = {
            "model": cfg["model_name"],
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"You are a translator. Translate the user's message to {target_lang}. "
                        f"Return ONLY the translation, nothing else. "
                        f"If the message is already in {target_lang}, respond with exactly: "
                        f"[No translation needed]"
                    ),
                },
                {"role": "user", "content": text},
            ],
            "max_tokens": 2048,
            "temperature": 0.1,
        }

        # Try primary endpoint
        result = await self._call_api(client, f"{api_url}/chat/completions", headers, payload)

        # Try fallback if primary fails
        if result is None and cfg["fallback_url"]:
            fallback_url = cfg["fallback_url"].rstrip("/")
            fallback_payload = {**payload, "model": cfg["fallback_model"] or cfg["model_name"]}
            result = await self._call_api(
                client, f"{fallback_url}/chat/completions", headers, fallback_payload
            )

        if result is None:
            return False

        # Clean LLM thinking tags
        result = _THINK_RE.sub("", result).strip()

        # Skip if already in target language
        if result == "[No translation needed]":
            # Mark as processed so we don't re-check
            await self.db.update_ocr_text(
                item["chat_id"], item["message_id"], f"[Translation → {target_lang}] ≡"
            )
            return True

        # Store with translation prefix
        translated = f"[Translation → {target_lang}] {result}"
        await self.db.update_ocr_text(item["chat_id"], item["message_id"], translated)
        return True

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
            logger.warning(f"Translation API call failed ({url}): {e}")
            return None
