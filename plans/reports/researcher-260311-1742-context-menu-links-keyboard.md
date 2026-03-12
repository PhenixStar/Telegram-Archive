# Research: Context Menu, Permalinks & Keyboard Shortcuts

**Date:** 2026-03-11
**Scope:** Vue 3 CDN (Options/Composition API via `setup()`) + FastAPI backend
**Codebase:** Single `index.html` template with inline Vue 3 app, Tailwind CSS CDN, no build step

---

## Existing Codebase Patterns (Relevant)

- **Vue 3 CDN** loaded from `unpkg.com/vue@3/dist/vue.global.prod.js` -- no SFC, no `<script setup>`, all logic in one `setup()` function
- **Keyboard handling** already exists for lightbox: `document.addEventListener('keydown', handleLightboxKeydown)` with Escape/ArrowLeft/ArrowRight
- **`scrollToMessage(msgId)`** already implemented: finds message in `sortedMessages`, calls `scrollIntoView({ behavior: 'smooth', block: 'center' })`, highlights briefly with blue bg
- **Clipboard** used in admin panel: `navigator.clipboard.writeText(...)` inline in template `@click`
- **Deep-link from push notifications** partially exists: parses `chatId` and `msgId` from notification data, calls `scrollToMessage(msgId)`
- **No context menu** exists anywhere in the codebase
- **No keyboard shortcuts** for general navigation (only lightbox keys)
- **No permalink system** -- no `/chat/:id?msg=:id` route or link-copy UI

---

## Topic 1: Custom Right-Click Context Menu

### Implementation Approach

Since this is a CDN Vue 3 app (no build step, no npm), use a plain reactive component inside the existing `setup()` function. No external library needed.

### Core Pattern

```html
<!-- Menu container (teleported to body-level inside #app) -->
<div v-if="contextMenu.visible"
     :style="{ top: contextMenu.y + 'px', left: contextMenu.x + 'px' }"
     class="fixed z-[9999] min-w-[180px] py-1 rounded-lg shadow-xl border"
     style="background: var(--tg-sidebar); border-color: var(--tg-border);"
     role="menu"
     aria-label="Context menu"
     @click.stop>
  <button v-for="item in contextMenu.items" :key="item.label"
          @click="item.action(); closeContextMenu()"
          role="menuitem"
          class="w-full text-left px-4 py-2 text-sm hover:bg-[var(--tg-hover)] flex items-center gap-2"
          style="color: var(--tg-text);">
    <i :class="item.icon" class="w-4 text-center"></i>
    {{ item.label }}
  </button>
</div>
```

### State Shape

```js
const contextMenu = reactive({
  visible: false,
  x: 0,
  y: 0,
  items: [],    // [{ label, icon, action }]
  target: null  // the message/chat object that was right-clicked
})
```

### Context-Aware Items

Build items dynamically based on what was right-clicked:

| Target | Menu Items |
|--------|-----------|
| **Message (text)** | Copy text, Copy permalink, Reply info, Jump to reply |
| **Message (image)** | Open image, Copy image URL, Download, Copy permalink |
| **Message (link detected)** | Open link, Copy link, Copy permalink |
| **Chat list item** | Open chat, Copy chat ID, Mark as read |
| **Media in lightbox** | Download, Copy URL, Open in new tab |

Detection logic:
```js
const openContextMenu = (e, type, data) => {
  e.preventDefault()
  const items = []
  if (type === 'message') {
    if (data.text) items.push({ label: 'Copy Text', icon: 'fas fa-copy', action: () => copyToClipboard(data.text) })
    items.push({ label: 'Copy Link to Message', icon: 'fas fa-link', action: () => copyPermalink(data) })
    if (data.media) items.push({ label: 'Open Media', icon: 'fas fa-image', action: () => openMedia(data) })
    if (data.reply_to_msg_id) items.push({ label: 'Jump to Reply', icon: 'fas fa-reply', action: () => scrollToMessage(data.reply_to_msg_id) })
  }
  // ... other types
}
```

