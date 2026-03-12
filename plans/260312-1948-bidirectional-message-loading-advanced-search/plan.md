# Bidirectional Message Loading + Advanced Search

## Status: COMPLETE
## Branch: feat/settings-menu-restructure (continue)

---

## Problem

Permalink navigation (`/chat/{id}?msg={msgId}`) fails to scroll to the target message because:

1. `selectChat()` calls `loadMessages()` which loads the **latest 50** messages
2. Context API then replaces messages with ~50 around the target
3. But `selectChat` already set up observers/auto-refresh for "latest" paradigm
4. If the target message is old (thousands of messages back), it's unreachable — the 50-message context window around it works, but there's **no way to load newer messages** (forward scroll) since pagination only supports `before_date` (backward)

Current message loading is **unidirectional** (newest → oldest). Need **bidirectional** loading from any reference point.

## Solution Overview

| Part | What | Complexity |
|------|------|------------|
| **A** | Bidirectional message loading from reference point | High |
| **B** | Advanced search with date range filters | Low |

---

## Phase 1: Backend — Add forward pagination

**File:** `src/web/main.py` + `src/db/adapter.py`

### Changes

1. **`get_messages_paginated`** in adapter.py — add `after_date`/`after_id` params:
   - When provided, query `WHERE date > after_date OR (date == after_date AND id > after_id)`
   - Order by `date ASC, id ASC` (oldest first for forward loading)
   - Return results in chronological order

2. **`GET /api/chats/{chat_id}/messages`** in main.py — add `after_date`/`after_id` query params:
   - Parse same as `before_date`/`before_id`
   - Pass to adapter
   - Mutual exclusion: `before_*` and `after_*` cannot both be provided

3. **`GET /api/chats/{chat_id}/messages/{msg_id}/context`** — add `has_more_older`/`has_more_newer` to response:
   - Check if messages exist before the oldest returned
   - Check if messages exist after the newest returned
   - Lets frontend know if bidirectional scrolling is needed

### Files Modified
- `src/db/adapter.py` (~20 lines added to `get_messages_paginated`)
- `src/web/main.py` (~15 lines added to `get_messages` endpoint, ~10 lines to context endpoint)

---

## Phase 2: Frontend — Bidirectional loading from reference point

**File:** `src/web/templates/index.html`

### New State
```javascript
const hasMoreNewer = ref(false)        // Are there newer messages to load?
const loadingNewer = ref(false)        // Loading newer messages in progress?
const contextMode = ref(false)         // Loaded from reference point (not from latest)?
```

### Core Logic

**A. Modify `selectChat` to accept optional target message:**
```javascript
// selectChat(chat, { targetMsgId: 732913 })
// When targetMsgId provided: skip loadMessages(), call context API instead
```

**B. New `loadNewerMessages()` function:**
```javascript
// Mirror of loadMessages() but uses after_date/after_id cursor
// Gets the newest currently loaded message, fetches messages AFTER it
// Appends to messages.value
```

**C. Bottom sentinel for forward loading:**
```html
<!-- At bottom of message list (opposite of existing top sentinel) -->
<div v-if="hasMoreNewer && !loadingNewer" ref="loadNewerSentinel"></div>
```

**D. Bottom IntersectionObserver:**
- Mirrors existing `setupMessagesScrollObserver`
- Watches bottom sentinel → triggers `loadNewerMessages()`
- `rootMargin: '200px'` for preloading buffer

**E. Fix permalink handlers (onMounted + performLogin):**
```javascript
// Before:
await selectChat(targetChat)
// context API call
// scrollToMessage()

// After:
await selectChat(targetChat, { targetMsgId: msgId })
// selectChat internally handles context loading + scroll
// No separate context API call needed — it's inside selectChat now
```

**F. Scroll position preservation on prepend/append:**
- When loading older (prepend): preserve scroll position (existing behavior)
- When loading newer (append): no special handling needed (new messages below viewport)

### Files Modified
- `src/web/templates/index.html` (~80 lines added/modified across setup, template, and return block)

---

## Phase 3: Advanced Search UI

**File:** `src/web/templates/index.html`

### New State
```javascript
const showAdvancedSearch = ref(false)
const searchDateFrom = ref('')
const searchDateTo = ref('')
```

### UI Changes

Inside the chat bar search area (`div.px-2.sm:px-4.py-2...`):

1. When search input is focused/has text → show small "Advanced" toggle button
2. When toggled, expand inline: show date-from + date-to fields in the same bar row
3. Fields use `<input type="date">` (native date picker, consistent UX)
4. Dates passed to search as additional query params

```
[ Search...🔍 ] [⚙ Adv]  [Export] [AI]
              ↓ (toggled)
[ Search...🔍 ] [⚙ Adv]  [Export] [AI]
[ From: ______ ] [ To: ______ ]
```

### Backend Support

Add `date_from`/`date_to` params to `GET /api/chats/{chat_id}/messages`:
- `WHERE date >= date_from AND date <= date_to`
- Works with both search text and standalone date filtering

### Files Modified
- `src/web/templates/index.html` (~40 lines for UI + logic)
- `src/web/main.py` (~10 lines for date range params)
- `src/db/adapter.py` (~8 lines for date range filtering)

---

## Execution Order

```
Phase 1 (Backend) → Phase 2 (Bidirectional Loading) → Phase 3 (Advanced Search)
```

Phase 1 must complete first (Phase 2 depends on `after_date` API).
Phase 3 is independent of Phase 2 but benefits from Phase 1's date filtering.

---

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Scroll position jumps when prepending older messages | High | Use `scrollTop` adjustment after DOM update (existing pattern) |
| `flex-col-reverse` complicates bidirectional sentinel placement | Medium | Test carefully; may need to swap sentinel positions |
| Auto-refresh conflicts with context mode | Medium | Disable auto-refresh when `contextMode` is true, re-enable when user scrolls to latest |
| Large message gaps (e.g., 10k messages between context and latest) | Low | Accept — user scrolls forward naturally, loads 50 at a time |

---

## Success Criteria

1. Open permalink → chat loads with target message visible and highlighted
2. Scroll up from target → older messages load progressively
3. Scroll down from target → newer messages load progressively
4. Advanced search: filter messages by date range within a chat
5. Mobile responsive — all new UI fits on small screens
