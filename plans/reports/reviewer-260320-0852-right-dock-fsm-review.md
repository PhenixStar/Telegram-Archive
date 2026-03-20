# Code Review: Right Dock FSM (index.html)

**File:** `/home/phenix/projects/tele-private/repo/dev/src/web/templates/index.html`
**Scope:** New right-dock state machine (strip + canvas UI), sidebar compact logic, escape cascade, logout cleanup

---

## Issues

### 1. CRITICAL -- `sidebarFocusCompact` computed is not reactive to window width (line 3651-3654)

```js
const sidebarFocusCompact = computed(() =>
    typeof window !== 'undefined' && window.innerWidth >= 768
    && !!selectedChat.value && rightDockOpen.value
)
```

`window.innerWidth` is a plain DOM property -- Vue cannot track it. This computed will only re-evaluate when `selectedChat` or `rightDockOpen` change, NOT when the browser is resized. If a user opens the dock on desktop then shrinks below 768px, `sidebarFocusCompact` stays `true` and the sidebar remains stuck at 64px.

**Fix:** Add a reactive `windowWidth` ref updated via a `resize` listener (one already exists at line ~6697). Feed that ref into the computed instead of reading `window.innerWidth` directly.

---

### 2. WARN -- `_savedSidebarWidth` declared but never written to (line 3650)

The ref is declared for "compact rail save/restore" but `applySidebarWidth` (line 6666-6673) never saves the current width before compacting, and never restores it when the dock closes. When the dock closes the watcher fires `applySidebarWidth`, which reads `sidebarFocusCompact` (now false) and writes `sidebarWidth.value` -- so restore works by accident via `sidebarWidth`. But `_savedSidebarWidth` is dead code.

**Fix:** Either remove `_savedSidebarWidth` entirely (YAGNI -- `sidebarWidth` already serves as the restore value), or actually use it to snapshot/restore if there is a reason `sidebarWidth` might change while compacted.

---

### 3. WARN -- `hydratePanel('ai')` assigns array reference, not reactive binding (line 6909-6910)

```js
if (!aiMessagesByChatId[cid]) aiMessagesByChatId[cid] = []
aiMessages.value = aiMessagesByChatId[cid]
```

`aiMessages` is a `ref`. Setting `.value` to a plain array from `reactive({})` means `aiMessages.value` points to that array object. Pushes to `aiMessages.value` will mutate the shared array (correct), BUT Vue's reactivity on `aiMessages` triggers only on `.value` replacement, not on mutations to the array it points to. If any template or watcher uses `aiMessages.value.length` to react to new messages, it will NOT update when items are pushed via the shared reference.

**Fix:** Either (a) make `aiMessagesByChatId[cid]` a `ref([])` and use `aiMessages.value = aiMessagesByChatId[cid].value`, or (b) ensure all push operations go through `aiMessages.value = [...aiMessages.value, newMsg]` to trigger reactivity, or (c) use `Vue.shallowRef` and manually trigger after push.

---

### 4. WARN -- `onToolClick` does not guard against null `selectedChat` (line 6913-6929)

`hydratePanel` guards with `if (!selectedChat.value) return` (line 6901), but `onToolClick` still sets `rightDockOpen.value = true` and `rightDockMode.value = mode` before calling `hydratePanel`. The dock opens visually with no chat selected, showing an empty canvas. The template at line 2343 has `v-if="rightDockOpen && selectedChat"` which prevents rendering, but the strip buttons still highlight (lines 2300-2333 use `rightDockOpen && rightDockMode === '...'`), creating a confusing UI state: button highlighted, no panel visible.

**Fix:** Add `if (!selectedChat.value) return` at the top of `onToolClick`.

---

### 5. WARN -- Escape cascade has dead branch for `showAiPanel` (line 7249)

```js
if (rightDockOpen.value) { rightDockOpen.value = false; return }
if (showAiPanel.value) { showAiPanel.value = false; return }
```

`onToolClick` always sets `showAiPanel.value = false` (line 6927). If AI is accessed through the dock (the new path), `showAiPanel` is always false when the dock is open. The `showAiPanel` branch after the dock branch is only reachable if AI was opened via some legacy path that does not go through `onToolClick`. If that legacy path no longer exists, this is dead code. If it does exist, the order is correct (dock first, then legacy flag).

**Fix:** Verify whether any code path still sets `showAiPanel.value = true` outside of `onToolClick`. If not, remove the dead branch.

---

### 6. INFO -- `openProfile` closes dock but does not reset mode (line 6835)

```js
rightDockOpen.value = false  // Close dock canvas to avoid clutter
```

Mode stays at whatever it was. Next time user clicks a strip button, `onToolClick` sees `rightDockOpen === false` and opens with the clicked mode -- so this is harmless. No fix needed unless you want a clean slate.

---

### 7. INFO -- `performLogout` reactive cleanup is correct (line 5869-5870)

```js
Object.keys(aiMessagesByChatId).forEach(k => delete aiMessagesByChatId[k])
aiMessages.value = []
```

Deleting keys from `Vue.reactive({})` is properly tracked by Vue 3's Proxy. Order is fine: clear the map first, then reset the ref. No issue.

---

### 8. INFO -- `selectChat` hydration uses `nextTick` correctly (line 6075-6077)

```js
if (rightDockOpen.value) {
    nextTick(() => hydratePanel(rightDockMode.value))
}
```

`nextTick` ensures DOM updates from the chat switch (message list swap, etc.) complete before hydration triggers data loads. This is correct. The dock stays open across chat switches, and `hydratePanel` re-fetches media/timeline/AI thread for the new chat. Per-chat AI swap happens inside `hydratePanel('ai')` which reads the new `selectedChat.value.id`.

---

### 9. INFO -- Return block exposes all new vars (lines 8153-8158)

`rightDockOpen`, `rightDockMode`, `aiMessagesByChatId`, `sidebarFocusCompact`, `onToolClick`, `hydratePanel` -- all present. `_savedSidebarWidth` is not exposed, which is correct (internal only). No missing exports.

---

### 10. INFO -- Rapid click race condition in `onToolClick` (line 6913-6929)

Timeline -> Media -> Timeline rapid sequence: first click opens dock + sets mode to timeline + hydrates. Second click sees `rightDockMode !== 'media'`, switches mode + hydrates media. Third click sees `rightDockMode !== 'timeline'`, switches + hydrates. All synchronous state transitions, no async in `onToolClick` itself. `hydratePanel` triggers async loads (`loadMediaPanel`, `loadTimeline`) but those are fire-and-forget with their own loading guards. No race condition.

---

## Summary

| Severity | Count | Key Items |
|----------|-------|-----------|
| CRITICAL | 1 | `sidebarFocusCompact` not reactive to window resize |
| WARN | 3 | Dead `_savedSidebarWidth` ref; AI array reactivity gap; null-chat dock open |
| INFO | 4 | Escape dead branch, profile mode reset, logout cleanup OK, return block OK |

### Action Items

- [ ] Fix `sidebarFocusCompact` to use a reactive `windowWidth` ref
- [ ] Guard `onToolClick` against null `selectedChat`
- [ ] Verify `aiMessages` reactivity when items are pushed via shared array reference
- [ ] Remove `_savedSidebarWidth` if unused, or wire it up
- [ ] Audit whether `showAiPanel` is still set outside `onToolClick`
