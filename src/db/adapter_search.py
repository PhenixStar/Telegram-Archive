"""Full-text search, AI/OCR, and semantic search operations mixin.

Handles FTS5, AI comments, OCR text, transcription progress, and vector embeddings.
"""

import html as html_mod
import json
import logging
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import selectinload

from .adapter import retry_on_locked
from .models import Media, Message, MessageEmbedding, User

logger = logging.getLogger(__name__)


class SearchMixin:
    """Mixin providing FTS5, AI/OCR, and semantic search operations.

    Assumes ``self.db_manager`` and ``self._is_sqlite`` are set by the host class.
    """

    # ========================================================================
    # FTS5 Full-Text Search (SQLite only)
    # ========================================================================

    async def init_fts(self) -> None:
        """Initialize FTS5 virtual table (SQLite only, no-op on PostgreSQL)."""
        if not self._is_sqlite:
            logger.info("FTS5 skipped: PostgreSQL detected")
            return
        from .fts import setup_sqlite_fts

        async with self.db_manager.async_session_factory() as session:
            await setup_sqlite_fts(session)

    async def rebuild_fts_index(self) -> int:
        """Rebuild the FTS5 index from scratch. Returns rows indexed."""
        if not self._is_sqlite:
            return 0
        from .fts import rebuild_sqlite_fts

        async with self.db_manager.async_session_factory() as session:
            return await rebuild_sqlite_fts(session)

    async def incremental_fts_index(self) -> int:
        """Index new messages since last checkpoint. Returns rows indexed."""
        if not self._is_sqlite:
            return 0
        from .fts import incremental_index_fts

        async with self.db_manager.async_session_factory() as session:
            return await incremental_index_fts(session)

    async def get_fts_status(self) -> str | None:
        """Get FTS index build status from app_settings."""
        return await self.get_setting("fts_index_status")

    async def set_fts_status(self, status: str) -> None:
        """Set FTS index build status in app_settings."""
        await self.set_setting("fts_index_status", status)

    @staticmethod
    def _build_fts_where(
        sanitized_query: str,
        chat_id: int | None,
        allowed_chat_ids: set[int] | None,
    ) -> tuple[str, dict[str, Any]]:
        """Build WHERE clause + params for FTS5 queries with access control."""
        where_parts = ["fts.text MATCH :query"]
        params: dict[str, Any] = {"query": sanitized_query}

        if chat_id is not None:
            where_parts.append("fts.chat_id = :chat_id")
            params["chat_id"] = chat_id

        if allowed_chat_ids is not None:
            placeholders = ", ".join(
                f":acid{i}" for i in range(len(allowed_chat_ids))
            )
            where_parts.append(f"fts.chat_id IN ({placeholders})")
            for i, cid in enumerate(allowed_chat_ids):
                params[f"acid{i}"] = cid

        return " AND ".join(where_parts), params

    async def search_messages_fts(
        self,
        query: str,
        chat_id: int | None,
        allowed_chat_ids: set[int] | None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Search messages using FTS5 with access control."""
        from .fts import sanitize_fts_query

        sanitized = sanitize_fts_query(query)
        if not sanitized:
            return []

        async with self.db_manager.async_session_factory() as session:
            where_clause, params = self._build_fts_where(sanitized, chat_id, allowed_chat_ids)
            params["lim"] = limit
            params["off"] = offset

            sql = f"""
                SELECT m.id, m.chat_id, m.text, m.date, m.sender_id,
                       u.first_name, u.last_name, u.username,
                       snippet(messages_fts, 0, '<b>', '</b>', '...', 40) as snippet,
                       rank
                FROM messages_fts fts
                JOIN messages m ON m.id = fts.msg_id AND m.chat_id = fts.chat_id
                LEFT JOIN users u ON u.id = m.sender_id
                WHERE {where_clause}
                ORDER BY rank
                LIMIT :lim OFFSET :off
            """

            try:
                result = await session.execute(text(sql), params)
                rows = result.fetchall()
            except Exception as e:
                logger.warning("FTS query failed: %s", e)
                return []

            return [
                {
                    "id": row.id,
                    "chat_id": row.chat_id,
                    "text": row.text,
                    "date": row.date.isoformat() if hasattr(row.date, 'isoformat') else row.date,
                    "sender_id": row.sender_id,
                    "first_name": row.first_name,
                    "last_name": row.last_name,
                    "username": row.username,
                    "snippet": self._sanitize_fts_snippet(row.snippet),
                }
                for row in rows
            ]

    @staticmethod
    def _sanitize_fts_snippet(snippet: str | None) -> str | None:
        """Escape HTML in FTS snippet while preserving <b> highlight markers."""
        if not snippet:
            return snippet
        # Replace FTS markers with placeholders, escape, then restore
        s = snippet.replace("<b>", "\x00B\x00").replace("</b>", "\x00/B\x00")
        s = html_mod.escape(s)
        return s.replace("\x00B\x00", "<b>").replace("\x00/B\x00", "</b>")

    async def count_fts_matches(
        self,
        query: str,
        chat_id: int | None,
        allowed_chat_ids: set[int] | None,
    ) -> int:
        """Count total FTS matches for a query (for pagination info)."""
        from .fts import sanitize_fts_query

        sanitized = sanitize_fts_query(query)
        if not sanitized:
            return 0

        async with self.db_manager.async_session_factory() as session:
            where_clause, params = self._build_fts_where(sanitized, chat_id, allowed_chat_ids)
            sql = f"SELECT count(*) FROM messages_fts fts WHERE {where_clause}"

            try:
                result = await session.execute(text(sql), params)
                return result.scalar() or 0
            except Exception:
                return 0

    async def insert_fts_entry(self, rowid: int, msg_id: int, chat_id: int, msg_text: str) -> None:
        """Insert a single message into the FTS index (for real-time sync)."""
        if not self._is_sqlite or not msg_text:
            return
        try:
            async with self.db_manager.async_session_factory() as session:
                await session.execute(
                    text(
                        "INSERT OR IGNORE INTO messages_fts(rowid, text, chat_id, msg_id) "
                        "VALUES(:rowid, :text, :chat_id, :msg_id)"
                    ),
                    {"rowid": rowid, "text": msg_text, "chat_id": chat_id, "msg_id": msg_id},
                )
                await session.commit()
        except Exception as e:
            logger.debug("FTS insert skipped: %s", e)

    # ========================================================================
    # AI Assistant Methods (v8.0)
    # ========================================================================

    async def update_ai_comment(self, chat_id: int, message_id: int, comment: str) -> bool:
        """Store an AI-generated comment/annotation on a message."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                select(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id))
            )
            msg = result.scalar_one_or_none()
            if not msg:
                return False
            msg.ai_comment = comment
            await session.commit()
            return True

    async def update_ocr_text(self, chat_id: int, message_id: int, ocr_text: str) -> bool:
        """Store OCR-extracted text for a message's image/document."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                select(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id))
            )
            msg = result.scalar_one_or_none()
            if not msg:
                return False
            msg.ocr_text = ocr_text
            await session.commit()
            return True

    async def get_messages_needing_ocr(self, chat_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """Get messages with images that haven't been OCR'd yet."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Message.id, Message.chat_id, Media.file_path, Media.type, Media.mime_type)
                .join(Media, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
                .where(
                    and_(
                        Message.chat_id == chat_id,
                        Message.ocr_text.is_(None),
                        Media.type.in_(["photo", "document"]),
                        Media.downloaded == 1,
                        # Exclude PDFs/videos/audio — only process images
                        ~Media.file_path.ilike("%.pdf"),
                        ~Media.file_path.ilike("%.mp4"),
                        ~Media.file_path.ilike("%.mp3"),
                        ~Media.file_path.ilike("%.ogg"),
                    )
                )
                .order_by(Message.date.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [
                {"message_id": r[0], "chat_id": r[1], "file_path": r[2], "type": r[3], "mime_type": r[4]}
                for r in result
            ]

    async def get_messages_needing_transcription(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get voice messages that haven't been transcribed yet (across all chats)."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Message.id, Message.chat_id, Media.file_path)
                .join(Media, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
                .where(
                    and_(
                        Message.ocr_text.is_(None),
                        Media.type == "voice",
                        Media.downloaded == 1,
                    )
                )
                .order_by(Message.date.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [
                {"message_id": r[0], "chat_id": r[1], "file_path": r[2]}
                for r in result
            ]

    async def get_ocr_progress(self, chat_id: int) -> dict[str, int]:
        """Get OCR processing progress for a chat: how many photos processed vs total."""
        async with self.db_manager.async_session_factory() as session:
            # Total photos with downloaded files
            total_result = await session.execute(
                select(func.count(Media.id))
                .join(Message, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
                .where(
                    and_(
                        Media.chat_id == chat_id,
                        Media.type.in_(["photo", "document"]),
                        Media.downloaded == 1,
                    )
                )
            )
            total = total_result.scalar() or 0

            # Photos already OCR'd (ocr_text is not null)
            processed_result = await session.execute(
                select(func.count(Media.id))
                .join(Message, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
                .where(
                    and_(
                        Media.chat_id == chat_id,
                        Media.type.in_(["photo", "document"]),
                        Media.downloaded == 1,
                        Message.ocr_text.isnot(None),
                    )
                )
            )
            processed = processed_result.scalar() or 0

            return {"processed": processed, "total": total}

    async def get_transcription_progress(self) -> dict[str, int]:
        """Get global voice transcription progress: processed vs total."""
        async with self.db_manager.async_session_factory() as session:
            total_result = await session.execute(
                select(func.count(Media.id))
                .join(Message, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
                .where(and_(Media.type == "voice", Media.downloaded == 1))
            )
            total = total_result.scalar() or 0
            processed_result = await session.execute(
                select(func.count(Media.id))
                .join(Message, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
                .where(and_(Media.type == "voice", Media.downloaded == 1, Message.ocr_text.isnot(None)))
            )
            processed = processed_result.scalar() or 0
            return {"processed": processed, "total": total}

    async def get_ai_context_for_chat(self, chat_id: int, limit: int = 30) -> list[dict[str, Any]]:
        """Get recent messages with AI annotations for AI context building."""
        async with self.db_manager.async_session_factory() as session:
            # Use a subquery for media to avoid row duplication when a message has multiple media
            media_sub = (
                select(
                    Media.message_id,
                    Media.chat_id,
                    func.min(Media.type).label("media_type"),
                )
                .where(Media.chat_id == chat_id)
                .group_by(Media.message_id, Media.chat_id)
                .subquery()
            )
            stmt = (
                select(
                    Message.id, Message.text, Message.ai_comment, Message.ocr_text,
                    Message.date, User.first_name, User.last_name,
                    media_sub.c.media_type,
                )
                .outerjoin(User, Message.sender_id == User.id)
                .outerjoin(media_sub, and_(
                    media_sub.c.message_id == Message.id,
                    media_sub.c.chat_id == Message.chat_id,
                ))
                .where(Message.chat_id == chat_id)
                .order_by(Message.date.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [
                {
                    "id": r[0], "text": r[1], "ai_comment": r[2], "ocr_text": r[3],
                    "date": str(r[4]), "sender": f"{r[5] or ''} {r[6] or ''}".strip(),
                    "media_type": r[7],
                }
                for r in result
            ]

    # ------------------------------------------------------------------
    # Semantic search (v9.1.0)
    # ------------------------------------------------------------------

    async def get_unembedded_messages(self, chat_id: int, limit: int = 100) -> list[dict]:
        """Get messages that haven't been embedded yet."""
        async with self.db_manager.async_session_factory() as session:
            embedded_subq = select(MessageEmbedding.message_id).where(
                MessageEmbedding.chat_id == chat_id
            )
            stmt = (
                select(Message.id, Message.text, Message.ocr_text)
                .where(Message.chat_id == chat_id)
                .where(Message.id.notin_(embedded_subq))
                .where(or_(Message.text.isnot(None), Message.ocr_text.isnot(None)))
                .where(or_(Message.text != "", Message.ocr_text != ""))
                .order_by(Message.date.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.all()
            return [
                {
                    "id": r.id,
                    "text": (r.text or "") + (" " + r.ocr_text if r.ocr_text else ""),
                }
                for r in rows
            ]

    async def store_embeddings(self, chat_id: int, embeddings: list[dict], model: str) -> int:
        """Store message embeddings. Each dict has 'message_id' and 'embedding' (list of floats)."""
        stored = 0
        async with self.db_manager.async_session_factory() as session:
            for emb in embeddings:
                obj = MessageEmbedding(
                    message_id=emb["message_id"],
                    chat_id=chat_id,
                    model=model,
                    embedding=json.dumps(emb["embedding"]),
                )
                session.add(obj)
                stored += 1
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        return stored

    async def get_embedding_count(self, chat_id: int) -> dict:
        """Get embedding progress for a chat."""
        async with self.db_manager.async_session_factory() as session:
            total_stmt = (
                select(func.count())
                .select_from(Message)
                .where(Message.chat_id == chat_id)
                .where(or_(Message.text.isnot(None), Message.ocr_text.isnot(None)))
                .where(or_(Message.text != "", Message.ocr_text != ""))
            )
            total = (await session.execute(total_stmt)).scalar() or 0

            embedded_stmt = (
                select(func.count())
                .select_from(MessageEmbedding)
                .where(MessageEmbedding.chat_id == chat_id)
            )
            embedded = (await session.execute(embedded_stmt)).scalar() or 0
        return {"total": total, "embedded": embedded}

    async def semantic_search(
        self, chat_id: int, query_embedding: list[float], limit: int = 20
    ) -> list[dict]:
        """Find messages most similar to query_embedding using cosine similarity."""
        import math

        async with self.db_manager.async_session_factory() as session:
            stmt = select(MessageEmbedding).where(MessageEmbedding.chat_id == chat_id)
            result = await session.execute(stmt)
            rows = result.scalars().all()

        if not rows:
            return []

        def cosine_sim(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            norm_a = math.sqrt(sum(x * x for x in a))
            norm_b = math.sqrt(sum(x * x for x in b))
            return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

        scored = []
        for row in rows:
            emb = json.loads(row.embedding)
            sim = cosine_sim(query_embedding, emb)
            scored.append((row.message_id, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:limit]
        if not top:
            return []

        msg_ids = [m[0] for m in top]
        sim_map = {m[0]: m[1] for m in top}

        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Message)
                .options(selectinload(Message.sender))
                .where(Message.chat_id == chat_id)
                .where(Message.id.in_(msg_ids))
            )
            result = await session.execute(stmt)
            msgs = result.scalars().all()

        results = []
        for msg in msgs:
            results.append(
                {
                    "id": msg.id,
                    "chat_id": chat_id,
                    "text": msg.text,
                    "date": msg.date.isoformat() if msg.date else None,
                    "sender_name": msg.sender.first_name if msg.sender else None,
                    "similarity": round(sim_map.get(msg.id, 0), 4),
                }
            )
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results
