# Phase 1: Profile Sidebar Bug Fix

## Overview
- **Priority:** P0 (quick fix)
- **Status:** Planned
- **Effort:** 15 minutes

## Bug Description
Profile sidebar shows blank message count when clicking profile picture. Root cause: property name mismatch between API response and template.

## Root Cause
- API `/api/chats/{chat_id}/stats` returns `{ messages: 123, media_files: 45, ... }` (see `adapter.py:707`)
- Profile sidebar template uses `chatStats.message_count` (line 1299) — **does not exist**
- Header bar correctly uses `chatStats.messages` (line 1074)

## Fix
**File:** `src/web/templates/index.html`
**Line 1299:** Change `chatStats.message_count` → `chatStats.messages`

```html
<!-- BEFORE (broken) -->
<div class="text-lg font-bold text-white">{{ chatStats.message_count?.toLocaleString() }}</div>

<!-- AFTER (fixed) -->
<div class="text-lg font-bold text-white">{{ chatStats.messages?.toLocaleString() }}</div>
```

## Todo
- [ ] Fix property name in profile sidebar template
- [ ] Verify in browser: profile panel shows correct message count
