# Phase 2: Frontend — Bidirectional Loading from Reference Point

## Priority: HIGH
## Status: COMPLETE
## Depends on: Phase 1

---

## Overview

When a permalink targets a specific message, load messages centered around that message (not from latest), then enable lazy loading in both directions — older messages on scroll up, newer messages on scroll down.

---

## Key Insights

- `selectChat()` (line 3726) always calls `loadMessages()` which fetches LATEST 50
- `flex-col-reverse` on the messages container inverts scroll math — "top" sentinel is visually at the top but DOM-wise at the bottom
- `loadMessages()` uses cursor-based pagination with `before_date` — only goes backward
- The existing `messagesScrollObserver` with `loadMoreSentinel` handles upward (older) loading
- Context API (`get_messages_around`) returns ~50 messages centered on target
- Auto-refresh (`startMessageRefresh`, line 3770) polls for newest messages every 3s — conflicts with context mode

---

## Related Code Files

### Modify
- `src/web/templates/index.html`:
  - State declarations (~line 2140)
  - `selectChat()` (line 3726)
  - `loadMessages()` (line 3875)
  - `setupMessagesScrollObserver()` (line 2818)
  - Permalink handlers in `onMounted` (line 3036) and `performLogin` (line 3530)
  - `scrollToMessage()` (line 4627)
  - Template: message list sentinel elements (~line 1100)
  - Return block (~line 5080)

---

## Implementation Steps

### Step 1: Add new state refs

Near line 2147 (where `messageSearchQuery` is declared):

```javascript
const hasMoreNewer = ref(false)
const loadingNewer = ref(false)
const contextMode = ref(false)       // true = loaded from reference point
const pendingTargetMsgId = ref(null) // set before selectChat, cleared after scroll
```

### Step 2: Modify `selectChat` to accept target message

```javascript
const selectChat = async (chat, opts = {}) => {
    // ... existing setup (clear state, set selectedChat) ...

    if (opts.targetMsgId) {
        // Context mode: load around target instead of latest
        contextMode.value = true
        pendingTargetMsgId.value = opts.targetMsgId
        try {
            const ctxRes = await fetch(
                `/api/chats/${chat.id}/messages/${opts.targetMsgId}/context`,
                { credentials: 'include' }
            )
            if (ctxRes.ok) {
                const ctxData = await ctxRes.json()
                messages.value = ctxData.messages || []
                hasMore.value = ctxData.has_more_older !== false
                hasMoreNewer.value = ctxData.has_more_newer !== false
            }
        } catch (e) { console.error('Context load failed:', e) }

        loading.value = false
        await nextTick()
        scrollToMessage(opts.targetMsgId)
        setupMessagesScrollObserver()
        setupNewerMessagesObserver()
        // Do NOT start auto-refresh in context mode
        return
    }

    // Normal flow: load latest messages
    contextMode.value = false
    hasMoreNewer.value = false
    await loadMessages()
    // ... rest of existing selectChat ...
}
```

### Step 3: Add `loadNewerMessages()` function

After `loadMessages()` (~line 3954):

```javascript
const loadNewerMessages = async () => {
    if (loadingNewer.value || !hasMoreNewer.value || !selectedChat.value) return

    loadingNewer.value = true
    try {
        // Find the newest message currently loaded
        const newestMsg = messages.value.reduce((newest, msg) => {
            return new Date(msg.date) > new Date(newest.date) ? msg : newest
        }, messages.value[0])

        if (!newestMsg) return

        const url = `/api/chats/${selectedChat.value.id}/messages`
            + `?after_date=${encodeURIComponent(newestMsg.date)}`
            + `&after_id=${newestMsg.id}&limit=50`

        const res = await fetch(url, { credentials: 'include' })
        const newMessages = await res.json()

        if (newMessages.length < 50) {
            hasMoreNewer.value = false
        }

        // Merge new (newer) messages into the list
        const existingById = new Map(messages.value.map(m => [m.id, m]))
        for (const msg of newMessages) {
            existingById.set(msg.id, msg)
        }
        messages.value = Array.from(existingById.values())
    } catch (e) {
        console.error('Failed to load newer messages', e)
    } finally {
        loadingNewer.value = false
        await nextTick()
        if (newerMessagesObserver && loadNewerSentinel.value) {
            newerMessagesObserver.observe(loadNewerSentinel.value)
        }
    }
}
```

### Step 4: Add bottom sentinel + observer

**Template** — add sentinel inside the messages container (mirror of existing `loadMoreSentinel`):

```html
<!-- Newer messages sentinel (bottom of list in context mode) -->
<div v-if="contextMode && hasMoreNewer"
     ref="loadNewerSentinel"
     class="h-1 w-full"></div>
```

Note: Because `flex-col-reverse`, the "bottom" sentinel appears at the visual top or bottom depending on sort direction. Need to test placement.

**Observer setup:**

```javascript
let newerMessagesObserver = null
const loadNewerSentinel = ref(null)

const setupNewerMessagesObserver = () => {
    if (newerMessagesObserver) newerMessagesObserver.disconnect()

    newerMessagesObserver = new IntersectionObserver(
        (entries) => {
            if (entries[0].isIntersecting && hasMoreNewer.value && !loadingNewer.value) {
                loadNewerMessages()
            }
        },
        { root: messagesContainer.value, rootMargin: '200px' }
    )

    nextTick(() => {
        if (loadNewerSentinel.value) {
            newerMessagesObserver.observe(loadNewerSentinel.value)
        }
    })
}
```

### Step 5: Simplify permalink handlers

**In `onMounted` (line 3036):**

```javascript
// Replace the entire context API + scrollToMessage block with:
if (targetChat) {
    const msgId = msgIdParam ? parseInt(msgIdParam) : null
    await selectChat(targetChat, msgId ? { targetMsgId: msgId } : {})
}
```

**In `performLogin` (line 3530):** Same simplification.

### Step 6: Handle transition back to normal mode

When user scrolls to the very bottom in context mode and no more newer messages:
- Set `contextMode.value = false`
- Start auto-refresh to catch live messages
- This seamlessly transitions from "viewing history" to "live chat"

### Step 7: Expose new refs in return block

Add to the return block (~line 5080):
```javascript
hasMoreNewer, loadingNewer, contextMode,
loadNewerSentinel, loadNewerMessages,
```

---

## Todo List

- [x] Add `hasMoreNewer`, `loadingNewer`, `contextMode`, `pendingTargetMsgId` refs
- [x] Add `loadNewerSentinel` template ref
- [x] Modify `selectChat` to accept `opts.targetMsgId`
- [x] Implement `loadNewerMessages()` with `after_date` cursor
- [x] Add bottom sentinel element in template
- [x] Implement `setupNewerMessagesObserver()`
- [x] Simplify permalink handlers in `onMounted` and `performLogin`
- [x] Handle context-to-normal mode transition
- [x] Add new refs to return block
- [x] Test with `flex-col-reverse` — verify sentinel placement

---

## Success Criteria

1. Permalink opens chat with target message visible and highlighted
2. Scroll up → older messages load (existing behavior, now works from midpoint)
3. Scroll down → newer messages load (new behavior)
4. Reaching the latest message transitions to normal auto-refresh mode
5. Normal chat selection (non-permalink) works as before
