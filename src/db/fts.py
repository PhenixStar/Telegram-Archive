"""
SQLite FTS5 full-text search support.

Contentless FTS5 virtual table for message search. Stores its own copy
of text to avoid rowid instability with composite PK tables after VACUUM.

Usage:
    await setup_sqlite_fts(session)
    await rebuild_sqlite_fts(session, set_status_cb, batch_size=1000)
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
    # Strip existing quotes and skip empty tokens
    sanitized = []
    for t in terms:
        cleaned = t.replace('"', "").strip()
        if cleaned:
            sanitized.append(f'"{cleaned}"')
    return " ".join(sanitized)


async def setup_sqlite_fts(session) -> None:
    """Create contentless FTS5 virtual table if it doesn't exist."""
    await session.execute(
        text(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                text, chat_id UNINDEXED, msg_id UNINDEXED,
                content='',
                tokenize='unicode61 remove_diacritics 2'
            )
            """
        )
    )
    await session.commit()
    logger.info("FTS5 virtual table ready")


async def rebuild_sqlite_fts(session, batch_size: int = 1000) -> int:
    """Rebuild contentless FTS5 index by full re-insert.

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
                "SELECT rowid, id, chat_id, text FROM messages "
                "WHERE text IS NOT NULL AND text != '' AND rowid > :last "
                "ORDER BY rowid LIMIT :batch"
            ),
            {"last": last_rowid, "batch": batch_size},
        )
        batch = rows.fetchall()
        if not batch:
            break

        for row in batch:
            await session.execute(
                text(
                    "INSERT INTO messages_fts(rowid, text, chat_id, msg_id) "
                    "VALUES(:rowid, :text, :chat_id, :msg_id)"
                ),
                {
                    "rowid": row.rowid,
                    "text": row.text,
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