### Positioning Without Overflow

```js
const positionMenu = (e) => {
  const menuWidth = 200   // approximate min-w
  const menuHeight = contextMenu.items.length * 36 + 8  // item height * count + padding
  let x = e.clientX
  let y = e.clientY
  if (x + menuWidth > window.innerWidth) x = window.innerWidth - menuWidth - 8
  if (y + menuHeight > window.innerHeight) y = window.innerHeight - menuHeight - 8
  if (x < 0) x = 8
  if (y < 0) y = 8
  contextMenu.x = x
  contextMenu.y = y
}
```

### Dismiss Behavior

```js
// Click outside -- attach once on mount
document.addEventListener('click', () => { contextMenu.visible = false })
document.addEventListener('contextmenu', () => { contextMenu.visible = false }) // close before re-open

// Escape key -- integrate into global keydown handler (see Topic 3)
// Scroll -- close on scroll of message container
```

### Accessibility

- Container: `role="menu"`, `aria-label="Context menu"`
- Each item: `role="menuitem"`, `tabindex="-1"`
- On open: focus first item via `nextTick(() => menuEl.querySelector('[role=menuitem]')?.focus())`
- Arrow Up/Down to navigate items, Enter/Space to activate, Escape to close
- Keyboard nav within menu:
```js
const handleMenuKeydown = (e) => {
  const items = [...menuEl.querySelectorAll('[role=menuitem]')]
  const idx = items.indexOf(document.activeElement)
  if (e.key === 'ArrowDown') items[(idx + 1) % items.length]?.focus()
  else if (e.key === 'ArrowUp') items[(idx - 1 + items.length) % items.length]?.focus()
  else if (e.key === 'Escape') closeContextMenu()
}
```

### Template Integration

Add `@contextmenu.prevent="openContextMenu($event, 'message', msg)"` on each `.message-bubble` div (line ~1050 area in index.html). Add `@contextmenu.prevent="openContextMenu($event, 'chat', chat)"` on each sidebar chat item.

---

## Topic 2: Message Permalink / Deep-Link System

### URL Format

```
/chat/{chat_id}?msg={msg_id}
```

Example: `/chat/-1001234567890?msg=4521`

### Frontend: Link Icon on Hover

Add a small link icon that fades in on message hover. Place it at the top-right corner of the message bubble.

```html
<!-- Inside each .message-bubble -->
<button @click.stop="copyPermalink(msg)"
        class="absolute top-1 right-1 opacity-0 group-hover:opacity-70 hover:!opacity-100
               transition-opacity duration-150 p-1 rounded text-xs"
        style="color: var(--tg-muted);"
        title="Copy link to message"
        aria-label="Copy link to this message">
  <i class="fas fa-link text-[10px]"></i>
</button>
```

Requires adding `group` and `relative` classes to `.message-bubble` wrapper.

### Copy to Clipboard

```js
const copyPermalink = async (msg) => {
  const url = `${window.location.origin}/chat/${msg.chat_id}?msg=${msg.id}`
  try {
    await navigator.clipboard.writeText(url)
    showToast('Link copied!')   // reuse or create a simple toast
  } catch {
    // Fallback for non-HTTPS or denied permission
    const ta = document.createElement('textarea')
    ta.value = url
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
    showToast('Link copied!')
  }
}
```

**Note:** `navigator.clipboard.writeText()` requires HTTPS or localhost. Include the `document.execCommand('copy')` fallback.

### Backend Route

Add a FastAPI route that serves the same `index.html` but with query params the Vue app can read on mount:

```python
@app.get("/chat/{chat_id}")
async def chat_permalink(request: Request, chat_id: int, msg: int | None = None):
    """Serve the SPA; Vue reads chat_id and msg from URL on mount."""
    # Access control: verify user session has access to chat_id
    user = await get_current_user(request)
    if not user_can_access_chat(user, chat_id):
        raise HTTPException(403, "Access denied")
    # Serve same index.html -- Vue handles the rest
    return templates.TemplateResponse("index.html", {"request": request, ...})
```

