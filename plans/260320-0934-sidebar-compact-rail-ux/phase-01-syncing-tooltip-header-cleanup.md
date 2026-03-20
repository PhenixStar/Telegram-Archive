---
phase: 1
title: "Syncing tooltip + header cleanup"
status: pending
priority: P1
---

# Phase 1: Syncing Tooltip + Header Cleanup

## Context
- [Parent plan](plan.md)
- File: `repo/dev/src/web/templates/index.html`
- Sidebar header: lines 1052-1085

## Overview
The "Syncing..." text + spinner takes horizontal space in the sidebar header. Convert to hover-only tooltip on the spinner icon — cleaner in both normal and compact modes.

## Related Code Files
- `index.html:1070-1077` — loadingStats indicator (spinner + "Syncing..." span)
- `index.html:1078-1083` — Settings + Logout buttons

## Implementation Steps

1. **Replace the inline "Syncing..." text with hover tooltip:**
   ```html
   <!-- BEFORE: always-visible text -->
   <div v-if="loadingStats" class="flex items-center gap-1.5 text-xs text-tg-muted">
       <svg ...spinner...></svg>
       <span>Syncing...</span>
   </div>

   <!-- AFTER: icon-only with tooltip popup on hover -->
   <div v-if="loadingStats" class="relative group">
       <svg class="w-3.5 h-3.5 animate-spin text-tg-muted" ...></svg>
       <div class="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 px-2 py-1 text-xs rounded
                    bg-gray-900 text-white whitespace-nowrap opacity-0 group-hover:opacity-100
                    pointer-events-none transition-opacity z-50">
           Syncing...
       </div>
   </div>
   ```

2. **Verify tooltip doesn't clip** inside the sidebar header — add `overflow-visible` to parent if needed.

## Todo
- [ ] Replace "Syncing..." inline text with hover tooltip on spinner
- [ ] Test tooltip visibility in normal width sidebar
- [ ] Test tooltip visibility at minimum sidebar width (220px)

## Success Criteria
- Spinner visible when `loadingStats` is true
- "Syncing..." text only appears on hover as floating tooltip
- No layout shift in header row
