# Security Adversary Review -- Plan: viewer-advanced-features (2026-03-11)

## Executive Summary

| Metric | Result |
|--------|--------|
| Overall Assessment | Major Issues |
| Security Score | D |
| Maintainability | B |
| Test Coverage | none detected (plan only) |

9 findings. 3 Critical, 4 High, 2 Medium.

---

## Finding 1: Cross-chat search leaks messages from restricted chats (CRITICAL)

- **Severity:** Critical
- **Location:** Phase 9, "Search API endpoint" (step 4); Phase 8, "Architecture"
- **Flaw:** `GET /api/search` pseudocode calls `db.search_messages_fts(q, chat_id, limit, offset)` without passing `user` or `allowed_chat_ids`. Cross-chat mode (no `chat_id`) returns results from ALL chats.
- **Failure scenario:** Viewer restricted to chats [100, 200] searches `GET /api/search?q=secret` and receives messages from chats 500, 600 -- full authorization bypass on private Telegram messages.
- **Evidence:** Phase 9 step 4 pseudocode has no user context. Existing `get_messages` endpoint (main.py:1326-1328) checks `user_chat_ids`; new endpoint does not.
- **Fix:** Pass `user_chat_ids` into `search_messages_fts()`. Add `WHERE chat_id IN (...)` filter. Make this a mandatory implementation step.

## Finding 2: Permalink open redirect via unvalidated `redirect` parameter (CRITICAL)

- **Severity:** Critical
- **Location:** Phase 5, steps 3 and 7
- **Flaw:** Backend redirects to `/?redirect=/chat/{chat_id}?msg={msg_id}`. Frontend uses `window.location.href = redirect || '/'` from URL query string without validation.
- **Failure scenario:** Attacker crafts `https://app.example.com/?redirect=https://evil.com/phish`. User logs in, gets redirected to phishing site. Classic CWE-601.
- **Evidence:** Phase 5 step 7: "After successful login, `window.location.href = redirect || '/'`"
- **Fix:** Validate redirect starts with `/` and contains no `//` or protocol scheme. Use `new URL()` origin check on frontend.

## Finding 3: XSS via regex-based highlight on HTML content (CRITICAL)

- **Severity:** Critical
- **Location:** Phase 8, steps 5-6 and Risk Assessment
- **Flaw:** `highlightText()` applies regex replace on already-linkified HTML. Regex matches inside `<a href="...">` attributes, injecting `<mark>` into tag structure. Rendered via `v-html`.
- **Failure scenario:** Search for a term present in a URL attribute breaks HTML structure. Malformed tags in `v-html` context enable XSS.
- **Evidence:** Phase 8 step 5: `html.replace(new RegExp('(${escaped})', 'gi'), '<mark>$1</mark>')` on full HTML.
- **Fix:** Use DOM TreeWalker to highlight text nodes only, or apply highlight before linkify on escaped plaintext. Specify safe ordering as mandatory, not "test edge cases."

## Finding 4: AI API keys stored in plaintext JSON in database (HIGH)

- **Severity:** High
- **Location:** Phase 10, step 4 and Risk Assessment
- **Flaw:** `await db.set_setting("ai_config", json.dumps(data))` stores OpenAI/Anthropic API keys as plaintext. Encryption deferred to unnamed "production" phase with no plan, no todo, no timeline.
- **Failure scenario:** SQLite file backup or SQL injection leaks `app_settings` table. Attacker extracts API keys, runs up usage costs.
- **Evidence:** Phase 10 Risk Assessment: "currently stored as plain JSON in app_settings ... Production: encrypt with Fernet"
- **Fix:** Encrypt in skeleton phase. 10 lines with `cryptography.fernet`. Add as blocker todo in Phase 10.

## Finding 5: FTS MATCH injection in SQLite (HIGH)

