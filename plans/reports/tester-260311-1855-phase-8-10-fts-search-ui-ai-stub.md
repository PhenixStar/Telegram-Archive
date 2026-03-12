# Test Report: Phase 8-10 (FTS Search, Enhanced Search UI, AI Stub)

**Date:** 2026-03-11
**Tester:** tester-ae7f6420
**Status:** PASS (with minor findings)

---

## 1. Python Syntax Validation

| File | Result |
|------|--------|
| `src/db/fts.py` | PASS |
| `src/db/adapter.py` | PASS |
| `src/web/main.py` | PASS |
| `src/db/models.py` | PASS |
| `src/db/base.py` | PASS |
| `src/db/__init__.py` | PASS |
| `src/config.py` | PASS |

All 7 Python files compile without errors.

## 2. Existing Test Suite Regression

- **33 tests passed** (test_config.py, test_auth.py)
- **14 tests failed** due to pre-existing `NameError` in `models.py` (forward reference `Mapped[list[Message]]` before `Message` class is defined). This is NOT caused by Phase 8-10 changes.
- No regressions introduced by the new code.

## 3. FTS Query Sanitizer (`sanitize_fts_query`)

**15/15 tests passed:**

| Test Case | Input | Output | Result |
|-----------|-------|--------|--------|
| Normal query | `hello world` | `"hello" "world"` | PASS |
| Empty input | `` | `` | PASS |
| Whitespace only | `   ` | `` | PASS |
| NOT injection | `* NOT important` | `"*" "NOT" "important"` | PASS |
| Column injection | `chat_id:12345` | `"chat_id:12345"` | PASS |
| Existing quotes | `"hello" "world"` | `"hello" "world"` | PASS |
| NEAR injection | `NEAR(hello, world)` | `"NEAR(hello," "world)"` | PASS |
| Asterisk wildcard | `test*` | `"test*"` | PASS |
| Single term | `hello` | `"hello"` | PASS |
| Embedded quotes | `he"llo wor"ld` | `"hello" "world"` | PASS |
| OR operator | `hello OR world` | `"hello" "OR" "world"` | PASS |
| AND operator | `hello AND world` | `"hello" "AND" "world"` | PASS |
| Mixed whitespace | `  hello \t world  ` | `"hello" "world"` | PASS |
| Hyphenated term | `well-known term` | `"well-known" "term"` | PASS |
| Only quotes | `""` | `` | PASS |

All FTS5 operators (NOT, OR, AND, NEAR, column:, asterisk) are neutralized by double-quoting each token.

## 4. FTS Function Signatures

| Function | Params | Default | Status |
|----------|--------|---------|--------|
| `setup_sqlite_fts` | `session` | - | PASS |
| `rebuild_sqlite_fts` | `session, set_status_cb, batch_size` | `batch_size=1000` | PASS |
| `sanitize_fts_query` | `raw_query` | - | PASS |

## 5. Adapter FTS Methods

| Method | SQLite Guard | Error Handling | Status |
|--------|-------------|----------------|--------|
| `init_fts()` | `_is_sqlite` check, no-op on PG | N/A | PASS |
| `rebuild_fts_index()` | `_is_sqlite` check, returns 0 on PG | Via session | PASS |
| `get_fts_status()` | Delegates to `get_setting` | N/A | PASS |
| `set_fts_status()` | Delegates to `set_setting` | N/A | PASS |
| `search_messages_fts()` | Via inner import | try/except returns `[]` | PASS |
| `count_fts_matches()` | Via inner import | try/except returns `0` | PASS |
| `insert_fts_entry()` | `_is_sqlite` check + empty text guard | try/except with debug log | PASS |

## 6. Search Endpoint (`GET /api/search`)

| Check | Status | Notes |
|-------|--------|-------|
| Auth required | PASS | `Depends(require_auth)` |
| 200-char limit | PASS | HTTP 400 if exceeded |
| Empty query returns early | PASS | Returns `{"results":[], "total":0}` |
| Limit clamped [1,200] | PASS | `min(max(limit, 1), 200)` |
| Offset non-negative | PASS | `max(offset, 0)` |
| FTS path uses access control | PASS | `allowed_chat_ids` passed to SQL WHERE |
| ILIKE fallback checks chat access | PASS | Explicit 403 if chat not in allowed set |
| ILIKE fallback requires `chat_id` | PASS | Returns empty if `chat_id` is None |

## 7. FTS Status Endpoint (`GET /api/fts/status`)

| Check | Status |
|-------|--------|
| Auth required | PASS |
| Returns JSON `{"status": ...}` | PASS |
| Handles null status | PASS (returns `"not_initialized"`) |

## 8. Background FTS Worker

