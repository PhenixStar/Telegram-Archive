---
phase: 3
title: "Compact chat list (avatars only)"
status: pending
priority: P1
---

# Phase 3: Compact Chat List — Avatars Only

## Context
- [Parent plan](plan.md)
- File: `repo/dev/src/web/templates/index.html`
- Chat list container: line 1224 (`flex-1 overflow-y-auto`)
- Chat row template: lines 1334-1388
- Avatar element: lines 1339-1356 (w-12 h-12 rounded-full)

## Overview
When `sidebarFocusCompact` is true, each chat row shows ONLY the avatar circle — no name, no message preview, no timestamp, no unread dot. The avatars center in the 64px rail. Hover tooltip shows chat name.

## Architecture

```
Normal chat row:                 Compact chat row:
┌──────────────────────┐        ┌────┐
│ [avatar] Chat Name   │        │ ○  │  ← 48px avatar, centered
│          Preview...  │        └────┘
└──────────────────────┘
```

## Related Code Files
- `index.html:1334-1388` — Chat item `v-for` row
- `index.html:1339-1356` — Avatar div (w-12 h-12)
- `index.html:1358-1387` — Chat name + preview + date (the part to hide)
- `index.html:1316-1331` — Archived chats row (also needs compact variant)

## Implementation Steps

1. **Chat row: conditional layout**
   ```html
   <div v-for="(chat, chatIdx) in filteredChats" :key="chat.id"
       @click="selectChat(chat)"
       class="cursor-pointer hover:bg-tg-hover transition-colors"
       :class="[
           selectedChat?.id === chat.id ? 'bg-tg-active sidebar-chat-active' : '',
           chatListFocusIndex === chatIdx ? 'chat-focused' : '',
           sidebarFocusCompact
               ? 'flex items-center justify-center py-2'
               : 'p-3 flex items-center gap-3'
       ]"
       :title="sidebarFocusCompact ? getChatName(chat) : undefined"
   >
       <!-- Avatar (always shown, same size) -->
       <div class="w-12 h-12 rounded-full ... relative"
           :class="{ 'ring-2 ring-blue-500': sidebarFocusCompact && selectedChat?.id === chat.id }">
           ...existing avatar content...
       </div>

       <!-- Chat info (hidden in compact) -->
       <div v-if="!sidebarFocusCompact" class="flex-1 min-w-0">
           ...existing name/preview/date...
       </div>
   </div>
   ```

2. **Selected chat indicator** — In compact mode, use a blue ring on the avatar instead of background highlight (since the row is just the avatar).

3. **Unread indicator** — In compact mode, show as a small dot overlaid on the avatar (absolute positioned, bottom-right corner) instead of inline.

4. **Archived chats row** — In compact mode, show the archive icon circle only (no text). Tooltip: "Archived Chats (N)".

5. **Topics view** — When in forum topics navigation inside compact sidebar, show topic icons only (same avatar-only pattern).

## Todo
- [ ] Chat row: conditional `p-3 flex gap-3` vs `justify-center py-2`
- [ ] Hide `div.flex-1.min-w-0` (name/preview) when compact
- [ ] Add `title` attribute for chat name tooltip in compact
- [ ] Selected chat: blue ring on avatar in compact mode
- [ ] Unread dot: absolute overlay on avatar corner in compact
- [ ] Archived row: icon-only in compact with tooltip
- [ ] Topics view: icon-only in compact (if applicable)

## Success Criteria
- Compact sidebar shows centered avatar column
- Hovering any avatar shows chat name tooltip
- Selected chat has visible ring highlight
- Unread indicator visible as small dot on avatar
- Clicking avatar opens the chat (same as normal mode)
- Scrolling works normally in compact list

## Risk
- Long chat lists may need scroll snapping or spacing adjustments at 64px width
- Context menu (`@contextmenu`) still needs to work on compact avatars
