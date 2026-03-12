# Phase 08: Smart Indexing / FTS Worker

## Context

- [Research: Search UX, DB Indexing, AI Panel](../reports/researcher-260311-1743-search-indexing-ai-panel.md)
- Current search: `ILIKE %term%` on `messages.text` (adapter.py line 1117-1118) -- full table scan
- Messages table: composite PK `(id, chat_id)`, SQLite internal `rowid` auto-assigned
- DB type detected at runtime: `self.db_manager.engine.url.drivername` reveals `sqlite` or `postgresql`
- `AppSettings` table used for key-value config storage
- Scheduler runs backup on cron schedule; backup completion is a natural trigger for re-indexing

## Overview

- **Priority:** HIGH
- **Status:** Pending
- **Description:** Add FTS5 (SQLite only) full-text search, background indexing worker, new search API endpoint, ILIKE fallback. ~~PostgreSQL tsvector deferred per red team -- determine production DB first~~

## Key Insights

- **[RED TEAM]** SQLite FTS5 only -- PostgreSQL tsvector deferred until production DB is determined. Halves implementation + testing surface.
- SQLite FTS5: separate virtual table `messages_fts`, synced via triggers on `messages` table
- SQLite composite PK caveat: FTS5 content sync uses internal `rowid`. Works correctly -- SQLite always has `rowid` even with composite PKs.
- Tokenizer choice: `unicode61 remove_diacritics 2` -- best for multilingual chat text
- **[RED TEAM]** MATCH terms must be quoted to prevent FTS query injection (`* NOT term`, `column:value`)
- Initial index build may be slow on large DBs (millions of messages). Must run in background with progress tracking.
- Graceful degradation: if FTS not yet built, fall back to ILIKE

## Requirements

**Functional:**
- SQLite FTS5: create virtual table with triggers to keep in sync
- ~~PostgreSQL deferred per red team~~
- Background asyncio worker: rebuilds index on startup + after each backup
- New API: `GET /api/search?q=term&chat_id=optional&limit=50&offset=0`
- **[RED TEAM]** `allowed_chat_ids` MANDATORY in search -- filter results to user's permitted chats
- **[RED TEAM]** Rate limit via `slowapi` library: 10 req/min per viewer, 30/min per admin. Max query length 200 chars. DB query timeout.
- Adapter method: `search_messages_fts(query, chat_id, allowed_chat_ids, limit, offset)` with ranking
- **[RED TEAM]** Sanitize MATCH input: wrap each user term in double-quotes before MATCH
- Fall back to ILIKE if FTS not available
- Track index status in `app_settings` (key: `fts_index_status`, values: `building`, `ready`, `error`)

**Non-functional:**
- Index build must not block app startup
- Index build should log progress periodically
- FTS queries must be significantly faster than ILIKE for large datasets

## Architecture

```
Startup:
  1. Check if FTS index exists (SQLite: table exists, PG: column exists)
  2. If not: create schema (table/column/trigger/index)
  3. Start background worker: populate index for existing messages
  4. Set app_settings fts_index_status = 'building'
  5. On completion: set fts_index_status = 'ready'

After backup:
  1. SQLite: triggers handle new messages automatically; worker runs 'rebuild' for safety
  2. PostgreSQL: trigger handles new inserts; worker updates NULL text_search rows

Search API:
  GET /api/search?q=term&chat_id=123
    -> Check fts_index_status
    -> If 'ready': use FTS query with ranking
    -> If 'building' or 'error': fall back to ILIKE
    -> Return { results: [...], total, has_more, method: 'fts'|'ilike' }
```

## Related Code Files

**Create:**
- `src/db/fts.py` -- SQLite FTS5 setup functions (create tables/indexes, rebuild logic, status tracking, query sanitizer)

**Modify:**
- `src/db/adapter.py` -- add `search_messages_fts()`, `init_fts()`, `get_fts_status()` methods
- `src/web/main.py` -- add `GET /api/search` endpoint with auth + rate limit, start FTS worker in lifespan

## Implementation Steps

