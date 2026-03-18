# Phase 3: Verify + Clean Up

## Context

- [Plan overview](./plan.md)
- Depends on: Phase 1, Phase 2 — COMPLETE
- File: `src/web/templates/index.html`

## Overview

- **Priority:** Medium
- **Status:** Complete
- **Description:** End-to-end verification of both search UIs, clean up any dead code, ensure no conflicts

## Implementation Steps

1. **Verify header search flow**
   - Type query → matches highlighted in yellow, current match in orange
   - Count shows "X / Y" next to input
   - Up/Down arrows navigate, Enter = next, Escape = clear
   - Scroll down to load more → new matches highlighted + count updated
   - Clear field → original messages restored, no highlights
   - Switch chat → search cleared, no stale highlights

2. **Verify overlay search flow (Ctrl+F)**
   - Ctrl+F opens overlay with lighter background
   - Type query → highlights + count + navigation (unchanged behavior)
   - Switch chat → overlay closes, state cleared (Phase 1 fix)
   - Semantic mode still works

3. **Verify no conflicts between both**
   - Header search active → Ctrl+F opens overlay → overlay takes over highlights
   - Overlay active → click header search → type query → header search takes over
   - No stale mark elements from one system when other activates

4. **Theme testing**
   - Dark (default), Telegram Dark, Midnight, AMOLED, Light
   - Both search inputs visibly lighter than header
   - Mark highlighting visible (yellow + orange) in all themes

5. **Mobile testing**
   - Count hidden on small screens (`hidden sm:inline`)
   - Up/down arrows still accessible
   - Search input doesn't overflow header

6. **Clean up `clearMsgSearch` in `selectChat`**
   - Verify `clearMsgSearch()` doesn't cause issues when called during `selectChat()` since `messages.value = []` is set at line 5689 BEFORE `clearMsgSearch()` would try to reload messages
   - If `clearMsgSearch` reloads messages unnecessarily, guard with a check: skip reload if `messages.value` is already empty

## Todo List

- [x] Manual test: header search full flow
- [x] Manual test: overlay search full flow
- [x] Manual test: both searches don't conflict
- [x] Manual test: all 5 themes
- [x] Manual test: mobile viewport
- [x] Verify `selectChat` + `clearMsgSearch` interaction
- [x] Remove any dead code if found

## Success Criteria

- Both search UIs work independently without interference
- All 5 themes render correctly
- No console errors
- Mobile layout not broken

## Risk Assessment

- **Medium:** `clearMsgSearch()` in `selectChat()` may trigger unnecessary message reload. Mitigated by checking the function's guard conditions — `hadQuery` check (line 6932) prevents reload when `msgSearchQuery` is empty.