Existing routes in `main.py` already serve `index.html` at `/` with template context. The permalink route mirrors this but Vue app reads URL params on mount.

### Frontend: Auto-Open Chat and Scroll on Mount

In the `setup()` function, after chats are loaded:

```js
onMounted(async () => {
  // ... existing init ...

  // Handle permalink: /chat/{id}?msg={msgId}
  const pathMatch = window.location.pathname.match(/^\/chat\/(-?\d+)$/)
  if (pathMatch) {
    const chatId = parseInt(pathMatch[1])
    const urlParams = new URLSearchParams(window.location.search)
    const msgId = urlParams.get('msg') ? parseInt(urlParams.get('msg')) : null

    // Find chat in loaded list
    const chat = chats.value.find(c => c.id === chatId)
    if (chat) {
      await selectChat(chat)
      if (msgId) {
        // Wait for messages to load, then scroll
        await nextTick()
        scrollToMessage(msgId)
        // If message not found in initial page, use jumpToDate-like logic
        // or add a dedicated /api/chats/{id}/messages/by-id/{msgId} endpoint
      }
    }
    // Clean URL to base path (optional, avoids confusion on reload)
    window.history.replaceState({}, '', '/')
  }
})
```

### Backend: Message-by-ID Endpoint

Add to support jumping to a message that isn't in the initially loaded page:

```python
@app.get("/api/chats/{chat_id}/messages/{msg_id}")
async def get_message_by_id(chat_id: int, msg_id: int, db=Depends(get_db)):
    """Return single message by ID for permalink resolution."""
    msg = await db.get_message(chat_id, msg_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    return msg
```

### Access Control

- Existing auth system uses session cookies + per-user chat whitelists
- The permalink route MUST check `user_can_access_chat(user, chat_id)` before serving
- API endpoint also checks access (existing middleware pattern in `main.py`)
- Unauthenticated users hitting a permalink get redirected to login, then back to the permalink URL after login

### Post-Login Redirect

Store the original permalink in session/localStorage before redirecting to login:
```js
// In login handler, after successful auth:
const redirectTo = localStorage.getItem('post_login_redirect')
if (redirectTo) {
  localStorage.removeItem('post_login_redirect')
  window.location.href = redirectTo
}
```

---

## Topic 3: Keyboard Shortcuts for Scroll Navigation

### Design: Single Global Keydown Handler

Register ONE handler at the app level that dispatches based on current state. Deactivate when any input/textarea/contenteditable is focused.

```js
const handleGlobalKeydown = (e) => {
  // Skip when typing in inputs
  const tag = e.target.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) return

  // Skip if context menu keyboard nav is active
  if (contextMenu.visible) return

  // Lightbox shortcuts (already exist, move here)
  if (lightboxOpen.value) {
    if (e.key === 'Escape') closeLightbox()
    else if (e.key === 'ArrowLeft') lightboxPrev()
    else if (e.key === 'ArrowRight') lightboxNext()
    return
  }

  // Modal/overlay dismiss
  if (e.key === 'Escape') {
    if (showDatePickerModal.value) { closeDatePicker(); return }
    if (contextMenu.visible) { closeContextMenu(); return }
    // Close mobile sidebar, settings panel, etc.
    return
  }

  // Message scroll navigation (only when a chat is selected)
  if (selectedChat.value && messagesContainer.value) {
    const container = messagesContainer.value
    const scrollAmount = container.clientHeight * 0.8  // 80% of visible height

    if (e.key === 'PageDown') {
      e.preventDefault()
      // flex-col-reverse: scrollTop=0 is bottom, negative is scrolled up
      container.scrollBy({ top: scrollAmount, behavior: 'smooth' })
    }
    else if (e.key === 'PageUp') {
      e.preventDefault()
      container.scrollBy({ top: -scrollAmount, behavior: 'smooth' })
    }
    else if (e.key === 'Home') {
      e.preventDefault()
      // Jump to newest (bottom = scrollTop 0 in flex-col-reverse)
      container.scrollTop = 0
      showScrollToBottom.value = false
    }
    else if (e.key === 'End') {
      e.preventDefault()
      // Jump to oldest loaded messages (top = most negative scrollTop)
      container.scrollTop = container.scrollHeight * -1
      // Trigger load-more if needed
    }
  }

  // Chat list navigation (ArrowUp/Down when sidebar visible and no chat input focused)
  if (!selectedChat.value || !e.target.closest('.messages-panel')) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      navigateChatList(e.key === 'ArrowDown' ? 1 : -1)
    }
    if (e.key === 'Enter' && chatListFocusIndex.value >= 0) {
      e.preventDefault()
      selectChat(filteredChats.value[chatListFocusIndex.value])
    }
  }
}
```

