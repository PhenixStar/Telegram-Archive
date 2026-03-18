# Chat Search Backend Research

## 1. Search API Endpoints

### Global search: `GET /api/search` (routes_chat.py:241-284)

Parameters:
- `q` (str, required) -- search query, max 200 chars
- `chat_id` (int|None, optional) -- scope to single chat
- `limit` (int, default 50, clamped 1-200)
- `offset` (int, default 0)
- Auth: `require_auth`

Response shape:
```json
{"results": [...], "total": int, "method": "fts"|"ilike"|"none", "has_more": bool}
```

### Per-chat search: `GET /api/chats/{chat_id}/messages` (routes_chat.py:144-200)

The `search` query param triggers ILIKE filtering within `get_messages_paginated`. Returns raw message list, **no total count**, no `has_more`.

### Semantic search: `GET /api/semantic/search` (routes_ai.py:624-648)

Parameters: `q`, `chat_id` (required), `limit`. Returns `{"results": [...], "total": len(results), "method": "semantic"}`. Total is just `len(results)`, not a true count of all matches.

---

## 2. Does the API Return Total Count?

**YES -- but only for FTS path in `/api/search`.**

When `fts_status == "ready"`:
- Results: `search_messages_fts(q, chat_id, allowed_chat_ids, limit, offset)`
- Count: `count_fts_matches(q, chat_id, allowed_chat_ids)` -- **separate COUNT query**
- `has_more`: `len(results) == limit` (heuristic, not derived from total)

When FTS not ready (ILIKE fallback):
- If `chat_id is None`: returns empty results immediately (no cross-chat ILIKE)
- If `chat_id` provided: calls `get_messages_paginated(chat_id, limit, offset, search=q)`
- **Total is set to `len(results)`** -- only counts the current page, NOT true total
- `has_more` uses same heuristic

---

## 3. FTS Implementation (adapter_search.py + fts.py)

### FTS5 Virtual Table (SQLite only)
- Table: `messages_fts` with columns `text`, `chat_id` (UNINDEXED), `msg_id` (UNINDEXED)
- Tokenizer: `unicode61 remove_diacritics 2`
- Indexes: message text + OCR text + AI comments combined

### `count_fts_matches()` (adapter_search.py:155-176)
```sql
SELECT count(*) FROM messages_fts fts WHERE {where_clause}
```
- Reuses same `_build_fts_where()` helper as search query
- Supports `chat_id` filter and `allowed_chat_ids` access control
- Returns 0 on error (silently catches exceptions)

### `search_messages_fts()` (adapter_search.py:90-143)
- Uses `snippet()` for highlight markers (`<b>...</b>`)
- Joins `messages` + `users` tables
- Ordered by FTS5 `rank`
- LIMIT/OFFSET pagination

### Query sanitization (fts.py:21-35)
- Each term wrapped in double quotes to prevent FTS5 injection
- Empty/whitespace returns empty string

---

## 4. ILIKE Fallback Behavior

**Does NOT return true counts.**

The ILIKE path in `/api/search` (line 275-277):
```python
results = await deps.db.get_messages_paginated(chat_id, limit, offset, search=q)
total = len(results)  # <-- only current page count!
method = "ilike"
```

Inside `get_messages_paginated` (adapter_messages.py:169-177):
- Numeric queries: `func.replace()` normalization + `contains()`
- Text queries: `Message.text.ilike(f"%{escaped}%", escape="\\")`
- No separate count query exists for this path

Cross-chat ILIKE is **explicitly blocked** (returns empty) because it would be too slow.

---

## 5. Semantic Search Endpoint

`GET /api/semantic/search` (routes_ai.py:624-648):
- Requires `chat_id` (single-chat only)
- Embeds query via configured embedding API, then cosine similarity in Python
- Returns top-N results, `total = len(results)` (not a true count)
- No pagination (limit caps results, no offset)
- Requires embeddings to be pre-generated via `POST /api/semantic/embed`

---

## Summary: Count Support Matrix

| Path | True Total Count? | Notes |
|------|-------------------|-------|
| FTS (`/api/search`, status=ready) | YES | Separate `count_fts_matches()` query |
| ILIKE (`/api/search`, single chat) | NO | `total = len(results)` (page size) |
| ILIKE (cross-chat) | N/A | Returns empty, blocked |
| Per-chat messages (`/api/chats/{id}/messages`) | NO | No count returned at all |
| Semantic (`/api/semantic/search`) | NO | `total = len(results)` |

## Key Finding

The backend **already returns match counts for FTS search** via `count_fts_matches()`. The ILIKE fallback does NOT have a count query. If accurate counts are needed for the ILIKE path, a new `COUNT(*)` query with the same ILIKE filter would need to be added. However, since ILIKE is only used as a degraded fallback (FTS not built yet), this may not be worth the effort -- the `has_more` heuristic (`len(results) == limit`) is sufficient for pagination UX.

## Relevant Files
- `/home/phenix/projects/tele-private/repo/dev/src/web/routes_chat.py` -- search endpoint (lines 241-284)
- `/home/phenix/projects/tele-private/repo/dev/src/db/adapter_search.py` -- FTS search + count (lines 90-176)
- `/home/phenix/projects/tele-private/repo/dev/src/db/fts.py` -- FTS5 setup, query sanitization
- `/home/phenix/projects/tele-private/repo/dev/src/db/adapter_messages.py` -- ILIKE search in `get_messages_paginated` (lines 169-177)
- `/home/phenix/projects/tele-private/repo/dev/src/web/routes_ai.py` -- semantic search endpoint (lines 624-648)
