## Code Review: Right Dock Edge Cases & Accessibility

### Scope
- File: `src/web/templates/index.html` (~8363 lines)
- Focus: Right Dock (tool strip + canvas panel) — lines 2294-2450, 3647-3654, 6900-6929
- Type: Edge case + accessibility review

### Overall Assessment
The Right Dock implementation is solid structurally. The tool strip correctly uses `hidden md:flex` and the canvas guards on `rightDockOpen && selectedChat`. Several medium-severity edge cases and accessibility gaps found below.

---

### Issues

#### 1. WARN — Canvas panel visible on mobile after resize

**Lines:** 2342-2344

The canvas div has `v-if="rightDockOpen && selectedChat"` but NO responsive guard (`hidden md:flex` or equivalent). If user has dock open on desktop then resizes below 768px:
- Tool strip hides (good, `hidden md:flex`)
- Canvas stays visible — 380px panel overlaps mobile chat view
- User has no way to close it since the strip (with close affordance) is hidden

**Fix:** Add `hidden md:flex` to the canvas div, or add a `window resize` listener that auto-closes the dock below 768px:
```js
// In setup(), after rightDockOpen definition:
window.addEventListener('resize', () => {
    if (window.innerWidth < 768 && rightDockOpen.value) {
        rightDockOpen.value = false
    }
})
```

#### 2. WARN — `sidebarFocusCompact` not reactive to window width

**Lines:** 3651-3654

```js
const sidebarFocusCompact = computed(() =>
    typeof window !== 'undefined' && window.innerWidth >= 768
    && !!selectedChat.value && rightDockOpen.value
)
```

`window.innerWidth` is not a reactive value. The computed only re-evaluates when `selectedChat` or `rightDockOpen` change — NOT on resize. If user resizes from 1200px to 600px while dock is open, the sidebar stays in compact-rail mode even though the dock canvas is now problematic.

**Fix:** Add a reactive `windowWidth` ref updated on resize:
```js
const windowWidth = ref(window.innerWidth)
window.addEventListener('resize', () => { windowWidth.value = window.innerWidth })
const sidebarFocusCompact = computed(() =>
    windowWidth.value >= 768 && !!selectedChat.value && rightDockOpen.value
)
```

#### 3. INFO — Forum chat does not close dock

**Lines:** 6041-6046, 6074-6077

`selectChat` returns early for forum chats (`chat.is_forum`) before reaching the dock hydration code at line 6075. If dock is open showing chat A's data and user clicks a forum chat:
- `selectedChat` is NOT set to the forum chat (stays as chat A or gets set via `openForumTopics`)
- Canvas stays open showing stale chat A data
- This is arguably correct (forum opens topics, not a chat), but confusing visually

**Fix (optional):** Close the dock on forum navigation, or at least clear the canvas:
```js
if (chat.is_forum) {
    rightDockOpen.value = false  // or just add this line
    await openForumTopics(chat)
    return
}
```

#### 4. INFO — `mobileBackStep` nulls `selectedChat` without closing dock

**Line:** 4703

`mobileBackStep` sets `selectedChat.value = null` but does not set `rightDockOpen.value = false`. The canvas has `v-if="rightDockOpen && selectedChat"` so it hides, but `rightDockOpen` remains `true`. Next time user selects a chat, the dock auto-shows (since `rightDockOpen` is still true and `selectChat` calls `hydratePanel` when dock is open — line 6075).

This is a UX surprise: user backed out of chat, picked a new one, and the dock re-opens unexpectedly.

**Fix:** Close dock in `mobileBackStep`:
```js
if (selectedChat.value) {
    selectedChat.value = null
    rightDockOpen.value = false  // add this
    stopMessageRefresh()
    messages.value = []
    return
}
```

#### 5. INFO — Per-chat AI persistence works correctly (reference, not copy)

**Lines:** 6908-6910

```js
if (!aiMessagesByChatId[cid]) aiMessagesByChatId[cid] = []
aiMessages.value = aiMessagesByChatId[cid]
```

This assigns by reference (arrays are objects). Chat A -> B -> A correctly restores A's messages. The `aiMessagesByChatId` is `Vue.reactive({})`, and `aiMessages` is a `ref` pointing to the same array. Pushes to `aiMessages.value` mutate the stored array. This is correct and intentional.

**No issue.** Just confirming the pattern works as designed.

#### 6. WARN — Close buttons lack screen reader labels

**Lines:** 2349, 2379, 2426