1. **Create `src/db/fts.py`** (SQLite FTS5 only):

   **SQLite FTS5 setup:**
   ```python
   async def setup_sqlite_fts(session):
       # [RED TEAM] Use contentless FTS5 (content='') to avoid rowid instability
       # with composite PK tables after VACUUM. Trade-off: stores its own copy of text.
       await session.execute(text("""
           CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
               text, chat_id UNINDEXED, msg_id UNINDEXED,
               content='',
               tokenize='unicode61 remove_diacritics 2'
           )
       """))
       # No sync triggers needed for contentless FTS -- must INSERT/DELETE manually
       # when messages are added/removed (handle in adapter insert/delete methods)
   ```

   **[RED TEAM] MATCH input sanitizer (mandatory):**
   ```python
   def sanitize_fts_query(raw_query: str) -> str:
       """Wrap each term in double-quotes to prevent FTS5 query injection."""
       terms = raw_query.strip().split()
       return ' '.join(f'"{t}"' for t in terms if t)
   ```

   **Rebuild function (contentless -- full re-insert):**
   ```python
   async def rebuild_sqlite_fts(session, batch_size=1000):
       """Contentless FTS5 cannot use 'rebuild' command. Must DELETE all + re-INSERT."""
       await session.execute(text("DELETE FROM messages_fts"))
       last_rowid = 0  # checkpoint for crash recovery
       while True:
           rows = await session.execute(text(
               "SELECT rowid, id, chat_id, text FROM messages WHERE rowid > :last ORDER BY rowid LIMIT :batch"
           ), {"last": last_rowid, "batch": batch_size})
           batch = rows.fetchall()
           if not batch:
               break
           for row in batch:
               await session.execute(text(
                   "INSERT INTO messages_fts(rowid, text, chat_id, msg_id) VALUES(:rowid, :text, :chat_id, :msg_id)"
               ), {"rowid": row.rowid, "text": row.text, "chat_id": row.chat_id, "msg_id": row.id})
           last_rowid = batch[-1].rowid
           # [RED TEAM] Save checkpoint for crash recovery
           await session.execute(text(
               "INSERT OR REPLACE INTO app_settings(key, value) VALUES('fts_last_indexed_rowid', :rid)"
           ), {"rid": str(last_rowid)})
           await session.commit()
   ```

2. **Adapter methods** (`adapter.py`):

   `init_fts()` -- called on startup, runs setup + triggers creation
   `rebuild_fts_index()` -- called by background worker
   `get_fts_status()` -- reads `app_settings` key `fts_index_status`
   `set_fts_status(status)` -- writes to `app_settings`

   `search_messages_fts(query, chat_id, allowed_chat_ids, limit, offset)`:
   - **[RED TEAM]** `allowed_chat_ids` is MANDATORY -- `WHERE m.chat_id IN (...)` filter always applied
   - SQLite (contentless): `SELECT ... FROM messages_fts fts JOIN messages m ON m.id = fts.msg_id AND m.chat_id = fts.chat_id WHERE fts.text MATCH ? AND fts.chat_id IN (?)`
   - Use `sanitize_fts_query()` before passing to MATCH
   - Include `snippet()` for highlighted excerpts
   - Return list of dicts with `id, chat_id, text, date, sender_name, snippet`

3. **Background worker** (`main.py` lifespan):
   ```python
   async def fts_index_worker():
       await db.init_fts()
       await db.set_fts_status('building')
       try:
           await db.rebuild_fts_index()
           await db.set_fts_status('ready')
       except Exception as e:
           logger.error(f"FTS index build failed: {e}")
           await db.set_fts_status('error')
   ```
   Start as `asyncio.create_task(fts_index_worker())` in lifespan startup.
   Also trigger after backup completion (add hook in scheduler or use signal).

4. **Search API endpoint** (`main.py`):
   ```python
   @app.get("/api/search")
   async def search_messages(q: str, chat_id: int | None = None, limit: int = 50, offset: int = 0,
                              user=Depends(require_auth)):
       # [RED TEAM] Rate limit: 10 req/min viewers, 30/min admins
       # [RED TEAM] Max query length
       if len(q) > 200:
           raise HTTPException(400, "Query too long (max 200 chars)")
       allowed_chat_ids = user.allowed_chat_ids  # MANDATORY filter
       status = await db.get_fts_status()
       if status == 'ready':
           results = await db.search_messages_fts(q, chat_id, allowed_chat_ids, limit, offset)
           method = 'fts'
       else:
           results = await db.get_messages_paginated(chat_id, limit, offset, search=q)
           method = 'ilike'
       return { "results": results, "method": method, "has_more": len(results) == limit }
   ```

