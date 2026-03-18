# Phase 2: Enhance Header Search with Count + Highlighting + Navigation

## Context

- [Plan overview](./plan.md)
- [Frontend research](./reports/researcher-260318-1345-chat-search-frontend.md)
- Depends on: Phase 1 (contrast fix) — COMPLETE
- File: `src/web/templates/index.html`

## Overview

- **Priority:** High
- **Status:** Complete
- **Description:** Add match count display ("3/15"), highlight matches in messages, add up/down arrow buttons to header search — like browser Ctrl+F

## Key Insights

- `searchMessages()` (line 5996) resets `messages.value = []` then calls `loadMessages()` — async
- `loadMessages()` (line 5859) fetches server-side with `?search=Q`, returns paginated results
- After messages render, `applySearchHighlights()` (line 6883) can highlight matches
- `navigateSearchMatch()` (line 6919) already handles up/down with `.current` class + scrollIntoView
- `searchMatchCount`/`searchMatchIndex` refs (lines 6826-6827) already exposed in return block
- These state vars are shared with overlay search — acceptable since both won't be active simultaneously
- `linkifyText(msg.text, messageSearchQuery)` at line 2200 already does inline highlighting via v-html — but DOM TreeWalker highlighting (`applySearchHighlights`) is more reliable for match counting

## Requirements

### Functional
- After header search returns results, highlight all matches with `<mark>` tags
- Display match count next to search input: "3 / 15" format (or "No matches")
- Up/Down arrow buttons next to count to jump between matches
- Enter key navigates to next match
- Escape key clears search and restores original messages
- Clearing the search field restores original messages (existing behavior)
- Results persist until leaving chat or clearing field (already works)

### Non-functional
- Debounce search input (already 0ms via immediate `@input`, but `loadMessages` is async so naturally debounced)
- Reuse existing functions: `applySearchHighlights`, `navigateSearchMatch`, `clearSearchHighlights`
- No new ref vars — reuse `searchMatchCount`, `searchMatchIndex`

## Architecture

```
User types in header search input
  → @input="searchMessages" (line 5996)
    → messages.value = [], page = 0, hasMore = true
    → await loadMessages() (fetches with ?search=Q)
    → NEW: await nextTick()
    → NEW: applySearchHighlights(messagesContainer, messageSearchQuery)
    → NEW: count marks, set searchMatchCount/searchMatchIndex
    → NEW: scroll first match into view

Up/Down arrows or Enter
  → navigateSearchMatch(+1/-1) — already works

Escape key
  → NEW: clearHeaderSearch() — clear query, highlights, reload messages

Clear field (empty input)
  → @input fires searchMessages()
  → loadMessages() fetches without search param → original messages
  → NEW: clearSearchHighlights(), reset counts
```

## Related Code Files

- **Modify:** `src/web/templates/index.html`
  - Lines 1503-1512: Header search HTML — add count display + arrows
  - Line 5996-6001: `searchMessages()` — add post-search highlighting
  - Near line 6928: Add `clearHeaderSearch()` function
  - Return block (~7481): Expose new function if needed

## Implementation Steps

### Step 1: Modify `searchMessages()` to apply highlights after load (line 5996)

Replace:
```js
const searchMessages = async () => {
    messages.value = []
    page.value = 0
    hasMore.value = true
    await loadMessages()
}
```

With:
```js
const searchMessages = async () => {
    messages.value = []
    page.value = 0
    hasMore.value = true
    // Clear previous highlights before loading
    clearSearchHighlights()
    searchMatchCount.value = 0
    searchMatchIndex.value = -1
    await loadMessages()
    // Apply highlights after messages render
    const q = messageSearchQuery.value.trim()
    if (q) {
        await Vue.nextTick()
        applySearchHighlights(messagesContainer.value, q)
        const marks = messagesContainer.value?.querySelectorAll('mark') || []
        searchMatchCount.value = marks.length
        searchMatchIndex.value = marks.length > 0 ? 0 : -1
        if (marks.length > 0) {
            marks[0].classList.add('current')
            marks[0].scrollIntoView({ block: 'center', behavior: 'smooth' })
        }
    } else {
        // Field cleared — reset highlight state
        searchMatchCount.value = 0
        searchMatchIndex.value = -1
    }
}
```

### Step 2: Add `clearHeaderSearch()` function (near line 6928)

```js
const clearHeaderSearch = () => {
    messageSearchQuery.value = ''
    clearSearchHighlights()
    searchMatchCount.value = 0
    searchMatchIndex.value = -1
    // Reload original messages
    messages.value = []
    page.value = 0
    hasMore.value = true
    loadMessages()
}
```

Add to return block (~7481): `clearHeaderSearch,`

### Step 3: Update header search HTML (lines 1503-1512)

