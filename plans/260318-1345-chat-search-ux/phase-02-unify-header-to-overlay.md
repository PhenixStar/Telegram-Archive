# Phase 2: Convert Header Search to Overlay Trigger

## Context

- [Frontend research](../reports/researcher-260318-1345-chat-search-frontend.md)
- File: `src/web/templates/index.html`
- Depends on: Phase 1

## Overview

- **Priority:** High
- **Status:** Pending
- **Description:** Replace header search input with a search icon button that opens the overlay search. Overlay already has match count, up/down navigation, highlighting — no need to duplicate.

## Key Insights

- Header search (line 1503-1512): `v-model="messageSearchQuery"`, `@input="searchMessages"` — replaces entire messages array, no highlighting, no match count
- Overlay search (line 1832-1847): `v-model="msgSearchQuery"`, has match count ("X/Y"), up/down chevrons, mark highlighting, semantic toggle
- Header search uses `searchMessages()` (line 5996) which resets `messages.value = []` then calls `loadMessages()` — destructive, loses scroll position
- Overlay search uses `handleSearchInput()` (line 6841) which fetches search results and applies DOM highlighting — non-destructive within the fetched results
- Date filter fields (lines 1514-1535) and advanced search toggle (lines 1537-1542) must be preserved — they work with `loadMessages()` independently

## Requirements

### Functional
- Header search input replaced with search icon button
- Clicking button opens overlay search bar (same as Ctrl+F)
- Date range filters preserved separately (not behind search button)
- Advanced search toggle preserved
- `searchMessages()` function preserved for date-range-only filtering
- Old `messageSearchQuery` still usable by date filter flow

### Non-functional
- No additional JS functions needed — reuse `toggleSearchBar()`
- Mobile-friendly: icon button takes less space than input field

## Related Code Files

- **Modify:** `src/web/templates/index.html`
  - Lines 1502-1512: Replace search input with icon button
  - Line 6832: `toggleSearchBar()` already exists — no changes needed
  - Lines 5996-6001: `searchMessages()` kept for date filter
  - `linkifyText` calls passing `messageSearchQuery` — leave as-is (used for date-filtered highlighting)

## Architecture

```
Before:
  Header: [search input] [date fields] [advanced toggle] [timeline]
  Overlay (Ctrl+F): [mode] [search input] [count] [up/down] [close]

After:
  Header: [🔍 button] [date fields] [advanced toggle] [timeline]
  Overlay (click or Ctrl+F): [mode] [search input] [count] [up/down] [close]
```

## Implementation Steps

1. **Replace header search input with icon button (lines 1502-1512)**

   Replace this block:
   ```html
   <!-- Search Bar - responsive width -->
   <div class="relative w-20 sm:w-48 md:w-64">
       <input v-model="messageSearchQuery" @input="searchMessages" ...>
       <svg ...search icon...></svg>
   </div>
   ```

   With:
   ```html
   <!-- Search button - opens overlay search -->
   <button @click="toggleSearchBar()"
       :style="searchBarVisible ? { color: 'var(--tg-accent)' } : { color: 'var(--tg-muted)' }"
       class="p-1.5 sm:p-2 hover:text-white rounded-lg hover:bg-white/10 transition-colors"
       title="Search messages (Ctrl+F)">
       <i class="fas fa-search text-xs sm:text-sm"></i>
   </button>
   ```

2. **Keep date filters working independently**
   The date filter fields (lines 1514-1535) use `messageSearchQuery` via `searchMessages()` → `loadMessages()`. They work independently — when search input is removed from header, date filters still call `searchMessages()` which uses `messageSearchQuery.value` (will be empty string = no text filter, just date filtering). No changes needed here.

3. **Verify `linkifyText` fallback**
   `linkifyText(msg.text, messageSearchQuery)` at line ~2200 passes header search query for inline highlighting. With header input removed, `messageSearchQuery` will always be empty — `linkifyText` returns unhighlighted text. This is correct because overlay search uses DOM-based highlighting (`applySearchHighlights`) instead.

## Todo List

- [ ] Replace header search input with search icon button
- [ ] Verify icon button toggles overlay correctly
- [ ] Verify date filters still work independently
- [ ] Verify Ctrl+F still works
- [ ] Test on mobile (icon should be smaller)
- [ ] Verify `linkifyText` doesn't break without messageSearchQuery

## Success Criteria

- Search icon button in header opens/closes overlay search
- Overlay search shows match count, up/down arrows, highlighting
- Date range filters still function independently
- Ctrl+F keyboard shortcut still works
- No dead code left (clean up if `messageSearchQuery` unused)

## Risk Assessment

- **Medium:** Removing header search input changes UX flow — users accustomed to typing directly in header. Mitigated: search button is in same position, overlay opens immediately with focus.
- **Low:** `messageSearchQuery` may still be needed by date filter flow. Must verify before removing the ref entirely.

## Security Considerations

- No auth/data changes — purely UI restructuring
