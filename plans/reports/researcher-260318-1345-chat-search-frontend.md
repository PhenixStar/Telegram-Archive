# In-Chat Search Implementation - Frontend Research

## Summary

Three distinct search systems exist in the frontend. This report focuses on the **in-chat message search** (Phase 9 search bar overlay), but documents the other two for context.

---

## 1. Search Input Fields

### A. Sidebar Global Search (line 1114)
- **v-model:** `searchQuery`
- **Trigger:** `@input="onSearchInput"`
- **Placeholder:** `"Search all chats..."`
- **Container:** `<div class="relative mb-3">` inside sidebar panel
- **Styling:** `w-full bg-gray-900 text-white rounded-lg pl-10 pr-4 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500`
- **Purpose:** Searches chats + global message search across all chats

### B. Header Search (line 1504) -- LEGACY/DUPLICATE
- **v-model:** `messageSearchQuery`
- **Trigger:** `@input="searchMessages"`
- **Placeholder:** `"Search..."`
- **Container:** `<div class="relative w-20 sm:w-48 md:w-64">` inside chat header toolbar
- **Styling:** `w-full bg-gray-900 text-white rounded-lg pl-8 sm:pl-10 pr-2 sm:pr-4 py-1.5 sm:py-2 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-blue-500`
- **Purpose:** Server-side message search within current chat, replaces messages array with results
- **Also has:** date range filters (`searchDateFrom`, `searchDateTo`), advanced search toggle

### C. Overlay Search Bar (line 1832-1846) -- PRIMARY IN-CHAT SEARCH
- **v-model:** `msgSearchQuery`
- **Trigger:** `@input="handleSearchInput"`, debounced 300ms
- **Placeholder:** `"Search messages..."` (text mode) / `"Semantic search (find similar meaning)..."` (semantic mode)
- **Container:** `<div v-show="searchBarVisible" class="absolute top-0 left-0 right-0 flex items-center gap-2 px-3 py-2 border-b shadow-lg">`
- **Styling:** `flex-1 bg-transparent text-sm outline-none`, with `color: var(--tg-text)`
- **Background:** `var(--tg-sidebar)`, border `var(--tg-border)`, z-index 20
- **Activated by:** `Ctrl+F` keyboard shortcut or `toggleSearchBar()` (line 6832)
- **Has enter/escape handlers:** Enter navigates to next match, Escape clears

---

## 2. How Search is Triggered

### Overlay Search (Primary -- `handleSearchInput`, line 6841)
1. User types in overlay input -> `@input="handleSearchInput"`
2. Debounce timer: 300ms for text mode, 500ms for semantic mode
3. Aborts previous in-flight request via `AbortController`
4. **Text mode:** Fetches `/api/chats/${chatId}/messages?search=${q}&limit=200`
5. Replaces `messages.value` with search results
6. After render (`Vue.nextTick`), calls `applySearchHighlights()` to add `<mark>` tags via DOM TreeWalker
7. **Semantic mode:** Fetches `/api/chats/${chatId}/semantic-search?q=${q}&limit=10` (line ~7015-7030)
8. Results shown in dropdown overlay, not inline highlights

### Header Search (`searchMessages`, line 5996)
1. Resets `messages.value = []`, `page.value = 0`, `hasMore.value = true`
2. Calls `loadMessages()` which appends `?search=` to the API URL (line 5898)
3. API endpoint: `/api/chats/${chatId}/messages?limit=X&offset=Y&search=Q`
4. Supports `from:SENDER_ID` syntax (line 4095)
5. No client-side highlighting

---

## 3. Search Result Handling & Highlighting

### Text Highlighting (overlay search)
- **`applySearchHighlights(container, term)`** (line 6883): DOM TreeWalker finds text nodes, wraps matches in `<mark>` tags
- Skips nodes inside `<a>`, `<button>`, `<svg>`, `<mark>` elements
- **`clearSearchHighlights(container)`** (line 6909): Unwraps `<mark>` back to text nodes, normalizes parent

### CSS for highlights (line 755-757)
```css
mark { background: #ffeb3b; color: #000; border-radius: 2px; padding: 0 1px; }
mark.current { background: #ff9800; }
[data-theme^="light"] mark { background: #ffeb3b; color: #000; }
```

### linkifyText highlighting (line 7039, 2200)
- `linkifyText(msg.text, messageSearchQuery)` -- the header search query is passed as highlight param
- Used in message rendering via `v-html`
- This is separate from the overlay search's DOM-based highlighting

---

## 4. CSS Variables for Top Bar / Header

### Header element (line 1445-1447)
```html
<div class="px-2 sm:px-4 py-2 sm:py-3 bg-tg-sidebar border-b flex items-center justify-between z-10 overflow-hidden max-w-full"
     style="border-color: var(--tg-border);">
```
- **Background:** Uses Tailwind class `bg-tg-sidebar` which maps to `var(--tg-sidebar)`
- **Border:** `var(--tg-border)`
- No dedicated `--tg-header-*` variables exist

