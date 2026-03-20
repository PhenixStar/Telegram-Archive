# Phase 4: OCR Frontend Toggle & Viewer

## Overview
- **Priority:** P1
- **Status:** Planned (depends on Phase 3)
- **Effort:** 3 hours

## Requirements
1. Admin toggle per chat to enable/disable OCR processing
2. Admin toggle per chat to show/hide OCR results to viewers
3. OCR text overlay on media in message view (when visible)
4. OCR progress indicator in admin/profile panel

## UI Components

### 1. Admin OCR Toggle (Profile Sidebar)
In the profile sidebar (Phase 6), add admin-only section:

```html
<!-- Below "Shared Media" button in profile sidebar -->
<div v-if="isAdmin" class="px-4 py-2 space-y-2 border-t" style="border-color: var(--tg-border);">
    <div class="text-xs text-tg-muted mb-1">AI Features</div>

    <!-- OCR Toggle -->
    <div class="flex items-center justify-between px-3 py-2 rounded-lg hover:bg-white/5">
        <div class="flex items-center gap-2 text-sm text-gray-300">
            <i class="fas fa-eye w-5 text-center text-tg-muted"></i>
            <span>OCR Scan</span>
        </div>
        <label class="relative inline-flex items-center cursor-pointer">
            <input type="checkbox" :checked="chatOcrEnabled" @change="toggleChatOcr" class="sr-only peer">
            <div class="w-9 h-5 bg-gray-600 peer-checked:bg-blue-500 rounded-full peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
        </label>
    </div>

    <!-- OCR Visibility Toggle -->
    <div v-if="chatOcrEnabled" class="flex items-center justify-between px-3 py-2 rounded-lg hover:bg-white/5">
        <div class="flex items-center gap-2 text-sm text-gray-300">
            <i class="fas fa-font w-5 text-center text-tg-muted"></i>
            <span>Show OCR Text</span>
        </div>
        <label class="relative inline-flex items-center cursor-pointer">
            <input type="checkbox" :checked="chatOcrVisible" @change="toggleChatOcrVisibility" class="sr-only peer">
            <div class="w-9 h-5 bg-gray-600 peer-checked:bg-green-500 rounded-full ..."></div>
        </label>
    </div>

    <!-- OCR Progress -->
    <div v-if="chatOcrEnabled && ocrProgress" class="px-3 py-1">
        <div class="flex justify-between text-xs text-tg-muted mb-1">
            <span>OCR Progress</span>
            <span>{{ ocrProgress.processed }}/{{ ocrProgress.total }}</span>
        </div>
        <div class="w-full bg-gray-700 rounded-full h-1.5">
            <div class="bg-blue-500 h-1.5 rounded-full" :style="{ width: ocrProgressPercent + '%' }"></div>
        </div>
    </div>
</div>
```

### 2. OCR Text Display on Media Messages
When OCR visibility is ON and a photo message has OCR data, show a small expandable text overlay:

```html
<!-- Inside photo message bubble, below the image -->
<div v-if="chatOcrVisible && msg.ocr_text" class="mt-1 px-2 py-1 rounded text-xs bg-black/30 border border-white/10">
    <div class="flex items-center gap-1 cursor-pointer" @click="msg._ocrExpanded = !msg._ocrExpanded">
        <i class="fas fa-eye text-blue-400" style="font-size: 9px;"></i>
        <span class="text-blue-300">OCR</span>
        <i :class="msg._ocrExpanded ? 'fa-chevron-up' : 'fa-chevron-down'" class="fas text-gray-500" style="font-size: 8px;"></i>
    </div>
    <div v-if="msg._ocrExpanded" class="mt-1 text-gray-300 whitespace-pre-wrap break-words select-all">
        {{ msg.ocr_text }}
    </div>
</div>
```

### 3. OCR Data Loading
- When loading messages, if `chatOcrVisible` is true, fetch OCR data alongside messages
- Backend returns `ocr_text` field in message response when OCR visibility is enabled for that chat
- Lazy: only fetch OCR when user scrolls to photo messages (or batch with message load)

## Vue State
```javascript
const chatOcrEnabled = ref(false)
const chatOcrVisible = ref(false)
const ocrProgress = ref(null)  // { processed: 0, total: 0 }
const ocrProgressPercent = computed(() => ocrProgress.value ?
    Math.round(ocrProgress.value.processed / ocrProgress.value.total * 100) : 0)
```

## API Calls
```javascript
const toggleChatOcr = async () => {
    const res = await fetch(`/api/admin/chats/${selectedChat.value.id}/ocr`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ enabled: !chatOcrEnabled.value }),
        credentials: 'include'
    })
    if (res.ok) chatOcrEnabled.value = !chatOcrEnabled.value
}

const loadOcrStatus = async () => {
    const res = await fetch(`/api/admin/chats/${selectedChat.value.id}/ocr/status`, { credentials: 'include' })
    if (res.ok) {
        const data = await res.json()
        chatOcrEnabled.value = data.enabled
        chatOcrVisible.value = data.visible
        ocrProgress.value = { processed: data.processed, total: data.total }
    }
}
```

## Todo
- [ ] Add OCR toggle section to profile sidebar (admin-only)
- [ ] Add OCR visibility toggle
- [ ] Add OCR progress bar
- [ ] Add OCR text overlay on photo messages
- [ ] Wire API calls for toggle/status
- [ ] Load OCR status when opening profile panel
- [ ] Poll OCR progress while panel is open
