# Phase 1: Fix Search Bar Contrast + Overlay State Clearing

## Context

- [Plan overview](./plan.md)
- [Frontend research](./reports/researcher-260318-1345-chat-search-frontend.md)
- File: `src/web/templates/index.html`

## Overview

- **Priority:** High
- **Status:** Complete
- **Description:** Fix visual contrast on both search inputs + fix overlay state not clearing on chat switch

## Key Insights

- Both search bars use backgrounds that match the header (`var(--tg-sidebar)` / `bg-gray-900`)
- Header search input (line 1506): `class="... bg-gray-900 ..."` — hardcoded, doesn't follow theme vars
- Overlay search bar (line 1832): `style="background: var(--tg-sidebar)"` — theme-aware but same color as header
- `selectChat()` (line ~5695) clears `messageSearchQuery` but NOT overlay state
- `clearMsgSearch()` (line 6928) exists and handles all overlay cleanup

## Requirements

### Functional
- Both search inputs visually distinct from header background
- Overlay search state fully cleared when switching chats

### Non-functional
- Must work across all 5 themes (dark, telegram-dark, midnight, amoled, light)
- Header search should use CSS vars instead of hardcoded `bg-gray-900`

## Related Code Files

- **Modify:** `src/web/templates/index.html`
  - Line 1506: header search input `bg-gray-900` class
  - Line 1832: overlay search div `background: var(--tg-sidebar)`
  - Line ~5709: `selectChat()` — add `clearMsgSearch()` call

## Implementation Steps

1. **Fix header search input background (line 1506)**
   Replace `bg-gray-900` with inline style using theme vars:
   ```
   style="background: color-mix(in srgb, var(--tg-sidebar) 80%, white)"
   ```
   Remove `bg-gray-900` from the class list. This lightens ~20% from sidebar color across all themes.

2. **Fix overlay search bar background (line 1832)**
   Change `background: var(--tg-sidebar)` to:
   ```
   background: color-mix(in srgb, var(--tg-sidebar) 80%, white)
   ```
   Same treatment as header search for visual consistency between the two.

3. **Clear overlay state on chat switch (line ~5709)**
   In `selectChat()`, after `stopOcrPolling()` (line 5709), add:
   ```js
   clearMsgSearch()
   ```
   This calls the existing function (line 6928) that handles:
   - `searchBarVisible = false`
   - `msgSearchQuery = ''`
   - `searchMatchCount = 0`, `searchMatchIndex = -1`
   - `semanticResults = []`
   - `clearSearchHighlights()`
   - Reload original messages if had active query

## Todo List

- [x] Replace `bg-gray-900` on header search input with `color-mix()` style
- [x] Update overlay search bar background to matching `color-mix()`
- [x] Add `clearMsgSearch()` to `selectChat()`
- [x] Visual test across all 5 themes

## Success Criteria

- Both search bars visually lighter than header/sidebar background
- Switching chats closes overlay search and clears all state
- No regressions in search or theme rendering

## Risk Assessment

- **Low:** `color-mix()` baseline 2023, 96%+ browser support
- **Low:** `clearMsgSearch()` is battle-tested, adding one call site
- **Note:** `clearMsgSearch()` reloads messages if had active query — in `selectChat()` this is fine since messages get reset anyway (line 5689)
