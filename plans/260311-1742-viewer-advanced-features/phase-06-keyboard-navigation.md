# Phase 06: Keyboard Navigation

## Context

- [Research: Context Menu, Links, Keyboard](../reports/researcher-260311-1742-context-menu-links-keyboard.md)
- Existing keyboard handler: `handleLightboxKeydown` (index.html line 3546) -- Escape, ArrowLeft, ArrowRight for lightbox only
- Message container: `flex flex-col-reverse` (line 1040) -- scroll direction is inverted
- `showScrollToBottom` ref exists for scroll-to-bottom button
- `showDatePickerModal` ref exists for date picker modal
- Context menu from Phase 4 needs Escape integration

## Overview

- **Priority:** MEDIUM
- **Status:** Pending
- **Description:** Global keyboard shortcut system: PageUp/Down for message scroll, Home/End for jump, ArrowUp/Down for chat list, Escape cascade, Ctrl+F for search

## Key Insights

- `flex-col-reverse` inverts scroll: `scrollTop=0` is bottom (newest), negative scrollTop is scrolled up (older)
- PageUp should scroll toward older (negative `top`), PageDown toward newer (positive `top`)
- Must guard against input/textarea focus -- skip shortcuts when typing
- Existing lightbox keydown handler should be replaced by unified global handler
- Escape cascade: lightbox > context menu > date picker > search bar > mobile sidebar

## Requirements

**Functional:**

| Key | Action | Context |
|-----|--------|---------|
| PageUp | Scroll up (older messages) | Message view |
| PageDown | Scroll down (newer messages) | Message view |
| Home | Jump to newest (scrollTop = 0) | Message view |
| End | Jump to oldest loaded | Message view |
| ArrowUp/Down | Navigate chat list | Sidebar (no input focused) |
| Enter | Open focused chat | Sidebar with focused chat |
| Escape | Close topmost overlay | Global (cascade) |
| Ctrl+F | Toggle in-chat search bar | Message view (Phase 8) |

**Non-functional:**
- Skip all shortcuts when `INPUT`, `TEXTAREA`, or `contentEditable` is focused
- Skip when context menu is visible (context menu has its own keyboard nav)

## Related Code Files

**Modify:**
- `src/web/templates/index.html`:
  - JS: add `handleGlobalKeydown()` function replacing `handleLightboxKeydown`
  - JS: add `chatListFocusIndex` ref + `navigateChatList()` function
  - JS: update `onMounted`/`onUnmounted` to use new global handler
  - HTML: add visual focus indicator on chat list items (border or bg change for focused index)
  - CSS: `.chat-focused` class for keyboard-selected chat item

## Implementation Steps

1. **Replace lightbox handler with global handler**:
   - Remove `handleLightboxKeydown` and its `addEventListener`/`removeEventListener`
   - Create `handleGlobalKeydown(e)` that dispatches based on app state
   - Register in `onMounted`, unregister in `onUnmounted`

2. **Input guard**:
   ```js
   const tag = e.target.tagName
   if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) return
   ```

3. **Escape cascade** (check in priority order):
   - `lightboxOpen.value` -> `closeLightbox()`
   - `contextMenu.visible` -> `closeContextMenu()`
   - `showDatePickerModal.value` -> `closeDatePicker()`
   - Future: search bar, mobile sidebar

4. **Lightbox keys** (when `lightboxOpen.value`):
   - ArrowLeft -> `lightboxPrev()`
   - ArrowRight -> `lightboxNext()`
   - Return early after handling

5. **Message scroll** (when `selectedChat.value` and no overlay):
   - PageDown: `container.scrollBy({ top: scrollAmount, behavior: 'smooth' })`
   - PageUp: `container.scrollBy({ top: -scrollAmount, behavior: 'smooth' })`
   - Home: `container.scrollTop = 0` (newest)
   - End: `container.scrollTop = -container.scrollHeight` (oldest loaded)
   - `scrollAmount = container.clientHeight * 0.8`

6. **Chat list navigation**:
   - `chatListFocusIndex = ref(-1)`
   - ArrowDown/ArrowUp: increment/decrement index within `filteredChats` bounds
   - Enter: `selectChat(filteredChats.value[chatListFocusIndex.value])`
   - Scroll focused item into view: `items[idx].scrollIntoView({ block: 'nearest' })`
   - Reset `chatListFocusIndex` when search filter changes

7. **Ctrl+F handler** (stub for Phase 8):
   - `if (e.ctrlKey && e.key === 'f') { e.preventDefault(); toggleSearchBar() }`
   - `toggleSearchBar` is a no-op until Phase 8

8. **Visual focus indicator**:
   - Add conditional class to chat list item: `:class="{ 'chat-focused': chatListFocusIndex === index }"`
   - CSS: `.chat-focused { outline: 2px solid var(--tg-accent); outline-offset: -2px; }`

## Todo

- [ ] Create `handleGlobalKeydown()` function
- [ ] Add input focus guard
- [ ] Implement Escape cascade (lightbox > context menu > date picker)
- [ ] Move lightbox ArrowLeft/Right into global handler
- [ ] Remove old `handleLightboxKeydown` and its event listeners
- [ ] Implement PageUp/PageDown message scrolling (flex-col-reverse aware)
- [ ] Implement Home/End jump to newest/oldest
- [ ] Add `chatListFocusIndex` ref and `navigateChatList()` function
- [ ] Add Enter to select focused chat
- [ ] Add Ctrl+F stub for search bar toggle
- [ ] Add `.chat-focused` CSS class and visual indicator
- [ ] Test: verify no shortcuts fire when typing in search/input fields
- [ ] Test: verify Escape cascade order is correct

## Success Criteria

- PageUp/Down scrolls messages correctly in flex-col-reverse container
- Home jumps to newest, End to oldest loaded
- ArrowUp/Down navigates chat list with visible focus indicator
- Enter opens focused chat
- Escape closes overlays in correct priority order
- No shortcuts fire when input is focused
- Lightbox keyboard nav still works (delegated through global handler)

## Risk Assessment

- **flex-col-reverse scroll math** -- inverted from intuition. Must test PageUp goes up visually (toward older = negative scrollBy top).
- **Mitigation:** Manual test with long chat, verify PageUp scrolls toward older messages
- **Removing lightbox handler** -- must ensure all lightbox keys still work through new global handler
- **Mitigation:** Global handler checks `lightboxOpen.value` first, handles same keys