### Relevant CSS variables (per theme, lines 57-190):
| Variable | Dark (default) | Telegram Dark | Midnight | AMOLED |
|----------|---------------|--------------|----------|--------|
| `--tg-sidebar` | `#0f0f0f` | `#0e1621` | `#1e293b` | `#000000` |
| `--tg-bg` | `#181818` | `#17212b` | `#0f172a` | `#000000` |
| `--tg-text` | `#ffffff` | `#e2e8f0` | `#e2e8f0` | `#e0e0e0` |
| `--tg-muted` | `#aaaaaa` | `#8696a7` | `#94a3b8` | `#777777` |
| `--tg-border` | `#303030` | `#1c2836` | `#334155` | `#1a1a1a` |
| `--tg-accent` | `#8774e1` | `#5eaddb` | `#3b82f6` | `#8774e1` |

### Search overlay bar uses:
- Background: `var(--tg-sidebar)`
- Border: `var(--tg-border)`
- Text: `var(--tg-text)`
- Buttons/counters: `var(--tg-muted)`
- Semantic mode toggle: `var(--tg-accent)`

---

## 5. Chat Navigation -- Search State Clearing

### When `selectChat()` is called (line ~5685-5709):
- `messageSearchQuery.value = ''` -- clears header search
- `showAdvancedSearch.value = false` -- hides date filters
- `searchDateFrom.value = ''` / `searchDateTo.value = ''`
- **Does NOT clear:** `searchBarVisible`, `msgSearchQuery`, `searchMatchCount`, `searchMatchIndex`

### When `selectTopic()` is called (line ~4553-4556):
- `messageSearchQuery.value = ''` -- clears header search only

### `clearMsgSearch()` (line 6928) clears overlay search:
- Sets `searchBarVisible = false`
- Clears `msgSearchQuery`, `searchMatchCount`, `searchMatchIndex`
- Clears `semanticResults`, `semanticSearchLoading`
- Calls `clearSearchHighlights()`
- Reloads original messages if had active query

### Gap: `selectChat` does NOT call `clearMsgSearch()`. The overlay search bar could persist when switching chats (though messages get replaced, the bar stays visible with stale state).

---

## 6. Match Count & Navigation Logic

### State (line 6824-6827):
```js
const searchBarVisible = ref(false)
const msgSearchQuery = ref('')
const searchMatchCount = ref(0)
const searchMatchIndex = ref(-1)
```

### Display (line 1838-1839):
```
{{ searchMatchCount > 0 ? `${searchMatchIndex + 1} / ${searchMatchCount}` : 'No matches' }}
```
- Shows `"X / Y"` format (1-indexed display, 0-indexed internal)
- Shows `"No matches"` when `searchMatchCount === 0`
- Only shown when `msgSearchQuery` is non-empty and not in semantic mode

### Up/Down arrows (lines 1844-1845):
- Visible only when `searchMatchCount > 0` and not in semantic mode
- Up arrow: `navigateSearchMatch(-1)` -- previous match
- Down arrow: `navigateSearchMatch(1)` -- next match
- Enter key also navigates to next match

### `navigateSearchMatch(dir)` (line 6919):
```js
const marks = messagesContainer.value?.querySelectorAll('mark') || []
if (marks.length === 0) return
marks.forEach(m => m.classList.remove('current'))
searchMatchIndex.value = (searchMatchIndex.value + dir + marks.length) % marks.length
marks[searchMatchIndex.value].classList.add('current')
marks[searchMatchIndex.value].scrollIntoView({ block: 'center', behavior: 'smooth' })
```
- Wraps around (modulo arithmetic)
- Adds `.current` class to active match (orange background)
- Scrolls active match into center of viewport

---

## 7. Semantic Search Mode (v9.1.0)

- Toggle button switches between text/semantic mode
- Semantic: fetches `/api/chats/${chatId}/semantic-search?q=${q}&limit=10`
- Results displayed in dropdown overlay (`max-height: 60vh`) below search bar
- Each result shows sender name, similarity score, text preview, timestamp
- Clicking a result calls `selectChat(selectedChat, { targetMsgId: result.id })` then `clearMsgSearch()`
- Loading spinner shown during semantic search
- Count display: `"X similar"` instead of `"X / Y"`

---

## Unresolved Questions

1. **Two overlapping search UIs:** Header search (`messageSearchQuery`) and overlay search (`msgSearchQuery`) coexist. Are both intentional? They serve different purposes (server-side filtering vs. in-page highlight+navigate) but could confuse users.
2. **selectChat gap:** Overlay search state (`searchBarVisible`, `msgSearchQuery`) is NOT cleared when switching chats. Is this a bug or intentional?
3. **Highlight limitation:** The overlay search fetches up to 200 messages then highlights client-side. If chat has >200 messages, some matches won't be found. No indication to user about this limit.