6. **Graceful degradation**:
   - If FTS tables/columns don't exist, `search_messages_fts` catches exception and returns empty
   - API always falls back to ILIKE if FTS unavailable
   - Frontend shows "Indexing in progress..." badge when status is 'building'

## Todo

- [ ] Create `src/db/fts.py` with SQLite FTS5 setup functions
- [ ] Add FTS sync triggers (INSERT/DELETE/UPDATE)
- [ ] Implement `rebuild_sqlite_fts()` with batch processing
- [ ] **[RED TEAM]** Implement `sanitize_fts_query()` -- wrap terms in double-quotes
- [ ] Add `init_fts()`, `search_messages_fts()`, `get_fts_status()`, `set_fts_status()` to adapter
- [ ] **[RED TEAM]** `search_messages_fts()` MUST accept and filter by `allowed_chat_ids`
- [ ] Add `GET /api/search` endpoint in main.py with auth dependency
- [ ] **[RED TEAM]** Add rate limit: 10 req/min viewers, 30/min admins
- [ ] **[RED TEAM]** Max query length 200 chars
- [ ] Start FTS worker as asyncio task in app lifespan
- [ ] Add FTS rebuild trigger after backup completion
- [ ] Add ILIKE fallback when FTS not ready
- [ ] Test: SQLite FTS5 search returns ranked results
- [ ] Test: search with special characters (quotes, backslashes, Unicode)
- [ ] Test: cross-chat search only returns results from allowed chats
- [ ] Test: FTS injection attempt with `* NOT term` is sanitized
- [ ] Test: graceful degradation when FTS not built
- [ ] **[RED TEAM]** Use contentless FTS5 (`content=''`) to avoid rowid instability with composite PK
- [ ] **[RED TEAM]** Track `fts_last_indexed_rowid` for crash recovery; resume from checkpoint on restart

## Success Criteria

- FTS index builds automatically on first startup
- Search queries use FTS when index is ready
- Search is significantly faster than ILIKE for large datasets
- Triggers keep index in sync with new messages
- Graceful fallback to ILIKE when FTS unavailable
- API returns search method used (`fts` or `ilike`)

## Risk Assessment

- **[RED TEAM] SQLite rowid + composite PK** -- FTS5 `content_rowid='rowid'` relies on internal rowid which can shift after `VACUUM` or `DELETE` with composite PK tables. This breaks content sync silently.
  - **Mitigation (MANDATORY):** Use **contentless FTS5** (`content=''`) which stores its own copy of text, eliminating rowid dependency. Trade-off: ~2x storage for FTS table, but eliminates silent data corruption. Alternatively, add explicit `docid` column (auto-increment) to messages table and use `content_rowid='docid'`.
  - **Decision:** Use contentless FTS5 (`content=''`) -- safer, no schema change to messages table. Rebuild is a full re-insert, not a `rebuild` command.
- **Large initial index build** -- may take minutes for millions of messages
  - **Mitigation:** Run in background, track progress in app_settings, don't block startup
- **[RED TEAM] FTS worker crash mid-build** -- if app restarts during index build, status stays `building` forever and partial index may be corrupt
  - **Mitigation:** Track `fts_last_indexed_rowid` in app_settings. On restart, if status is `building`, resume from last checkpoint instead of full rebuild. Use batch inserts (1000 rows/batch) and update checkpoint after each batch.
- **[RED TEAM RESOLVED]** FTS query injection: `sanitize_fts_query()` wraps each term in double-quotes. Prevents `* NOT`, `column:`, NEAR operators.
- **[RED TEAM RESOLVED]** PostgreSQL complexity deferred -- SQLite only in this phase

## Security Considerations

- **[RED TEAM]** Search endpoint MUST check authentication (`require_auth` dependency)
- **[RED TEAM]** Search endpoint MUST filter by `allowed_chat_ids` -- cross-chat without this = full authorization bypass on private Telegram messages
- **[RED TEAM]** Rate limit: 10 req/min viewers, 30/min admins. Max query 200 chars.
- FTS query input sanitized via `sanitize_fts_query()` (double-quote wrapping)

## Next Steps

- Phase 8 (search UX) consumes this backend via `/api/search` endpoint
- Phase 10 (AI panel) uses FTS for "search messages" tool
