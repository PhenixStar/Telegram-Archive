---
phase: 2
title: "Compact logo with role ring"
status: pending
priority: P1
---

# Phase 2: Compact Logo with Role Ring

## Context
- [Parent plan](plan.md)
- File: `repo/dev/src/web/templates/index.html`
- Sidebar header: lines 1052-1085
- `sidebarFocusCompact` computed: line ~3651
- Crown icon: line 1065 (`roleIconClass`)

## Overview
When `sidebarFocusCompact` is true, the entire sidebar header collapses to a single centered circular logo (48px, matching chat avatar size). The logo has a thick colored ring border — **gold (amber-400)** for super_admin, **blue-400** for other roles, **gray-600** for unauthenticated.

Replace the crown/shield/user icon with the ring on the logo circle — the ring IS the role indicator.

## Key Insights
- Current header height is ~219px (title + search + folder dropdown + backup line)
- Compact header should be ~64px (logo circle centered with padding)
- Search bar, folder dropdown, backup info: all hidden in compact mode
- Settings/Logout buttons: hidden in compact (accessible via right-click or expanding sidebar)

## Architecture

```
Normal header:                    Compact header (64px rail):
┌──────────────────────┐         ┌────┐
│ [👑] Telegram Archive│         │    │
│              [⚙] [↗] │         │ ◉  │  ← 48px circle, 3px ring
│ [Search.........   ] │         │    │
│ [All Chats ▼       ] │         └────┘
│ Last backup: Today   │
└──────────────────────┘
```

## Related Code Files
- `index.html:1052-1085` — Header row (title, role icon, settings, logout)
- `index.html:1087-1096` — Search bar
- `index.html:1097-1145` — Folder dropdown
- `index.html:1146+` — "Last backup" line (if present)

## Implementation Steps

1. **Wrap the entire header `<div class="p-4 border-b">` content in two branches:**
   ```html
   <div class="p-4 border-b border-[color:var(--tg-border)]"
        :class="sidebarFocusCompact ? 'flex items-center justify-center py-2' : ''">

       <!-- COMPACT MODE: logo circle only -->
       <template v-if="sidebarFocusCompact">
           <div class="w-12 h-12 rounded-full flex items-center justify-center text-white font-bold text-lg cursor-pointer"
               :class="{
                   'ring-[3px] ring-amber-400': userRole === 'super_admin',
                   'ring-[3px] ring-blue-400': userRole && userRole !== 'super_admin',
                   'ring-[3px] ring-gray-600': !userRole,
               }"
               style="background: var(--tg-accent);"
               :title="'Telegram Archive' + (roleIconLabel ? ' — ' + roleIconLabel : '')"
               @click="rightDockOpen = false"
           >
               TA
           </div>
       </template>

       <!-- NORMAL MODE: existing header content -->
       <template v-else>
           ... existing header rows (title, search, folders) ...
       </template>
   </div>
   ```

2. **"TA" logo text** — Use first letters of "Telegram Archive" as the logo text (same pattern as chat avatars using initials). Or use an SVG logo if one exists.

3. **Click on compact logo** → expands sidebar (sets `rightDockOpen = false` which removes compact state via the computed).

4. **Tooltip on hover** — Title attribute shows "Telegram Archive — Super admin" (or role name).

## Todo
- [ ] Wrap header in `v-if="sidebarFocusCompact"` / `v-else` branches
- [ ] Compact: centered 48px circle with 3px ring (gold/blue/gray by role)
- [ ] Compact: hide search, folders, backup line
- [ ] Compact: click logo → expand sidebar (close dock)
- [ ] Normal: no changes to existing header layout
- [ ] Verify compact → normal transition is smooth (no flash)

## Success Criteria
- Compact mode shows only the logo circle with role-colored ring
- Gold ring visible for super_admin, blue for other authenticated roles
- Click logo restores full sidebar
- No layout jump or flash on transition

## Risk
- The ring thickness `ring-[3px]` may look different at different DPIs — test on retina/hidpi