### Chat List Arrow Navigation

```js
const chatListFocusIndex = ref(-1)

const navigateChatList = (direction) => {
  const list = filteredChats.value
  if (!list.length) return
  chatListFocusIndex.value = Math.max(0, Math.min(list.length - 1, chatListFocusIndex.value + direction))
  // Scroll the focused item into view in sidebar
  nextTick(() => {
    const items = document.querySelectorAll('.chat-list-item')
    items[chatListFocusIndex.value]?.scrollIntoView({ block: 'nearest' })
  })
}
```

### Registration and Cleanup

```js
onMounted(() => {
  document.addEventListener('keydown', handleGlobalKeydown)
})

onUnmounted(() => {
  document.removeEventListener('keydown', handleGlobalKeydown)
})
```

### Important: `flex-col-reverse` Scroll Model

The message container uses `flex flex-col-reverse`, meaning:
- `scrollTop = 0` = bottom (newest messages visible)
- `scrollTop < 0` = scrolled up toward older messages
- `scrollBy({ top: negative })` = scroll UP (toward older)
- `scrollBy({ top: positive })` = scroll DOWN (toward newer)

This is backwards from typical scroll. PageUp should use negative `top`, PageDown positive `top`.

### Shortcut Reference (for potential UI tooltip/help)

| Key | Action | Context |
|-----|--------|---------|
| PageUp | Scroll up (older msgs) | Message view |
| PageDown | Scroll down (newer msgs) | Message view |
| Home | Jump to newest | Message view |
| End | Jump to oldest loaded | Message view |
| ArrowUp / ArrowDown | Navigate chat list | Sidebar |
| Enter | Open focused chat | Sidebar |
| Escape | Close modal/lightbox/menu | Global |
| ArrowLeft / ArrowRight | Prev/next media | Lightbox |

---

## Implementation Priority

1. **Keyboard shortcuts** -- lowest risk, highest UX impact, no backend changes
2. **Context menu** -- frontend-only, moderate complexity, reuses existing actions
3. **Permalinks** -- requires both backend route + frontend mount logic + access control check

## Key Files to Modify

| File | Changes |
|------|---------|
| `src/web/templates/index.html` | All three features (context menu component, permalink icon, keyboard handler) |
| `src/web/main.py` | Permalink route `/chat/{chat_id}`, message-by-ID API endpoint |

## Unresolved Questions

1. **Should permalink URLs persist in browser history** or be replaced with `/` after navigation completes? Persisting enables browser back/forward but may confuse users if they share the URL bar after navigating away from the linked message.
2. **Chat list arrow nav: should it auto-select (load messages) on hover** or require Enter to confirm? Auto-select could cause excessive API calls.
3. **Context menu on mobile:** long-press (`@touchstart` with 500ms timer) is the mobile equivalent of right-click. Should this be implemented now or deferred?
4. **Should the permalink include the base domain** or be a relative path? `navigator.clipboard` can write either; full URL is more shareable.
