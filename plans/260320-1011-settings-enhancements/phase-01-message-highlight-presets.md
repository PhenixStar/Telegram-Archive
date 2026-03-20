---
phase: 1
title: "Message Highlight Presets"
status: pending
priority: P2
effort: 3h
---

# Phase 1: Message Highlight Presets

## Context
- [Parent plan](plan.md)
- File: `src/web/templates/index.html`
- Settings General tab: lines 2790-2808
- Message rendering: `v-for="msg in sortedMessages"` with `linkifyText` for content

## Overview
Add a "Highlights" subsection to General settings with toggle presets. When active, messages matching preset patterns get a colored left-border or background tint for fast visual scanning.

## Key Insights
- Client-side only — no backend changes. Patterns applied at render time.
- Stored in localStorage for persistence across sessions.
- Each preset has a name, color, and array of regex/keyword patterns.
- Multiple presets can be active simultaneously (messages matching multiple get the first matching preset's color).

## Requirements
- Toggle presets on/off in General settings
- Visual highlight on matching messages (colored left-border, subtle bg tint)
- Preset definitions hardcoded in JS (expandable later via custom presets)

## Architecture

### Preset Definitions
```js
const highlightPresets = [
    {
        id: 'transactions',
        label: 'Transactions',
        icon: 'fa-money-bill-wave',
        color: '#22c55e', // green-500
        patterns: [
            /receipt|invoice|payment|paid|transfer|refund|deposit/i,
            /[+-]\s*\d{1,3}(,?\d{3})*(\.\d{1,2})?/,  // +1,000.00 or -500
            /\$\s*\d+|\d+\s*(USD|EUR|GBP|PHP|SAR|AED)/i,
        ]
    },
    {
        id: 'links',
        label: 'Links & URLs',
        icon: 'fa-link',
        color: '#3b82f6', // blue-500
        patterns: [/https?:\/\/\S+/i]
    },
    {
        id: 'media',
        label: 'Media Messages',
        icon: 'fa-image',
        color: '#a855f7', // purple-500
        patterns: [] // Special: check msg.media !== null
    },
    {
        id: 'mentions',
        label: 'Mentions & Tags',
        icon: 'fa-at',
        color: '#f59e0b', // amber-500
        patterns: [/@\w+/]
    },
    {
        id: 'forwarded',
        label: 'Forwarded Messages',
        icon: 'fa-share',
        color: '#06b6d4', // cyan-500
        patterns: [] // Special: check msg.forward_from
    },
    // Future: custom preset via user-defined regex
]
```

### State
```js
const activeHighlights = ref(
    JSON.parse(localStorage.getItem('tg_highlight_presets') || '[]')
) // array of active preset IDs, e.g. ['transactions', 'links']

const isHighlightActive = (presetId) => activeHighlights.value.includes(presetId)
const toggleHighlight = (presetId) => {
    const idx = activeHighlights.value.indexOf(presetId)
    if (idx >= 0) activeHighlights.value.splice(idx, 1)
    else activeHighlights.value.push(presetId)
    localStorage.setItem('tg_highlight_presets', JSON.stringify(activeHighlights.value))
}

// Returns the highlight color for a message, or null
const getMessageHighlight = (msg) => {
    if (!activeHighlights.value.length) return null
    const text = (msg.text || '') + ' ' + (msg.ocr_text || '')
    for (const pid of activeHighlights.value) {
        const preset = highlightPresets.find(p => p.id === pid)
        if (!preset) continue
        // Special presets (non-regex)
        if (pid === 'media' && msg.media) return preset.color
        if (pid === 'forwarded' && msg.forward_from) return preset.color
        // Regex presets
        if (preset.patterns.some(rx => rx.test(text))) return preset.color
    }
    return null
}
```

### UI in Settings > General
```html
<!-- Highlights subsection -->
<div class="mt-4">
    <h4 class="text-sm font-semibold text-white mb-2">Message Highlights</h4>
    <p class="text-xs text-tg-muted mb-3">Toggle presets to highlight matching messages with colored borders.</p>
    <div class="space-y-2">
        <label v-for="preset in highlightPresets" :key="preset.id"
            class="flex items-center gap-3 px-3 py-2 rounded-lg cursor-pointer hover:bg-white/5">
            <div class="w-3 h-3 rounded-full shrink-0" :style="{ background: preset.color }"></div>
            <i class="fas text-sm text-tg-muted" :class="preset.icon"></i>
            <span class="flex-1 text-sm" style="color: var(--tg-text);">{{ preset.label }}</span>
            <input type="checkbox" class="rounded"
                :checked="isHighlightActive(preset.id)"
                @change="toggleHighlight(preset.id)">
        </label>
    </div>
</div>
```

### Message Rendering
In the message bubble, add conditional left-border:
```html
<div class="message-bubble ..."
    :style="getMessageHighlight(msg)
        ? 'border-left: 3px solid ' + getMessageHighlight(msg) + '; background: ' + getMessageHighlight(msg) + '10;'
        : ''">
```

## Related Code Files
- `index.html:2790-2808` — General settings tab (insert Highlights section after timezone)
- `index.html:1900-2100` (approx) — Message rendering `v-for` loop
- `index.html:7814+` — Return block (expose new vars)

## Implementation Steps
1. Add `highlightPresets` array constant in setup()
2. Add `activeHighlights` ref with localStorage persistence
3. Add `isHighlightActive`, `toggleHighlight`, `getMessageHighlight` functions
4. Insert Highlights UI in General settings tab (after timezone section)
5. Add highlight border/tint to message bubble rendering
6. Expose in return block
7. Test with transactions preset on a chat with payment-like messages

## Todo
- [ ] Define preset array with 5 presets
- [ ] Add state + toggle + match functions
- [ ] Insert settings UI with color dots + toggles
- [ ] Apply highlight style to message bubbles
- [ ] localStorage persistence
- [ ] Expose in return block

## Success Criteria
- Toggling "Transactions" highlights messages with +/- amounts, receipt/payment keywords
- Multiple presets can be active simultaneously
- Highlights persist across page refreshes
- Performance: regex matching on 1000+ messages should be < 50ms

## Risk
- **Regex perf** on large chat histories — mitigate by only checking visible messages (Vue's virtual rendering handles this)
- **False positives** in transactions preset — phone numbers like +1234567890 may match. Mitigate: require space/line-start before +/- numbers