Replace current search input block with:
```html
<!-- Search Bar - responsive width -->
<div class="relative flex items-center gap-1" style="min-width: 5rem;">
    <div class="relative flex-1" style="max-width: 16rem;">
        <input v-model="messageSearchQuery" @input="searchMessages"
            @keydown.enter.prevent="navigateSearchMatch(1)"
            @keydown.escape.prevent="clearHeaderSearch"
            type="text" placeholder="Search..."
            class="w-full text-white rounded-lg pl-8 sm:pl-10 pr-2 sm:pr-4 py-1.5 sm:py-2 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            style="background: color-mix(in srgb, var(--tg-sidebar) 80%, white)">
        <svg class="w-3.5 h-3.5 sm:w-4 sm:h-4 text-gray-400 absolute left-2 sm:left-3 top-2 sm:top-2.5" fill="none"
            stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path>
        </svg>
    </div>
    <!-- Match count + navigation (visible when searching) -->
    <template v-if="messageSearchQuery.trim()">
        <span class="text-xs whitespace-nowrap hidden sm:inline" style="color: var(--tg-muted);">
            {{ searchMatchCount > 0 ? `${searchMatchIndex + 1} / ${searchMatchCount}` : 'No matches' }}
        </span>
        <button v-if="searchMatchCount > 0" @click="navigateSearchMatch(-1)"
            class="p-0.5 rounded hover:opacity-80" style="color: var(--tg-muted);" title="Previous match">
            <i class="fas fa-chevron-up text-[10px]"></i>
        </button>
        <button v-if="searchMatchCount > 0" @click="navigateSearchMatch(1)"
            class="p-0.5 rounded hover:opacity-80" style="color: var(--tg-muted);" title="Next match">
            <i class="fas fa-chevron-down text-[10px]"></i>
        </button>
    </template>
</div>
```

Key changes:
- Input wrapped in flex container with count + arrows
- `@keydown.enter` → next match, `@keydown.escape` → clear search
- Count shown only when query non-empty, hidden on mobile (`hidden sm:inline`)
- Arrows only when matches > 0
- Uses `var(--tg-muted)` for count/arrows (consistent with overlay)
- Responsive: arrows use smaller `text-[10px]` to fit header space
- Background uses `color-mix()` from Phase 1

### Step 4: Handle edge case — new page loads during search

When user scrolls down and `loadMessages()` loads more pages during an active search, new messages won't have highlights. Add a `watch` or post-load hook:

In `loadMessages()` finally block (around line 5941), after `await nextTick()`:
```js
// Re-apply search highlights if header search is active
if (messageSearchQuery.value.trim()) {
    applySearchHighlights(messagesContainer.value, messageSearchQuery.value.trim())
    const marks = messagesContainer.value?.querySelectorAll('mark') || []
    searchMatchCount.value = marks.length
    // Preserve current index if valid, else reset
    if (searchMatchIndex.value >= marks.length) {
        searchMatchIndex.value = marks.length > 0 ? 0 : -1
    }
}
```

This ensures highlights persist across paginated loads.

## Todo List

- [x] Modify `searchMessages()` to apply highlights after load
- [x] Add `clearHeaderSearch()` function
- [x] Expose `clearHeaderSearch` in return block
- [x] Update header search HTML with count display + up/down arrows
- [x] Add re-highlight logic in `loadMessages()` finally block
- [x] Test: type query → see count → arrows navigate matches
- [x] Test: press Enter → next match, Escape → clear
- [x] Test: scroll to load more pages → new matches highlighted
- [x] Test: switch chat → highlights and count cleared (Phase 1)
- [x] Test on mobile — count hidden, arrows still accessible

## Success Criteria

- Match count displays next to search input ("3 / 15" format)
- Up/down arrows navigate between highlighted matches with orange `.current` highlight
- Enter jumps to next match, Escape clears search
- Highlights persist across paginated loads
- Results persist until chat switch or field clear
- No interference with overlay search (Ctrl+F)

## Risk Assessment

- **Medium:** Shared `searchMatchCount`/`searchMatchIndex` between header and overlay. If user has header search active and presses Ctrl+F, overlay takes over state. Acceptable — UX is coherent since overlay replaces the highlight context.
- **Low:** `applySearchHighlights` is idempotent (calls `clearSearchHighlights` first) — safe to call multiple times
- **Medium:** Re-highlighting in `loadMessages()` finally block runs on every page load even without search. Mitigated by `messageSearchQuery.value.trim()` guard — cost is one string check per load.
- **Low:** `linkifyText(msg.text, messageSearchQuery)` at line 2200 still does v-html highlighting in parallel. Both highlighting mechanisms coexist safely — DOM TreeWalker skips existing `<mark>` tags.

## Security Considerations

- `applySearchHighlights` uses `document.createTextNode()` for user input — no XSS risk
- Search query is URL-encoded when sent to server (line 5899)