| Check | Status | Notes |
|-------|--------|-------|
| Skips rebuild if status="ready" | PASS | Early return |
| Sets status "building" before work | PASS | |
| Sets status "ready" after completion | PASS | |
| Sets status "error" on failure | PASS | Double try/except |
| CancelledError re-raised | PASS | Correct asyncio pattern |
| Task tracked in `_fts_task` | PASS | Cancelled in lifespan shutdown |

## 9. Frontend Search UI (Phase 9)

### JavaScript Logic

| Component | Status | Notes |
|-----------|--------|-------|
| `handleSearchInput` debounce (300ms) | PASS | `clearTimeout` + `setTimeout` |
| AbortController for in-flight requests | PASS | Previous request aborted |
| `applySearchHighlights` TreeWalker | PASS | Correct DOM text node traversal |
| Regex escape in highlight | PASS | Standard `[.*+?^${}()|[\]\\]` escape |
| `regex.lastIndex = 0` after `test()` | PASS | Prevents skipped first match |
| `clearSearchHighlights` replaces marks | PASS | `parent.normalize()` merges text nodes |
| `navigateSearchMatch` wraps with modulo | PASS | `(idx + dir + len) % len` |
| `clearMsgSearch` reloads original messages | PASS | Only if `hadQuery` is true |
| `clearSearch` alias to `clearMsgSearch` | PASS | Backward compat with keyboard handler |

### HTML Template

| Component | Status |
|-----------|--------|
| Search bar placement (sticky top) | PASS |
| `v-show="searchBarVisible"` toggle | PASS |
| Match counter display | PASS |
| Prev/Next buttons (v-if matchCount > 0) | PASS |
| Close button calls `clearMsgSearch` | PASS |
| Input ref for auto-focus | PASS |

### CSS

| Rule | Status | Notes |
|------|--------|-------|
| `mark { background: #ffeb3b }` | PASS | Yellow highlight, visible on both themes |
| `mark.current { background: #ff9800 }` | PASS | Orange for active match |
| `[data-theme^="light"] mark` | PASS | Explicit light theme override |
| No dark theme conflict | PASS | Default yellow works on dark backgrounds |

### Keyboard Integration

| Key | Behavior | Status |
|-----|----------|--------|
| Ctrl+F | Toggle search bar, auto-focus | PASS |
| Escape (in input) | `clearMsgSearch` via `@keydown.escape.prevent` | PASS |
| Escape (global) | Cascade: lightbox > context > date > settings > AI > search | PASS |
| Enter (in input) | Navigate to next match | PASS |

## 10. AI Assistant Stub (Phase 10)

| Component | Status | Notes |
|-----------|--------|-------|
| Brain icon button | PASS | `fa-brain` icon, correct styling |
| Toggle `showAiComingSoon` ref | PASS | Click toggles tooltip |
| Tooltip positioning | PASS | `absolute right-0 top-full` |
| "Coming soon." text | PASS | |
| Escape dismissal | PASS | In cascade before search bar |
| `showAiComingSoon` in return block | PASS | Properly exposed |

## 11. Findings (Minor)

### F1: Unused `set_status_cb` Parameter (LOW)
**File:** `src/db/fts.py:55`
**Issue:** `rebuild_sqlite_fts` accepts `set_status_cb` but never calls it. The function writes checkpoints via raw SQL (`INSERT OR REPLACE INTO app_settings`) directly on the passed session instead.
**Impact:** Dead parameter. No runtime bug.
**Recommendation:** Either remove the parameter or use it for status updates.

### F2: Empty `allowed_chat_ids` Set (PRE-EXISTING, LOW)
**File:** `src/db/adapter.py:2308-2313`, `src/web/main.py:802`
**Issue:** If `get_user_chat_ids()` returns an empty set (intersection of user's chats and master filter is empty), the FTS queries would generate `IN ()` which is invalid SQL.
**Impact:** Would raise an exception caught by the try/except, returning `[]` or `0`. Not a crash but returns empty results silently.
**Note:** Pre-existing issue in access control layer, not introduced by Phase 8-10.

### F3: ILIKE Fallback Total Count Approximation (INFO)
**File:** `src/web/main.py:1502`
**Issue:** `total = len(results)` gives at most `limit` count, not the true total. The `has_more` flag compensates.
**Impact:** Acceptable for a fallback path. FTS path uses proper `count_fts_matches()`.

## 12. Summary

| Phase | Verdict | Critical Issues |
|-------|---------|----------------|
| Phase 8: FTS5 Backend | PASS | None |
| Phase 9: Enhanced Search UI | PASS | None |
| Phase 10: AI Assistant Stub | PASS | None |
| Regression | PASS | No new failures |

**Overall: PASS** -- All Phase 8-10 code compiles, sanitizer handles injection attempts correctly, endpoints enforce auth and access control, frontend search UI is properly integrated with keyboard handlers, and the AI stub is minimal and correctly wired. No critical or high-severity issues found.
