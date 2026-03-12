# Phase 09: Enhanced Message Search

## Context

- [Research: Search UX, DB Indexing, AI Panel](../reports/researcher-260311-1743-search-indexing-ai-panel.md)
- Current search: `GET /api/chats/{chat_id}/messages?search=term` uses `ILIKE %term%` (adapter.py line 1117-1118)
- Chat sidebar search exists: filters chat titles client-side
- No search result highlighting, no match count, no cross-chat search
- Phase 9 (FTS) provides the backend optimization; this phase is the UX layer

## Overview

- **Priority:** HIGH
- **Status:** Pending
- **Description:** Sticky search bar in message area, debounced search-as-you-type, match highlighting, match count + navigation arrows, cross-chat search option

## Key Insights

- Search bar should be pinned to top of message area (inside the messages panel, above the scrollable container)
- Ctrl+F toggles search bar (wired in Phase 6 keyboard handler)
- Highlighting: client-side `<mark>` tags via `highlightText()` function applied to message text
- `v-html` already used in message rendering via `linkifyText()` -- highlight can be composed with it
- Cross-chat search is a separate mode that shows results as a list (chat name + message preview)
- Backend: use existing `?search=` param for per-chat, new `/api/search` endpoint for cross-chat (Phase 9)
- Until Phase 9 (FTS) is done, per-chat search uses existing ILIKE; cross-chat deferred until FTS ready

## Requirements

**Functional:**
- Sticky search bar at top of message area, toggleable via Ctrl+F or icon button
- Search input with 300ms debounce
- Match count display: "X of Y matches"
- Up/down navigation arrows to jump between highlighted matches
- `<mark>` highlighting of search terms in rendered messages
- Close via Escape or X button
- Optional "Search all chats" toggle (functional only after Phase 9)

**Non-functional:**
- Cancel in-flight requests on new keystrokes (`AbortController`)
- Highlight CSS must work in both dark and light themes
- No re-fetch if query unchanged

## Architecture

```
Search bar state:
  searchBarVisible = ref(false)
  searchQuery = ref('')
  searchResults = ref({ total: 0, currentIndex: -1 })
  searchHighlightTerm = ref('')
  searchAbortController = ref(null)

Flow:
  Ctrl+F -> searchBarVisible = true, focus input
  User types -> debounce 300ms -> fetch /api/chats/{id}/messages?search=term
  Results render with <mark> highlighting
  Up/Down arrows in search bar -> navigate between matches (scroll to each)
  Escape -> close search bar, clear highlights
```

## Related Code Files

**Modify:**
- `src/web/templates/index.html`:
  - HTML: add search bar div (sticky, top of message panel)
  - JS: add search state refs, `toggleSearchBar()`, `handleSearchInput()`, `highlightText()`, `navigateSearchMatch()`
  - JS: update `linkifyText()` pipeline to include highlight (compose: linkify then highlight)
  - CSS: `mark` tag styling for dark and light themes
- `src/web/main.py`:
  - Modify `GET /api/chats/{chat_id}/messages` response to include `total_matches` count when `search` param provided
  - Add `GET /api/search` endpoint stub (functional in Phase 9)

## Implementation Steps

1. **Search bar HTML**:
   - Inside message panel, above `messagesContainer` div
   - `v-show="searchBarVisible"` with slide-down transition
   - Layout: `[search icon] [input] [X of Y] [up arrow] [down arrow] [close X]`
   - Sticky positioning: `position: sticky; top: 0; z-index: 10`

2. **Search state** (JS):
   ```js
   const searchBarVisible = ref(false)
   const searchQuery = ref('')
   const searchMatchCount = ref(0)
   const searchMatchIndex = ref(-1)
   const searchHighlightTerm = ref('')
   let searchDebounceTimer = null
   let searchAbortCtrl = null
   ```

3. **Toggle function**:
   ```js
   const toggleSearchBar = () => {
     searchBarVisible.value = !searchBarVisible.value
     if (searchBarVisible.value) nextTick(() => searchInput.value?.focus())
     else clearSearch()
   }
   ```
   Wire to Ctrl+F in Phase 6 global handler.

4. **Debounced search**:
   ```js
   const handleSearchInput = () => {
     clearTimeout(searchDebounceTimer)
     if (searchAbortCtrl) searchAbortCtrl.abort()
     searchDebounceTimer = setTimeout(async () => {
       if (!searchQuery.value.trim()) { clearSearch(); return }
       searchAbortCtrl = new AbortController()
       searchHighlightTerm.value = searchQuery.value
       // [RED TEAM] Verify actual loadMessages() signature before implementing.
       // Current signature may not accept options object -- may need to pass
       // search param via a ref (e.g. searchFilter.value) that loadMessages reads,
       // or use a separate fetch call to /api/chats/{id}/messages?search=term
       // and replace messages.value with the response directly.
       // AbortController must be wired to the fetch() call, not loadMessages().
       const resp = await fetch(
         `/api/chats/${selectedChat.value.id}/messages?search=${encodeURIComponent(searchQuery.value)}`,
         { signal: searchAbortCtrl.signal }
       )
       if (!resp.ok) return
       const data = await resp.json()
       messages.value = data.messages
       searchMatchCount.value = data.total_matches || 0
       searchMatchIndex.value = searchMatchCount.value > 0 ? 0 : -1
     }, 300)
   }
   ```

