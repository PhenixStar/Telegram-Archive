# Phase 3: Advanced Search UI with Date Range

## Priority: MEDIUM
## Status: COMPLETE
## Depends on: Phase 1 (date_from/date_to backend params)

---

## Overview

Add an "Advanced" toggle to the in-chat search bar. When toggled, reveal date-from and date-to fields inline. Dates filter the message search query.

---

## Related Code Files

### Modify
- `src/web/templates/index.html`:
  - Search bar template (line 1030-1041)
  - State declarations (~line 2147)
  - `searchMessages()` / `loadMessages()` — pass date params
  - Return block

---

## Implementation Steps

### Step 1: Add state refs

```javascript
const showAdvancedSearch = ref(false)
const searchDateFrom = ref('')
const searchDateTo = ref('')
```

### Step 2: UI — Advanced toggle + date fields

Current search bar (line 1030-1041):
```html
<div class="flex items-center gap-1 sm:gap-2 shrink-0">
    <div class="relative w-28 sm:w-48 md:w-64">
        <input v-model="messageSearchQuery" ...>
        <svg ...search icon...>
    </div>
```

Modified — add toggle button after search input, collapsible date row below:

```html
<div class="flex items-center gap-1 sm:gap-2 shrink-0 flex-wrap">
    <div class="flex items-center gap-1">
        <!-- Existing search input -->
        <div class="relative w-28 sm:w-48 md:w-64">
            <input v-model="messageSearchQuery" @input="searchMessages" ...>
            <svg ...>
        </div>
        <!-- Advanced toggle -->
        <button @click="showAdvancedSearch = !showAdvancedSearch"
            :class="showAdvancedSearch ? 'text-blue-400' : 'text-gray-400'"
            class="p-1.5 hover:text-white rounded-lg hover:bg-white/10 transition-colors"
            title="Advanced search">
            <i class="fas fa-sliders-h text-xs"></i>
        </button>
    </div>

    <!-- Advanced search fields (collapsed by default) -->
    <div v-if="showAdvancedSearch"
         class="flex items-center gap-2 w-full mt-1 sm:mt-0 sm:w-auto">
        <input v-model="searchDateFrom" type="date"
            @change="searchMessages"
            class="bg-gray-900 text-white text-xs rounded px-2 py-1 border border-gray-600 focus:border-blue-500 focus:outline-none"
            title="From date">
        <span class="text-gray-500 text-xs">—</span>
        <input v-model="searchDateTo" type="date"
            @change="searchMessages"
            class="bg-gray-900 text-white text-xs rounded px-2 py-1 border border-gray-600 focus:border-blue-500 focus:outline-none"
            title="To date">
        <button v-if="searchDateFrom || searchDateTo"
            @click="searchDateFrom = ''; searchDateTo = ''; searchMessages()"
            class="text-gray-400 hover:text-white text-xs px-1" title="Clear dates">
            <i class="fas fa-times"></i>
        </button>
    </div>

    <!-- Existing export + AI buttons -->
    ...
</div>
```

### Step 3: Pass date params in loadMessages

In `loadMessages()` search branch (~line 3909):

```javascript
if (messageSearchQuery.value) {
    // ... existing search URL build ...
    if (searchDateFrom.value) {
        url += `&date_from=${encodeURIComponent(searchDateFrom.value + 'T00:00:00')}`
    }
    if (searchDateTo.value) {
        url += `&date_to=${encodeURIComponent(searchDateTo.value + 'T23:59:59')}`
    }
}
```

Also support date-only filtering (no text query):

```javascript
// If dates set but no search text, still apply date filter
if (!messageSearchQuery.value && (searchDateFrom.value || searchDateTo.value)) {
    url = `/api/chats/${selectedChat.value.id}/messages?limit=${limit}&offset=${offset}`
    if (searchDateFrom.value) url += `&date_from=${encodeURIComponent(searchDateFrom.value + 'T00:00:00')}`
    if (searchDateTo.value) url += `&date_to=${encodeURIComponent(searchDateTo.value + 'T23:59:59')}`
}
```

### Step 4: Reset dates on chat switch

In `selectChat()`, clear advanced search state:
```javascript
showAdvancedSearch.value = false
searchDateFrom.value = ''
searchDateTo.value = ''
```

### Step 5: Expose in return block

```javascript
showAdvancedSearch, searchDateFrom, searchDateTo,
```

---

## Todo List

- [x] Add `showAdvancedSearch`, `searchDateFrom`, `searchDateTo` refs
- [x] Add advanced toggle button in search bar
- [x] Add collapsible date-from/date-to fields
- [x] Pass date params in `loadMessages()` search flow
- [x] Support date-only filtering (no search text)
- [x] Clear state on chat switch
- [x] Expose in return block
- [x] Test mobile responsiveness (fields wrap on small screens)

---

## Success Criteria

1. Click advanced toggle → date fields appear inline
2. Set date range → messages filtered to that range
3. Works with text search (AND condition)
4. Works without text search (date-only filter)
5. Clear button resets dates
6. Date fields collapse when toggling off
7. Mobile: fields wrap neatly below search bar
