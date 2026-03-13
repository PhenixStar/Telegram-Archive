"""
SQLite FTS5 full-text search support.

Regular (non-contentless) FTS5 virtual table for message search.
Stores its own copy of text, chat_id, and msg_id so snippet() and
column retrieval work correctly.

Usage:
    await setup_sqlite_fts(session)
    await rebuild_sqlite_fts(session, batch_size=1000)
    sanitized = sanitize_fts_query("hello world")
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)


def sanitize_fts_query(raw_query: str) -> str:
    """Wrap each term in double-quotes to prevent FTS5 query injection.

    Prevents operators like ``* NOT``, ``column:``, and NEAR from being
    interpreted. Empty/whitespace-only input returns empty string.
    """
    terms = raw_query.strip().split()
    if not terms:
        return ""
    sanitized = []
    for t in terms:
        cleaned = t.replace('"', "").strip()
        if cleaned:
            sanitized.append(f'"{cleaned}"')
    return " ".join(sanitized)


async def setup_sqlite_fts(session) -> None:
    """Create FTS5 virtual table if it doesn't exist.

    Drops old contentless table if detected (UNINDEXED columns return NULL).
    Recreates as regular FTS5 so snippet() and column retrieval work.
    """
    # Check if existing table is contentless (UNINDEXED cols return NULL)
    try:
        result = await session.execute(
            text("SELECT chat_id FROM messages_fts LIMIT 1")
        )
        row = result.fetchone()
        if row is not None and row[0] is None:
            logger.info("Detected contentless FTS5 table — dropping for upgrade")
            await session.execute(text("DROP TABLE messages_fts"))
            # Reset FTS status so rebuild triggers
            await session.execute(
                text(
                    "INSERT OR REPLACE INTO app_settings(key, value) "
                    "VALUES('fts_index_status', 'pending')"
                )
            )
            await session.commit()
    except Exception:
        pass  # Table doesn't exist yet, will be created below

    await session.execute(
        text(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                text, chat_id UNINDEXED, msg_id UNINDEXED,
                tokenize='unicode61 remove_diacritics 2'
            )
            """
        )
    )
    await session.commit()
    logger.info("FTS5 virtual table ready")


async def rebuild_sqlite_fts(session, batch_size: int = 1000) -> int:
    """Rebuild FTS5 index by full re-insert.

    Indexes message text + OCR text + AI comments so all content is searchable.

    Args:
        session: SQLAlchemy async session.
        batch_size: Rows per batch commit.

    Returns:
        Total rows indexed.
    """
    # Clear existing index
    await session.execute(text("DELETE FROM messages_fts"))
    await session.commit()

    total = 0
    last_rowid = 0

    while True:
        rows = await session.execute(
            text(
                "SELECT rowid, id, chat_id, text, ocr_text, ai_comment "
                "FROM messages "
                "WHERE ((text IS NOT NULL AND text != '') "
                "    OR (ocr_text IS NOT NULL AND ocr_text != '') "
                "    OR (ai_comment IS NOT NULL AND ai_comment != '')) "
                "  AND rowid > :last "
                "ORDER BY rowid LIMIT :batch"
            ),
            {"last": last_rowid, "batch": batch_size},
        )
        batch = rows.fetchall()
        if not batch:
            break

        for row in batch:
            parts = []
            if row.text:
                parts.append(row.text)
            if row.ocr_text:
                parts.append(row.ocr_text)
            if row.ai_comment:
                parts.append(row.ai_comment)
            combined_text = " ".join(parts)
            if not combined_text.strip():
                continue

            await session.execute(
                text(
                    "INSERT INTO messages_fts(rowid, text, chat_id, msg_id) "
                    "VALUES(:rowid, :text, :chat_id, :msg_id)"
                ),
                {
                    "rowid": row.rowid,
                    "text": combined_text,
                    "chat_id": row.chat_id,
                    "msg_id": row.id,
                },
            )

        last_rowid = batch[-1].rowid
        total += len(batch)

        # Checkpoint for crash recovery
        await session.execute(
            text(
                "INSERT OR REPLACE INTO app_settings(key, value) "
                "VALUES('fts_last_indexed_rowid', :rid)"
            ),
            {"rid": str(last_rowid)},
        )
        await session.commit()

        if total % 10000 == 0 or len(batch) < batch_size:
            logger.info("FTS index progress: %d rows indexed", total)

    return total