Timeline and Media close buttons:
```html
<button @click="rightDockOpen = false" class="text-tg-muted hover:text-white">
    <i class="fas fa-times"></i>
</button>
```

No `aria-label`, no `title`, no `<span class="sr-only">`. Screen readers announce nothing useful — just "button".

The AI panel close button (line 2426) has `title="Close panel"` — better, but still no `aria-label`.

**Fix:** Add `aria-label="Close panel"` to all three close buttons:
```html
<button @click="rightDockOpen = false" aria-label="Close panel" ...>
```

#### 7. INFO — Toolbar lacks keyboard arrow navigation

**Line:** 2295-2339

The tool strip has `role="toolbar" aria-label="Tool strip"` which is correct. However, WAI-ARIA toolbar pattern expects arrow-key navigation between buttons (left/right arrows move focus, Tab exits toolbar). Currently each button is a separate tab stop.

Not blocking, but incomplete for power users and screen reader users.

**Fix (low priority):** Add a `@keydown` handler on the toolbar div that moves focus between child buttons with ArrowUp/ArrowDown (vertical toolbar):
```js
function toolbarKeyNav(e) {
    const btns = [...e.currentTarget.querySelectorAll('button')]
    const i = btns.indexOf(document.activeElement)
    if (e.key === 'ArrowDown') { btns[(i+1) % btns.length]?.focus(); e.preventDefault() }
    if (e.key === 'ArrowUp') { btns[(i-1+btns.length) % btns.length]?.focus(); e.preventDefault() }
}
```

#### 8. INFO — CSS transition classes exist (confirmed)

**Lines:** 752-754

```css
.ai-panel-enter-active { transition: width 0.25s ease, opacity 0.25s ease; }
.ai-panel-leave-active { transition: width 0.2s ease, opacity 0.15s ease; }
.ai-panel-enter-from, .ai-panel-leave-to { width: 0 !important; min-width: 0 !important; opacity: 0; overflow: hidden; }
```

All three required classes present. No issue.

#### 9. INFO — Z-index: no conflict found

Canvas panel has no explicit z-index (flows in normal document order within a flex row). Profile panel is `z-30` with `absolute` positioning. Lightbox is `z-50` with `fixed`. Context menu uses `z-50` with absolute. These are in separate stacking contexts and the dock is not positioned/overlaid, so no conflict.

**No issue.** The dock is inline flex — z-index irrelevant unless it becomes `absolute` or `fixed`.

#### 10. INFO — Focus not managed on canvas open

When canvas opens via `onToolClick`, focus stays on the toolbar button. Screen reader users and keyboard users have no indication that new content appeared to the right. WCAG 2.1 SC 4.1.3 (Status Messages) and general UX best practice suggest moving focus to the panel header or announcing it.

**Fix (low priority):** After `rightDockOpen.value = true`, use `nextTick` to focus the panel's first heading or close button.

---

### Summary Table

| # | Severity | Issue | Line(s) |
|---|----------|-------|---------|
| 1 | **WARN** | Canvas visible on mobile after resize | 2342 |
| 2 | **WARN** | `sidebarFocusCompact` not reactive to resize | 3651 |
| 3 | INFO | Forum chat leaves dock showing stale data | 6043 |
| 4 | INFO | `mobileBackStep` leaves dock open flag set | 4703 |
| 5 | -- | AI persistence by reference: correct | 6908 |
| 6 | **WARN** | Close buttons missing `aria-label` | 2349,2379 |
| 7 | INFO | Toolbar missing arrow-key navigation | 2295 |
| 8 | -- | CSS transition classes: confirmed OK | 752 |
| 9 | -- | Z-index: no conflict | -- |
| 10 | INFO | No focus management on canvas open | 2342 |

### Recommended Actions (priority order)

1. Add responsive guard or resize listener to close dock below 768px (issues 1+2)
2. Add `aria-label="Close panel"` to timeline/media close buttons (issue 6)
3. Close `rightDockOpen` in `mobileBackStep` (issue 4)
4. Optionally close dock on forum chat navigation (issue 3)
5. Arrow-key navigation and focus management are nice-to-have (issues 7, 10)

### Unresolved Questions
- Is there a design intent for the dock to be accessible on mobile at all (e.g., via a bottom sheet)? If so, the `hidden md:flex` guard on the strip is intentional mobile-exclusion, but the canvas should match.
- Should `aiMessagesByChatId` be persisted to localStorage for session survival? Currently cleared on logout only.
