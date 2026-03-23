"""
Background embedding worker for generating message embeddings.

Processes chats with embedding enabled, rate-limited to avoid GPU saturation.
Reads embedding config from app_settings (same as routes_ai).
"""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


class EmbeddingWorker:
    """Async background worker that generates embeddings for unembedded messages."""

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        """Start the background worker loop."""
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"Embedding worker started (batch={self.config.ollama_embed_batch}, poll=60s)"
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
        logger.info("Embedding worker stopped")

    async def _get_embedding_config(self) -> dict:
        """Read embedding config from app_settings."""
        settings = await self.db.get_all_settings()
        api_url = (settings.get("ai.embedding.api_url", "") or self.config.ollama_url).rstrip("/")
        model = settings.get("ai.embedding.model_name", "") or self.config.ollama_embed_model
        clean_url = api_url[:-3] if api_url.endswith("/v1") else api_url
        if ":11434" in api_url:
            return {"base_url": clean_url, "model_name": model, "api_format": "ollama"}
        return {"base_url": clean_url, "model_name": model, "api_format": "openai"}

    async def _get_enabled_chats(self) -> list[int]:
        """Get list of chat IDs with embedding enabled."""
        settings = await self.db.get_all_settings()
        enabled = []
        for key, value in settings.items():
            if key.startswith("embedding_enabled:") and value == "true":
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
                logger.error(f"Embedding worker cycle error: {e}")
            await asyncio.sleep(60)

    async def _process_cycle(self):
        """One cycle: find enabled chats, embed pending messages."""
        enabled_chats = await self._get_enabled_chats()
        if not enabled_chats:
            return

        emb_cfg = await self._get_embedding_config()
        batch_size = self.config.ollama_embed_batch

        for chat_id in enabled_chats:
            if not self._running:
                break
            try:
                processed = await self._process_chat(chat_id, emb_cfg, batch_size)
                if processed:
                    logger.info(f"Embedding worker: embedded {processed} messages for chat {chat_id}")
            except Exception as e:
                logger.warning(f"Embedding worker: chat {chat_id} failed: {e}")

    async def _process_chat(self, chat_id: int, emb_cfg: dict, batch_size: int) -> int:
        """Embed one batch of unembedded messages for a chat. Returns count stored."""
        messages = await self.db.get_unembedded_messages(chat_id, limit=batch_size)
        if not messages:
            return 0

        texts = [m["text"][:2000] for m in messages]
        vectors = await self._call_embedding_api(emb_cfg, texts)
        if not vectors or len(vectors) != len(messages):
            return 0

        embeddings = [
            {"message_id": messages[i]["id"], "embedding": vectors[i]}
            for i in range(len(messages))
        ]
        stored = await self.db.store_embeddings(chat_id, embeddings, emb_cfg["model_name"])
        return stored

    async def _call_embedding_api(self, emb_cfg: dict, texts: list[str]) -> list[list[float]]:
        """Call embedding API (Ollama or OpenAI-compatible format)."""
        base_url = emb_cfg["base_url"]
        model = emb_cfg["model_name"]
        fmt = emb_cfg["api_format"]

        async with httpx.AsyncClient(timeout=120) as client:
            if fmt == "ollama":
                resp = await client.post(
                    f"{base_url}/api/embed",
                    json={"model": model, "input": texts},
                )
                resp.raise_for_status()
                return resp.json().get("embeddings", [])
            else:
                payload = {"model": model, "input": texts}
                for endpoint in [f"{base_url}/embeddings", f"{base_url}/v1/embeddings"]:
                    resp = await client.post(endpoint, json=payload)
                    if resp.status_code == 404:
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
                    return [item["embedding"] for item in items]
                return []
