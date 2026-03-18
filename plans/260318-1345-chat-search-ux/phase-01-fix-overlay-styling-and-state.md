# Phase 1: Fix Overlay Search Contrast + State Clearing

## Context

- [Frontend research](../reports/researcher-260318-1345-chat-search-frontend.md)
- File: `src/web/templates/index.html`

## Overview

- **Priority:** High
- **Status:** Pending
- **Description:** Fix 2 issues: (1) search bar background blends with header, (2) overlay state persists across chat switches

## Key Insights

- Overlay search bar (line 1832) uses `background: var(--tg-sidebar)` — same as header (line 1445 `bg-tg-sidebar`)
- `selectChat()` (line ~5695) clears `messageSearchQuery` but NOT overlay state (`searchBarVisible`, `msgSearchQuery`, `searchMatchCount`, `searchMatchIndex`)
- Theme CSS vars defined at lines 57-190

## Requirements

### Functional
- Search bar visually distinct from header — slightly lighter background
- Overlay search state fully cleared when switching chats

### Non-functional
- Must work across all 5 themes (dark, telegram-dark, midnight, amoled, light)
- No new CSS variables — use `rgba()` or `color-mix()` over existing vars

## Related Code Files

- **Modify:** `src/web/templates/index.html`
  - Line 1832: overlay search div `style="background: var(--tg-sidebar)"`
  - Lines ~5695-5714: `selectChat()` function — add `clearMsgSearch()` call

## Implementation Steps

1. **Fix search bar contrast (line 1832)**
   Change `background: var(--tg-sidebar)` to `background: color-mix(in srgb, var(--tg-sidebar) 85%, white)` on the overlay search div. This lightens it ~15% across all themes without hardcoding colors.

2. **Clear overlay search on chat switch (line ~5709)**
   Add `clearMsgSearch()` call inside `selectChat()` after `stopOcrPolling()` (line 5709). The function already exists (line 6928) and handles:
   - Setting `searchBarVisible = false`
   - Clearing `msgSearchQuery`, `searchMatchCount`, `searchMatchIndex`
   - Clearing semantic results
   - Removing highlight marks
   - Reloading original messages

## Todo List

- [ ] Change overlay search bar background to lighter shade via `color-mix()`
- [ ] Add `clearMsgSearch()` call in `selectChat()`
- [ ] Verify contrast across all 5 themes
- [ ] Verify search state clears on chat switch

## Success Criteria

- Search overlay visually distinguishable from header in all themes
- Switching chats closes search bar and clears all search state
- No regressions in search functionality

## Risk Assessment

- **Low:** `color-mix()` has 95%+ browser support (baseline 2023). Fallback not needed for this app's audience.
- **Low:** `clearMsgSearch()` already tested — just adding a call site
