---
phase: 4
title: "Stats relocation to profile panel"
status: pending
priority: P2
---

# Phase 4: Stats Relocation to Profile Panel

## Context
- [Parent plan](plan.md)
- File: `repo/dev/src/web/templates/index.html`
- Chat header stats: lines 1504-1516
- Profile panel stats: lines 1727-1739

## Overview
The per-chat stats in the chat header (📬 msgs, 🖼️ media, 💾 storage) duplicate info already in the profile panel. Remove from header, add storage size to profile panel for full parity.

## Related Code Files
- `index.html:1504-1516` — Chat header stats (3 spans + loading spinner)
- `index.html:1727-1739` — Profile panel stats (Messages, Media, Members)
- `chatStats` ref: loaded per chat via `/api/chats/{id}/stats`

## Implementation Steps

1. **Remove stats from chat header** (lines 1504-1516):
   - Delete the `<div v-if="chatStats" class="hidden sm:flex ...">` block (stats spans)
   - Delete the `<div v-else-if="selectedChat && !chatStats" ...>` block (loading spinner)
   - This frees ~60px horizontal space in the chat header for longer chat names

2. **Add storage size to profile panel** (after line 1735):
   ```html
   <div v-if="chatStats?.total_size_mb > 0" class="text-center">
       <div class="text-lg font-bold text-white">{{ formatSize(chatStats.total_size_mb) }}</div>
       <div class="text-xs text-tg-muted">Storage</div>
   </div>
   ```

3. **Verify** profile panel already loads `chatStats` — yes, `openProfile()` is called which triggers stat display. Stats load in `selectChat` → API call. Profile panel reads `chatStats` directly.

## Todo
- [ ] Remove chat header stats block (lines 1504-1516)
- [ ] Add "Storage" column to profile panel stats row
- [ ] Verify profile panel shows all 3 (or 4 with members) stat columns
- [ ] Test with chats that have 0 media / 0 storage (graceful hide)

## Success Criteria
- Chat header: cleaner, no stats pills — more space for chat name
- Profile panel: shows Messages, Media, Storage (and Members for groups)
- No stat information lost — all accessible via profile panel
- Mobile chat header: no change needed (stats were already `hidden sm:flex`)
