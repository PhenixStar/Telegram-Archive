---
phase: 5
title: "Token Chat Search Filter"
status: pending
priority: P2
effort: 1h
---

# Phase 5: Token Chat Search Filter

## Context
- [Parent plan](plan.md)
- Token UI: `index.html:3220-3315`
- `adminChats` ref: list of all chats for token scoping
- `adminTokenForm.allowed_chat_ids`: selected chat IDs array

## Overview
When creating/editing tokens, the "Allowed Chats" section shows checkboxes for all chats. With 100+ chats, finding the right ones is tedious. Add a search/filter input above the checkbox list.

## Key Insights
- `adminChats` is an array of chat objects with `id`, `title`, `username`, `type`
- Current UI: flat checkbox list with `v-for="chat in adminChats"` (lines 3228-3233)
- Edit mode has similar list (lines 3291-3313)
- Chat titles may include emojis, non-Latin characters

## Requirements
- Search input above chat checkboxes (filters by title or username)
- Works in both create and edit token modes
- Clear button to reset search
- Show match count ("3 of 47 chats")

## Architecture
```js
// New state
const tokenChatSearch = ref('')
const filteredTokenChats = computed(() => {
    const q = tokenChatSearch.value.toLowerCase().trim()
    if (!q) return adminChats.value
    return adminChats.value.filter(c =>
        (c.title || '').toLowerCase().includes(q) ||
        (c.username || '').toLowerCase().includes(q)
    )
})
```

```html
<!-- Search bar above chat checkboxes -->
<div class="relative mb-2">
    <input v-model="tokenChatSearch" type="text" placeholder="Search chats..."
        class="w-full rounded-lg pl-8 pr-8 py-1.5 text-xs ..."
        style="background: ...; color: var(--tg-text);">
    <svg class="w-3.5 h-3.5 absolute left-2.5 top-2 text-tg-muted" ...search icon...></svg>
    <button v-if="tokenChatSearch" @click="tokenChatSearch = ''"
        class="absolute right-2 top-1.5 text-tg-muted hover:text-white">
        <i class="fas fa-times text-xs"></i>
    </button>
</div>
<p class="text-xs text-tg-muted mb-1">
    {{ filteredTokenChats.length }} of {{ adminChats.length }} chats
</p>
<!-- Existing checkbox list, but iterate filteredTokenChats instead of adminChats -->
```

## Implementation Steps
1. Add `tokenChatSearch` ref and `filteredTokenChats` computed
2. Insert search input above create-mode chat checkboxes
3. Change `v-for="chat in adminChats"` to `v-for="chat in filteredTokenChats"` in both create and edit sections
4. Add clear button and match count
5. Expose in return block

## Todo
- [ ] Add `tokenChatSearch` ref + `filteredTokenChats` computed
- [ ] Insert search input in create token form
- [ ] Insert search input in edit token form
- [ ] Replace `adminChats` iteration with `filteredTokenChats`
- [ ] Add match count display
- [ ] Expose in return block

## Success Criteria
- Typing in search instantly filters chat checkboxes
- Selected chats remain checked even when filtered out of view
- Search works in both create and edit modes
- Clear button resets to full list

## Risk
- Selected chat IDs must be preserved when search filters the display — use `adminTokenForm.allowed_chat_ids` as source of truth (not filtered list)
