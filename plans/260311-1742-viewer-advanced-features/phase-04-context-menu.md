# Phase 04: Custom Right-Click Context Menu

## Context

- [Research: Context Menu, Links, Keyboard](../reports/researcher-260311-1742-context-menu-links-keyboard.md)
- No context menu exists in codebase
- Clipboard API used inline in admin panel: `navigator.clipboard.writeText()`
- `escapeHtml()` and `linkifyText()` exist (index.html line 3553-3563)
- Toast component from Phase 3 available

## Overview

- **Priority:** HIGH
- **Status:** Pending
- **Description:** Right-click context menu on messages, chat items, and images with context-aware menu items

## Key Insights

- Vue 3 CDN app -- no SFC, all in `setup()`. Menu component is reactive state + template div.
- `.message-bubble` is the target element (line 1056)
- Sidebar chat items are in the chat list loop
- `scrollToMessage()` already exists for "Jump to reply"
- `navigator.clipboard.writeText()` needs HTTPS or localhost; include `execCommand('copy')` fallback
- Must integrate with lightbox (context menu on lightbox images)
- Mobile long-press deferred to future

## Requirements

**Functional:**
- `@contextmenu.prevent` on: message bubbles, chat list items, lightbox images
- Context-aware menu items based on target:

| Target | Items |
|--------|-------|
| Text message | Copy Text, Copy Link to Message |
| Message with reply | + Jump to Reply |
| Message with media | + Open Media, Download |
| Chat list item | Open Chat, Copy Chat ID |
| Lightbox image/video | Download, Copy URL, Open in New Tab |

- Viewport-clamped positioning (no overflow off-screen)
- Dismiss on: click outside, Escape key, scroll, another right-click

**Non-functional:**
- Accessibility: `role="menu"`, `role="menuitem"`, keyboard nav within menu (Arrow keys, Enter, Escape)
- Theme-aware: use CSS custom properties for colors
- Z-index above everything except lightbox: `z-[9999]`

## Architecture

```
State:
  contextMenu = reactive({ visible, x, y, items[], target })

Flow:
  @contextmenu.prevent="openContextMenu($event, 'message', msg)"
    -> build items[] based on type + data
    -> positionMenu(event) with viewport clamping
    -> contextMenu.visible = true

Dismiss:
  document.addEventListener('click', close)
  document.addEventListener('contextmenu', close-then-reopen)
  Escape key (via global keydown handler from Phase 6, or standalone)
```

## Related Code Files

**Modify:**
- `src/web/templates/index.html`:
  - JS: add `contextMenu` reactive state, `openContextMenu()`, `closeContextMenu()`, `positionMenu()`, `handleMenuKeydown()`, `copyToClipboard()` helper
  - HTML: add context menu div (fixed positioned), add `@contextmenu.prevent` to message bubbles, chat items, lightbox
  - CSS: menu styling using CSS custom properties

## Implementation Steps

1. **State & helpers** (JS in `setup()`):
   - `const contextMenu = reactive({ visible: false, x: 0, y: 0, items: [], target: null })`
   - `copyToClipboard(text)` -- uses `navigator.clipboard.writeText()` with `execCommand` fallback, calls `showToast()`
   - `openContextMenu(event, type, data)` -- **[RED TEAM]** first check `if (event.shiftKey) return` to allow browser default; then builds items array, calls `positionMenu()`, sets visible
   - `closeContextMenu()` -- sets `contextMenu.visible = false`
   - `positionMenu(event)` -- calculates x/y with viewport clamping

2. **Menu item builders** (in `openContextMenu`):
   ```
   if type === 'message':
     if data.text: push Copy Text
     push Copy Link to Message (calls copyPermalink from Phase 5; until then, copies msg ID)
     if data.reply_to_msg_id: push Jump to Reply
     if data.media_items?.length: push Open Media, push Download
   if type === 'chat':
     push Open Chat
     push Copy Chat ID
   if type === 'lightbox':
     push Download
     push Copy URL
     push Open in New Tab
   ```

3. **Template** -- add before closing `</div>` of `#app`:
   - Fixed div with `v-if="contextMenu.visible"`, styled with CSS custom properties
   - `v-for="item in contextMenu.items"` rendering button elements
   - `@keydown` handler for arrow nav within menu

4. **Event binding** -- add `@contextmenu.prevent` to:
   - `.message-bubble` div (line ~1056): `@contextmenu.prevent="openContextMenu($event, 'message', msg)"`
   - Chat list item divs: `@contextmenu.prevent="openContextMenu($event, 'chat', chat)"`
   - Lightbox overlay image: `@contextmenu.prevent="openContextMenu($event, 'lightbox', lightboxMedia)"`

5. **Dismiss handlers** -- **[RED TEAM]** register scroll listener ONLY when menu opens, remove on close (avoids firing on every scroll event globally):
   - `document.addEventListener('click', closeContextMenu)` -- always registered (cheap)
   - In `openContextMenu()`: `document.addEventListener('scroll', closeContextMenu, true)`
   - In `closeContextMenu()`: `document.removeEventListener('scroll', closeContextMenu, true)`

6. **Accessibility**:
   - Menu container: `role="menu"`, `aria-label="Context menu"`
   - Each item: `role="menuitem"`, `tabindex="-1"`
   - On open: `nextTick(() => firstItem.focus())`
   - ArrowDown/ArrowUp to navigate, Enter to activate, Escape to close

## Todo

- [ ] Add `contextMenu` reactive state
- [ ] Implement `copyToClipboard()` with fallback + toast
- [ ] Implement `openContextMenu()` with type-based item builder
- [ ] Implement `positionMenu()` with viewport clamping
- [ ] Implement `closeContextMenu()`
- [ ] Add menu template with `role="menu"` accessibility
- [ ] Add `@contextmenu.prevent` to message bubbles
- [ ] Add `@contextmenu.prevent` to chat list items
- [ ] Add `@contextmenu.prevent` to lightbox media
- [ ] Add click-outside and scroll dismiss handlers
- [ ] Add keyboard navigation within menu (ArrowUp/Down, Enter, Escape)
- [ ] Test menu positioning at all 4 screen corners
- [ ] **[RED TEAM]** Add `if (event.shiftKey) return` in `openContextMenu` for browser default escape hatch
- [ ] **[RED TEAM]** Register scroll listener only when menu opens, remove on close (avoid global scroll listener leak)
- [ ] Test with keyboard-only navigation

## Success Criteria

- Right-click on message shows context-appropriate menu
- Right-click on chat item shows chat-specific menu
- Menu never overflows viewport
- Escape closes menu
- Keyboard navigation works within menu
- Copy actions show toast confirmation

## Risk Assessment

- **[RED TEAM RESOLVED]** Browser default context menu: Shift+right-click passes through to browser default (for DevTools access). Implemented via `if (event.shiftKey) return` in `openContextMenu`.
- **Mobile:** long-press not implemented yet. Mobile users won't see context menu until long-press is added. Acceptable for now -- mobile has native long-press behavior for copy.