- **Severity:** High
- **Location:** Phase 9, step 2 and Risk Assessment
- **Flaw:** FTS5 MATCH has its own query syntax (AND, OR, NOT, NEAR, *, column prefixes). Plan parameterizes the SQL but not the FTS query language. Mitigation says "quote terms" without specifying how.
- **Failure scenario:** User searches `* NOT secret_term` to invert query, or `column:value` to probe structure. Crafted MATCH syntax causes excessive CPU on large indexes.
- **Evidence:** Phase 9 step 2: `WHERE fts.text MATCH ?` and Risk Assessment: "use fts5_tokenize() or quote terms"
- **Fix:** Wrap each user term in double quotes before MATCH. Add as mandatory sanitization step in adapter method.

## Finding 6: Global timezone writable by any viewer -- privilege escalation (HIGH)

- **Severity:** High
- **Location:** Phase 3, "Architecture" and step 3
- **Flaw:** `PUT /api/settings/timezone` is global (not per-user) but accessible to any authenticated user. Any viewer can change display timezone for all users including admins.
- **Failure scenario:** Malicious viewer sets timezone to UTC+14. All timestamps shift. Admin cannot identify who changed it.
- **Evidence:** Phase 3 step 3: "requires any auth" + "Note: timezone is global (not per-user)"
- **Fix:** Either make per-user (localStorage, no backend), or restrict to admin-only.

## Finding 7: No rate limiting on search endpoints (HIGH)

- **Severity:** High
- **Location:** Phase 9, step 4; Phase 8
- **Flaw:** No rate limiting on `/api/search` or ILIKE fallback. Both are expensive DB operations. FTS rebuild worker competes for same connection pool.
- **Failure scenario:** Viewer sends rapid search requests with long queries. ILIKE full table scans exhaust DB connections. App unresponsive for all users.
- **Evidence:** Phase 9 step 4 has no rate limit. Existing rate limiting only on `/auth/token` (main.py:1119).
- **Fix:** Per-user rate limit (10 req/min viewers, 30/min admins). Max query length 200 chars. DB query timeout.

## Finding 8: Permalink route leaks chat existence (MEDIUM)

- **Severity:** Medium
- **Location:** Phase 5, Security Considerations and step 3
- **Flaw:** Plan states "no information leakage on 403" but implementation step only checks `allowed_chat_ids`, not chat existence. Non-existent chat produces different error than forbidden chat.
- **Failure scenario:** Attacker enumerates chat IDs by response code differences (404 vs 403), discovering which Telegram chats are monitored.
- **Evidence:** Phase 5 Security Considerations vs step 3 implementation -- goal stated but not implemented.
- **Fix:** Return identical 403 for both "not found" and "forbidden." Check access before existence.

## Finding 9: Single-message API enables bulk enumeration (MEDIUM)

- **Severity:** Medium
- **Location:** Phase 5, step 4
- **Flaw:** `GET /api/chats/{chat_id}/messages/{msg_id}` returns full message by sequential integer ID. No rate limiting. Enables scripted enumeration of entire chat history.
- **Failure scenario:** Viewer scripts sequential ID requests (1, 2, 3, ...) to dump entire chat, bypassing pagination rate controls.
- **Evidence:** Phase 5 step 4: returns "message dict with sender info + media" with no abuse prevention.
- **Fix:** Rate limit aggressively (5 req/min). Or replace with paginated `around_msg_id` parameter on existing endpoint.

---

## Unresolved Questions

1. Phase 9: Which database connection pool does the FTS rebuild worker use? If shared with request handlers, large rebuilds will starve HTTP requests.
2. Phase 5: Is `history.replaceState({}, '', '/')` called before or after the chat is loaded? If before, a page refresh during loading loses the permalink.
3. Phase 10: The `POST /api/ai/chat` stub has no auth dependency in the pseudocode. Is this intentional for the skeleton, or an omission?
4. Phase 8: The "Search all chats" toggle is disabled until Phase 9. What prevents a user from manually calling the cross-chat API endpoint before Phase 9 gates it?
