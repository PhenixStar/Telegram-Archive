# Code Review: Frontend 3-Commit Range (de463b0..155b8b1)

**Commits:** `3ae4d95`, `e93734c`, `155b8b1`
**File:** `src/web/templates/index.html` (8518 LOC, +954/-387)
**Date:** 2026-03-21

## Severity Summary

| Severity | Count | Categories |
|----------|-------|-----------|
| Critical | 1 | Performance (regex per render) |
| High | 5 | Event leaks, state bugs, accessibility, dead code |
| Medium | 6 | Theme hardcoding, debug logs, UX quirks |
| Low | 3 | Style nits, minor redundancy |

---

## Critical Issues

### C1. `getMessageHighlight()` runs N regex tests per message per render cycle
**Lines:** ~3742-3755 (in diff: highlightPresets + getMessageHighlight)

Every call to `getMessageHighlight(msg)` iterates active presets and runs up to 5 regex patterns (the `transactions` preset alone has 5 regexes). This function is called from the `:style` binding on every message bubble in the `v-for`, meaning it executes **O(messages x patterns)** on every re-render -- including scroll-triggered reactivity.

With 100 visible messages and 5 active patterns = 500+ regex tests per render frame.

**Fix:** Memoize via `computed` Map keyed by `msg.id + activeHighlights hash`:
```js
const highlightCache = computed(() => {
    const map = new Map()
    if (!activeHighlights.value.length) return map
    for (const msg of sortedMessages.value) {
        const color = _computeHighlight(msg)
        if (color) map.set(msg.id, color)
    }
    return map
})
const getMessageHighlight = (msg) => highlightCache.value.get(msg.id) || null
```
Or at minimum, pre-compile regex once (they're already literal, so V8 caches them, but the iteration overhead remains).

---

## High Priority

### H1. `window.addEventListener('resize')` and `window.addEventListener('popstate')` never cleaned up
**Lines:** 6831-6851

Both listeners are added in `onMounted` but never removed in `onBeforeUnmount` / `onUnmounted`. If the Vue app is ever remounted (hot reload, SPA transitions), these stack.

```js
// Fix: add cleanup
onBeforeUnmount(() => {
    window.removeEventListener('resize', onWinResize)
    window.removeEventListener('popstate', onPopState)
})
```

### H2. `aiMessages` ref reassigned to a reactive array from `aiMessagesByChatId` -- shared reference semantics
**Lines:** ~3766, 7013

In `hydratePanel('ai')`, the code does:
```js
aiMessages.value = aiMessagesByChatId[cid]
```
`aiMessages` is a `ref()`, and its `.value` now points to a `Vue.reactive([])` array. Any code that does `aiMessages.value = []` (e.g., logout at line ~5978) only replaces the ref's pointer without clearing the per-chat array in `aiMessagesByChatId`. This means:
- Logout clears visible AI messages correctly (ref reassigned)
- But `aiMessagesByChatId[cid]` still holds old messages
- Re-login + same chat = stale AI thread appears

The `Object.keys(aiMessagesByChatId).forEach(k => delete ...)` in `performLogout` handles this -- but `showAllChats()` or other resets do not.

**Risk:** Moderate -- only manifests if user logs out and back in to same chat without page reload, but the reactive pointer sharing is fragile.

### H3. `showScrollToTop` / `showScrollToBottom` mutual exclusion with `v-if`/`v-else-if` loses both buttons
**Lines:** ~2324-2341 (template), 6559-6689 (JS)

The template uses:
```html
<button v-if="showScrollToTop && !showScrollToBottom" ...>  <!-- go to oldest -->
<button v-else-if="showScrollToBottom" ...>                  <!-- go to latest -->
```

When both `showScrollToTop` and `showScrollToBottom` are true simultaneously (user scrolled >800px up for 2s AND >300px from bottom), the first condition `showScrollToTop && !showScrollToBottom` is false, so it falls through to show "go to latest". The "go to oldest" button can NEVER appear while "go to latest" is showing, but the 2-second timer for `showScrollToTop` runs independently.

**Result:** `showScrollToTop` button only shows in the narrow window where user has scrolled exactly 300-800px and waited 2s -- practically never visible.

**Fix:** Rethink the FSM. Likely the intent is: show "go to latest" when scrolled up a bit, show "go to oldest" only when scrolled very far up. Use a single reactive with three states (hidden/up/down) instead of two booleans.

### H4. `_scrollUpTimer` never cleared on chat switch or component teardown
**Lines:** ~4508-4509 (`let _scrollUpTimer = null`)

When switching chats, `_scrollUpTimer` from the previous chat's scroll handler can fire and set `showScrollToTop = true` for the new chat incorrectly. No cleanup in `selectChat`.

```js
// In selectChat, add:
clearTimeout(_scrollUpTimer); _scrollUpTimer = null; showScrollToTop.value = false
```

### H5. Search match navigation direction labels are swapped
**Lines:** ~1534-1541 (template diff)

```html
<button ... @click="navigateSearchMatch(1)" title="Previous (older)">
    <i class="fas fa-chevron-up"></i>
</button>
<button ... @click="navigateSearchMatch(-1)" title="Next (newer)">
    <i class="fas fa-chevron-down"></i>
</button>
```

In the old code, `navigateSearchMatch(-1)` was "Previous" and `(1)` was "Next". The new code swaps the direction but keeps chevron-up = +1, chevron-down = -1. In a `flex-col-reverse` container, "up" arrow with +1 actually moves to higher indexes (which are visually lower/older due to reverse). The `title` attributes say "Previous (older)" for up and "Next (newer)" for down -- this is correct for `flex-col-reverse` UI but the `dir` value flip may confuse the index math. Needs manual testing.

---

## Medium Priority

### M1. Folder dropdown uses hardcoded `bg-gray-800`, `border-gray-600` -- only works because of CSS override layer
**Lines:** 1143, 2353

The new folder dropdown and dock export menu use `bg-gray-800 border border-gray-600` directly. These happen to be caught by the override rules at lines 536-544 (`bg-gray-800 → var(--tg-bg)`, `border-gray-600 → var(--tg-border)`). This works but is fragile -- any Tailwind purge or class renaming breaks it.

**Recommendation:** Use inline `style="background: var(--tg-bg); border-color: var(--tg-border);"` like the rest of the new code does.

### M2. Debug console.log statements left in `navigateSearchMatch`
**Lines:** 7542, 7548

```js
console.log('[nav] dir=', dir, 'marks=', marks.length, 'idx=', searchMatchIndex.value)
console.log('[nav] scrolling to mark', searchMatchIndex.value, mark.textContent?.substring(0, 20))
```

These fire on every search navigation click. Should be removed or guarded behind a debug flag.

### M3. `sidebarFocusCompact` computed triggers full sidebar re-layout on any `windowWidth` change
**Line:** 3768-3770

```js
const sidebarFocusCompact = computed(() =>
    windowWidth.value >= 768 && !!selectedChat.value && rightDockOpen.value
)
```

`windowWidth` updates on every window resize event (no debounce). Each change triggers computed recalculation, which triggers `:class` bindings across the entire sidebar template tree. The sidebar has 20+ conditional class bindings dependent on `sidebarFocusCompact`.

**Fix:** Debounce `windowWidth` updates or use a coarser `isMdPlus` boolean computed that only changes at the 768px threshold.

### M4. Compact sidebar mode: chat items get `role="button" tabindex="0"` only in compact, creating inconsistent keyboard nav
**Lines:** ~1383-1384

```html
role="button" :tabindex="sidebarFocusCompact ? '0' : undefined"
```

In normal mode, chat items have no `role` or `tabindex`, so keyboard users can't tab to them. In compact mode, they suddenly become tabbable. This is inconsistent -- either all chat items should be keyboard-navigable or none.

### M5. `mobileHistoryLayers` is a plain `let` variable, not reactive -- Vue template can't depend on it
**Lines:** ~4852

This is fine as long as no template binding reads it (confirmed: only JS reads it). Documenting for awareness -- if anyone adds `v-if="mobileHistoryLayers > 0"` later, it won't be reactive.

### M6. Compact mode `v-show="!sidebarFocusCompact"` vs `v-if` for chat detail blocks
**Lines:** ~1333, 1413-1416

Chat list uses `v-show="!sidebarFocusCompact"` for the text portion (good for perf -- avoids DOM churn). But the archived chats row uses `v-show` for some parts and leaves the 12x12 avatar visible always. This means compact mode still renders full DOM for all visible chat list items, just hidden. For 50+ chats this is fine; for 500+ it may matter.

---

## Low Priority

### L1. `box-shadow: 0 4px 12px 0 rgba(0, 0, 0, 0.15)` on message bubbles
**Line:** 428 (CSS)

The new shadow uses hardcoded `rgba(0,0,0,0.15)`. On light themes this is barely visible; on dark themes it's appropriate. Not broken, but a CSS variable would be more consistent.

### L2. `computeDefaultSidebarWidth()` function always returns 300
**Lines:** ~3702-3704

```js
const computeDefaultSidebarWidth = () => { return 300 }
```

This could just be `const DEFAULT_SIDEBAR_WIDTH = 300`. The function wrapper adds no value.

### L3. Redundant `transition-all duration-200` on every chat item in compact mode
**Line:** ~1385

`transition-all` is expensive -- it transitions every CSS property change including layout. Should be `transition-colors` or `transition-opacity` for just the hover effects.

---

## Positive Observations

1. **WebSocket auth guard (`wsMayConnect`)**: Well-implemented. Prevents WS connection attempts when auth hasn't completed, with proper cleanup in `stopWebSocket`. This fixes a real race condition from the previous code.

2. **Folder multi-select architecture**: Clean state management with `normalizeFolderIds`, `selectedFolderIds` computed, and `toggleFolderSelection`. The deduplication and sorting ensures consistent API queries.

3. **Mobile history stack**: The `pushMobileHistoryIfNeeded` / `popstate` pattern correctly handles Android back button navigation for plain chat views. The layer counting prevents over-popping.

4. **Right dock FSM**: The `onToolClick` toggle logic (open/switch/close) is clean and avoids panel state conflicts. The `hydratePanel` lazy-loading on mode switch prevents unnecessary API calls.

5. **Sidebar width validation**: The new initialization validates localStorage values against min/max bounds (`220-600`) with `Number.isFinite` check. Prevents NaN/corrupt values from breaking layout.

6. **Per-chat AI threads (`aiMessagesByChatId`)**: Good UX improvement -- switching between chats preserves AI conversation context.

---

## Recommended Actions (Priority Order)

1. **Memoize `getMessageHighlight`** -- computed Map keyed by msg.id, invalidated only when `activeHighlights` or `sortedMessages` change
2. **Add `onBeforeUnmount` cleanup** for resize and popstate listeners
3. **Clear `_scrollUpTimer`** on chat switch in `selectChat`
4. **Fix `showScrollToTop`/`showScrollToBottom` FSM** -- use single tri-state ref
5. **Remove debug console.log** from `navigateSearchMatch`
6. **Debounce `windowWidth` ref** updates to avoid layout thrashing

---

## Unresolved Questions

1. The search navigation direction was intentionally flipped (`+1` = up arrow, `-1` = down arrow). Was this tested with `flex-col-reverse` scroll behavior? The visual direction may be correct but the semantic labels ("Previous/Next") need verification.
2. Does the backend support `folder_ids` as repeated query params (line ~4642 `&folder_ids=${id}`)? The old code used singular `folder_id`. If the backend doesn't handle the array form, multi-folder filtering silently fails.
3. The `showMediaPanel` ref is still declared and used as a "legacy flag" alongside the dock. Is there a migration path to remove it, or does it serve the mobile-only media overlay?
