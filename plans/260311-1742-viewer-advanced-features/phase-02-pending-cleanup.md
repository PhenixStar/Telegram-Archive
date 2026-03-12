# Phase 02: Pending Cleanup Tasks

## Context

- [index.html](../../src/web/templates/index.html) -- lines 394-398 define `.msg-date-group` CSS but it is never applied in template
- Previous plan: "Viewer UX & Performance Enhancements" left this pending

## Overview

- **Priority:** LOW
- **Status:** Pending
- **Description:** Apply `.msg-date-group` CSS class to date separator wrapper elements for `content-visibility` optimization

## Key Insights

- `.msg-date-group` CSS (line 395-398) defines `content-visibility: auto` and `contain-intrinsic-size: auto 300px`
- This enables browser-level virtualization of off-screen date groups -- significant perf win for long message lists
- Messages are rendered via `v-for="(msg, index) in sortedMessages"` with inline date separators between messages
- Currently date separators are inserted by `shouldShowDateSeparator(index)` computed check with no wrapping group element

## Requirements

**Functional:**
- Wrap consecutive messages sharing the same date into a container with class `msg-date-group`
- Date separator label should be the first child inside each group

**Non-functional:**
- Must not break `flex-col-reverse` scroll behavior
- Must not break `scrollToMessage()` functionality
- Must not break infinite scroll / load-more triggers

## Related Code Files

**Modify:**
- `src/web/templates/index.html` -- template section around line 1045-1055 (message loop), and the `shouldShowDateSeparator` helper

## Implementation Steps

1. Read the message rendering loop (line ~1045) and `shouldShowDateSeparator` logic
2. Wrap the `v-for` output in date-grouped `<div class="msg-date-group">` containers
3. Two approaches:
   - **Option A (simpler):** Add `msg-date-group` class to the existing date separator `<div>` element and its sibling messages using a computed `groupedMessages` array
   - **Option B (minimal change):** Apply `.msg-date-group` to each message + separator pair using a wrapper div
4. Option B is preferred -- less refactoring, lower risk of breaking flex-col-reverse
5. Test: scroll behavior, load-more trigger, date separator visibility, `scrollToMessage` still works

## Todo

- [ ] Wrap message + date separator groups in `.msg-date-group` divs
- [ ] Verify flex-col-reverse scroll still works
- [ ] Verify infinite scroll load-more still triggers
- [ ] Verify `scrollToMessage()` still highlights correctly

## Success Criteria

- `.msg-date-group` class appears in rendered DOM on date group wrappers
- Chrome DevTools shows `content-visibility: auto` applied
- No regression in scroll behavior or message loading

## Risk Assessment

- **Medium risk** -- modifying the message rendering loop affects core UX
- **Mitigation:** Test with long chat (1000+ messages) to verify no scroll glitches
- **Mitigation:** Keep change minimal -- wrapper div only, no logic changes
