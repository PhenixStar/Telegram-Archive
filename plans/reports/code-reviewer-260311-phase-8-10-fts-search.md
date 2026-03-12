# Code Review -- Phase 8-10 FTS Search Implementation (2026-03-11)

## Executive Summary

| Metric | Result |
|--------|--------|
| Overall Assessment | Needs Work |
| Security Score     | C |
| Maintainability    | B |
| Test Coverage      | none detected |

One critical authorization bypass in the ILIKE fallback path and one dead-code parameter in the core FTS module. The FTS5 setup, query sanitizer, and frontend highlighting are well-implemented. The security issue must be fixed before merge.

---

## CRITICAL Issues

| File:Line | Issue | Why it is critical | Suggested Fix |
|-----------|-------|-------------------|---------------|
| `src/web/main.py:1496` | **ILIKE fallback skips `allowed_chat_ids` check** -- when FTS is not ready and a `chat_id` is provided, `get_messages_paginated(chat_id, ...)` is called without verifying the user has access to that chat. Any authenticated user can read messages from any chat by passing `chat_id` to `/api/search` while the FTS index is building or in error state. | Authorization bypass -- cross-chat data leak for multi-user deployments. A viewer account restricted to chat A can read chat B by supplying `?chat_id=<B>&q=keyword` before FTS index finishes. | Add access check before the ILIKE fallback: `if allowed_chat_ids is not None and chat_id not in allowed_chat_ids: raise HTTPException(status_code=403, detail="Access denied")` |

---

## MAJOR Issues

| File:Line | Issue | Why it matters | Suggested Fix |
|-----------|-------|---------------|---------------|
| `src/web/main.py:1469-1470` | **`limit`/`offset` on `/api/search` have no bounds validation** -- declared as bare `int` params (`limit: int = 50, offset: int = 0`). A caller can set `limit=999999` to dump the entire FTS index in one query, or use negative `offset`. | Performance / DoS risk. The `/api/chats` endpoint correctly uses `Query(50, ge=1, le=1000)`. This endpoint should match. | Change to `limit: int = Query(50, ge=1, le=200)` and `offset: int = Query(0, ge=0)`. |
| `src/db/fts.py:55` | **`set_status_cb` parameter is declared but never called** -- `rebuild_sqlite_fts` accepts a `set_status_cb` callback but writes directly to `app_settings` via raw SQL (line 104-110) instead. Dead parameter misleads callers into thinking status updates are delegated. | Maintainability confusion. The adapter passes `self.set_setting` (line 2258) for nothing. The raw SQL also bypasses SQLAlchemy ORM and the `@retry_on_locked` decorator on `set_setting`. | Either (a) use the callback: replace lines 104-110 with `await set_status_cb("fts_last_indexed_rowid", str(last_rowid))`, or (b) remove the parameter if direct SQL is preferred for performance. |
| `src/db/adapter.py:2296-2314` and `src/db/adapter.py:2366-2381` | **Duplicated WHERE-clause building logic** between `search_messages_fts` and `count_fts_matches`. Identical code for MATCH clause, `chat_id` filter, and `allowed_chat_ids` IN-list construction. | DRY violation. If access control logic changes, both must be updated in lockstep; forgetting one causes a security regression. | Extract a `_build_fts_where(query, chat_id, allowed_chat_ids)` helper that returns `(where_clause, params)`. |

---

## MINOR Suggestions

- `src/db/fts.py:106-110` -- Uses `INSERT OR REPLACE INTO app_settings` which assumes `key` is the PK. Works, but the rest of the codebase uses the SQLAlchemy upsert in `set_setting()`. Consistency would be better.
- `src/web/main.py:1497` -- `total = len(results)` in the ILIKE fallback is inaccurate for pagination; it reflects the page size, not the true total. Consider calling `db.get_message_count(chat_id, search=q)` if available, or documenting that `total` is approximate in ILIKE mode.
- `src/web/templates/index.html:3954` -- The in-chat search bar fetches from `/api/chats/.../messages?search=...&limit=200` (hardcoded `limit=200`). For very active chats, consider documenting why 200 is the cap or making it configurable.
- `src/web/templates/index.html:3959` -- `messages.value = data` replaces all messages with search results. This works because `clearMsgSearch` reloads the original messages, but there is no loading indicator while the search fetch is in progress (user sees stale results until the response arrives). Consider setting a `searchLoading` flag.
- `src/web/main.py:1504` -- `has_more` is computed as `len(results) == limit`, which can false-positive when the result count exactly equals the limit. Minor but worth a comment.
- `src/db/adapter.py:2320` -- The `snippet()` function generates `<b>...</b>` HTML tags. If this snippet is ever rendered with `v-html` in the frontend, it would need sanitization. Currently unused in the template; add a comment noting this for future developers.

---

## Positive Highlights

- Well-designed FTS5 contentless table (`content=''`) avoids rowid instability with composite PK. The `chat_id UNINDEXED, msg_id UNINDEXED` columns are correctly marked as non-searchable metadata.
- `sanitize_fts_query()` properly strips quotes and wraps each term in double-quotes, neutralizing FTS5 operators (`NOT`, `*`, `NEAR`, column filters). Empty/whitespace input returns empty string, short-circuiting downstream queries.
- Frontend `applySearchHighlights` uses `document.createTreeWalker(container, NodeFilter.SHOW_TEXT)` to walk text nodes only -- no HTML attribute manipulation, no XSS vector. `mark.textContent = match[1]` is safe.
- `AbortController` correctly cancels in-flight search requests on new input and on search close.
- `clearMsgSearch` properly restores original message list by resetting state and calling `loadMessages()`.
- FTS worker runs as `asyncio.create_task()` in the lifespan, does not block startup, and is properly cancelled during shutdown (line 514-520).
- FTS worker has proper exception handling: catches `CancelledError` (re-raises), catches general exceptions, and attempts to set status to "error" with nested try/except.
- `require_auth` dependency is correctly applied to both `/api/search` and `/api/fts/status`.
- Batch processing with rowid-based cursor (`WHERE rowid > :last ORDER BY rowid LIMIT :batch`) is efficient and crash-recoverable via the `fts_last_indexed_rowid` checkpoint.

---

## Action Checklist

- [ ] **CRITICAL**: Add `allowed_chat_ids` check before ILIKE fallback in `/api/search` (line 1496)
- [ ] Add `Query()` bounds to `limit` and `offset` on `/api/search` endpoint
- [ ] Remove or use `set_status_cb` parameter in `rebuild_sqlite_fts`
- [ ] Extract shared WHERE-clause builder from `search_messages_fts` / `count_fts_matches`
- [ ] Add unit tests for `sanitize_fts_query` edge cases (empty string, quotes, FTS5 operators)
- [ ] Add integration test verifying `/api/search` respects `allowed_chat_ids` in both FTS and ILIKE paths

---

## Unresolved Questions

1. Is the `fts_last_indexed_rowid` checkpoint used anywhere for incremental indexing (e.g., indexing only new messages since last rebuild)? Currently, `rebuild_sqlite_fts` always starts with `DELETE FROM messages_fts`, making the checkpoint useful only for progress tracking, not for resume-after-crash. Was incremental indexing intended?
2. For PostgreSQL deployments, is there a plan to use `tsvector`-based search? Currently FTS is SQLite-only and PostgreSQL users get no cross-chat search at all (returns empty results when `chat_id` is None).