5. **Highlight function** -- **[RED TEAM] DOM TreeWalker approach (mandatory, not regex on HTML)**:
   ```js
   // DO NOT use regex on HTML output -- breaks <a href> attributes, XSS vector
   // Instead: apply highlights POST-RENDER via DOM TreeWalker on text nodes only
   const applySearchHighlights = (containerEl, term) => {
     if (!term || !containerEl) return
     clearSearchHighlights(containerEl) // remove existing <mark> first
     const walker = document.createTreeWalker(containerEl, NodeFilter.SHOW_TEXT)
     const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
     const regex = new RegExp(`(${escaped})`, 'gi')
     const textNodes = []
     while (walker.nextNode()) textNodes.push(walker.currentNode)
     textNodes.forEach(node => {
       if (!regex.test(node.textContent)) return
       regex.lastIndex = 0
       const frag = document.createDocumentFragment()
       let lastIndex = 0
       let match
       while ((match = regex.exec(node.textContent)) !== null) {
         frag.appendChild(document.createTextNode(node.textContent.slice(lastIndex, match.index)))
         const mark = document.createElement('mark')
         mark.textContent = match[1]
         frag.appendChild(mark)
         lastIndex = regex.lastIndex
       }
       frag.appendChild(document.createTextNode(node.textContent.slice(lastIndex)))
       node.parentNode.replaceChild(frag, node)
     })
   }
   ```
   Call `applySearchHighlights()` in `nextTick()` after messages render (NOT via `v-html` composition).
   `v-html` continues to use `linkifyText()` only -- highlight is a separate DOM pass.

6. **Match navigation**:
   - After render, count `<mark>` elements in container
   - Up/Down buttons: increment/decrement `searchMatchIndex`, scroll `<mark>` element into view
   - Display: `"${searchMatchIndex + 1} of ${searchMatchCount}"`

7. **Highlight CSS**:
   ```css
   mark {
     background: #ffeb3b;
     color: #000;
     border-radius: 2px;
     padding: 0 1px;
   }
   mark.current {
     background: #ff9800;
   }
   ```

8. **Clear search**:
   - Reset all search state
   - Remove `searchHighlightTerm` (highlights disappear from template)
   - Reload messages without search filter

9. **Backend match count** (`main.py`):
   - When `search` param provided, add a count query alongside pagination
   - Return `total_matches` in response metadata

10. **Cross-chat search stub**:
    - Add checkbox/toggle "Search all chats" in search bar (disabled until Phase 9)
    - When Phase 9 ready: toggle switches to `/api/search?q=` endpoint
    - Results shown in a dropdown/overlay with chat name + message preview + click-to-navigate

## Todo

- [ ] Add search bar HTML template (sticky, top of message panel)
- [ ] Add search state refs and `toggleSearchBar()`
- [ ] Implement debounced `handleSearchInput()` with `AbortController`
- [ ] **[RED TEAM]** Implement `applySearchHighlights()` via DOM TreeWalker (text nodes only, NOT regex on HTML)
- [ ] Implement `clearSearchHighlights()` to remove `<mark>` elements
- [ ] Call highlight in `nextTick()` after message render (NOT composed with `linkifyText`)
- [ ] Add `mark` tag CSS for dark and light themes
- [ ] Implement match navigation (up/down arrows, scroll to current match)
- [ ] Implement match count display
- [ ] Implement `clearSearch()` on close/Escape
- [ ] Add `total_matches` to backend response when search param present
- [ ] Add "Search all chats" toggle (disabled/stub until Phase 9)
- [ ] Test: search, navigate matches, close, verify highlights removed
- [ ] Test: rapid typing triggers debounce correctly

## Success Criteria

- Ctrl+F or icon toggles search bar
- Typing highlights matching text in messages with `<mark>` tags
- Match count shows "X of Y"
- Up/down arrows jump between matches
- Escape or X clears search and highlights
- Search works in both dark and light themes
- No stale requests (AbortController cancels previous)

## Risk Assessment

- **[RED TEAM RESOLVED]** XSS via highlight: DOM TreeWalker approach operates on text nodes only, never touches HTML attributes or tag structure. Safe by design.
- **Performance with many matches** -- counting `<mark>` elements in DOM after every search
  - **Mitigation:** Limit to visible messages only; recount on scroll if needed
- **[RED TEAM RESOLVED]** Composing linkify + highlight: highlight is a separate post-render DOM pass, never composed with `linkifyText()` via regex

## Next Steps

- Phase 9 (FTS) enables fast cross-chat search and replaces ILIKE backend
