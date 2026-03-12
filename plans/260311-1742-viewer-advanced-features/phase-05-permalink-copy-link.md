# Phase 05: Message Permalink & Copy Link

## Context

- [Research: Context Menu, Links, Keyboard](../reports/researcher-260311-1742-context-menu-links-keyboard.md)
- `scrollToMessage(msgId)` exists (index.html line 3566) -- scrolls + highlights blue briefly
- Deep-link from push notifications partially exists: parses `chatId`/`msgId` from notification data
- No `/chat/{chat_id}` route in backend
- Access control: session cookies + per-user chat whitelists via `ViewerAccount.allowed_chat_ids`
- `jumpToDate()` (line 3619) has pattern for fetching messages at a specific point and scrolling

## Overview

- **Priority:** HIGH
- **Status:** Pending
- **Description:** Hover-to-show link icon on messages, copy permalink URL, backend route to serve permalink, frontend auto-navigate on mount

## Key Insights

- URL format: `/chat/{chat_id}?msg={msg_id}`
- Backend must serve same `index.html` at `/chat/{chat_id}` with access control
- Frontend reads URL on mount, auto-selects chat, fetches message, scrolls to it
- If target message not in initial page, need single-message fetch API + scroll logic
- `scrollToMessage()` only works if message is in `sortedMessages` -- need to handle "not loaded yet" case
- Post-login redirect: store permalink in `localStorage` before redirect to login page

## Requirements

**Functional:**
- Hover on message shows fade-in link icon (top-right corner of bubble)
- Click link icon copies permalink to clipboard, shows toast
- Backend: `GET /chat/{chat_id}` serves `index.html` with access control check
- Backend: `GET /api/chats/{chat_id}/messages/{msg_id}` returns single message
- Frontend: on mount, detect permalink URL, auto-select chat, scroll to message
- If user not logged in, redirect to login, then back to permalink after auth
- Permalink respects access control (viewer can only link to allowed chats)

**Non-functional:**
- Link icon must not interfere with message text selection
- Permalink URL should use full domain (more shareable)

## Architecture

```
User hovers message -> link icon fades in (top-right)
  -> Click copies: {origin}/chat/{chat_id}?msg={msg_id}
  -> Toast: "Link copied!"

Permalink visit:
  GET /chat/{chat_id}?msg=123
    -> Backend checks auth + chat access
    -> Serves index.html (same template context)
    -> Vue mount: parse URL, find chat, selectChat(), fetch message page, scrollToMessage()
```

## Related Code Files

**Modify:**
- `src/web/templates/index.html`:
  - HTML: add link icon button inside `.message-bubble` with hover opacity transition
  - HTML: add `group` and `relative` classes to `.message-bubble` wrapper
  - JS: add `copyPermalink(msg)` function
  - JS: add permalink detection in `onMounted` (after chats load)
  - JS: enhance `scrollToMessage()` to handle "message not loaded" case
- `src/web/main.py`:
  - Add `GET /chat/{chat_id}` route serving `index.html` with access control
  - Add `GET /api/chats/{chat_id}/messages/{msg_id}` API endpoint
- `src/db/adapter.py`:
  - Add `get_message_by_id(chat_id, msg_id)` method

## Implementation Steps

1. **Link icon in message bubble** (HTML):
   - Add `group relative` classes to `.message-bubble` div
   - Inside bubble, add absolute-positioned button:
     ```html
     <button @click.stop="copyPermalink(msg)"
       class="absolute top-1 right-1 opacity-0 group-hover:opacity-60 hover:!opacity-100
              transition-opacity p-1 rounded text-xs"
       style="color: var(--tg-muted);"
       title="Copy link to message">
       <i class="fas fa-link text-[10px]"></i>
     </button>
     ```

2. **`copyPermalink(msg)`** (JS):
   - Build URL: `${window.location.origin}/chat/${msg.chat_id}?msg=${msg.id}`
   - Use `copyToClipboard()` from Phase 4 (or inline `navigator.clipboard.writeText`)
   - Call `showToast('Link copied!')`

3. **Backend permalink route** (`main.py`):
   - `@app.get("/chat/{chat_id}")` -- same auth check as `/` route
   - **[RED TEAM]** `chat_id` MUST be typed as `int` in path param to prevent collision with other routes (e.g. `/chat/settings`, `/chat/search`). FastAPI will 422 non-integer paths automatically.
   - **[RED TEAM]** Check access BEFORE existence -- return identical 403 for both "not found" and "forbidden" to prevent chat ID enumeration
   - Return `TemplateResponse("index.html", same_context)` if authorized
   - If not authenticated: redirect to `/?redirect=/chat/{chat_id}?msg={msg_id}`

4. **Backend messages-around-target API** (`main.py`):
   - **[RED TEAM]** Instead of single-message fetch, return a PAGE of messages centered on the target:
   - `@app.get("/api/chats/{chat_id}/messages/{msg_id}/context")` -- returns ~50 messages around target (25 before, target, 24 after)
   - Auth check + chat access check (uniform 403 for not-found/forbidden)
   - Eliminates complex frontend multi-step dance (fetch single → get date → jumpToDate → wait → scroll)
   - Rate limit: 5 req/min per user to prevent enumeration via sequential IDs

5. **DB method** (`adapter.py`):
   - `get_messages_around(chat_id, msg_id, count=50)` -- fetch target message's date, then query `count/2` messages before and after, same joins as `get_messages_paginated`

6. **Frontend permalink detection** (JS in `onMounted`):
   - After chats loaded, check `window.location.pathname` for `/chat/{id}` pattern
   - Parse `chat_id` and `msg` query param
   - Find chat in `chats.value`, call `selectChat(chat)`
   - **[RED TEAM]** Call `/api/chats/{chat_id}/messages/{msg_id}/context` to get page centered on target
   - Replace current messages with returned page, then `await nextTick()`, then `scrollToMessage(msgId)`
   - If target not in response (deleted): show toast "Message not found"
   - After navigation completes: `history.replaceState({}, '', '/')` to clean URL

7. **Post-login redirect**:
   - On login page, check `redirect` query param
   - **[RED TEAM]** Validate redirect: must start with `/` and must NOT contain `//` or protocol scheme. Use: `if (!redirect.startsWith('/') || redirect.includes('//')) redirect = '/'`
   - After successful login, `window.location.href = redirect`

## Todo

- [ ] Add `group relative` classes to `.message-bubble` div
- [ ] Add hover link icon button inside message bubble
- [ ] Implement `copyPermalink(msg)` function
- [ ] Add `GET /chat/{chat_id}` backend route with access control
- [ ] Add `GET /api/chats/{chat_id}/messages/{msg_id}/context` API (page around target)
- [ ] Add `get_messages_around()` in adapter.py
- [ ] **[RED TEAM]** Return uniform 403 for both not-found and forbidden chats
- [ ] Add permalink detection in `onMounted` (URL parsing)
- [ ] **[RED TEAM]** Validate redirect param: `/`-prefix only, no `//` or protocol scheme
- [ ] Add post-login redirect for unauthenticated permalink visits
- [ ] Rate limit context endpoint (5 req/min per user)
- [ ] Test permalink with message in middle of chat history
- [ ] Test permalink with unauthorized chat (should 403)
- [ ] Test permalink with non-existent chat (should also 403, not 404)
- [ ] Test permalink when not logged in (should redirect to login, then back)
- [ ] **[RED TEAM]** Type `chat_id` as `int` in path param to prevent route collision
- [ ] Test redirect validation rejects `https://evil.com`

## Success Criteria

- Hovering a message shows link icon that fades in
- Clicking link icon copies full URL to clipboard and shows toast
- Visiting permalink URL auto-opens correct chat and scrolls to message
- Unauthenticated users redirected to login, then back to permalink
- Unauthorized chat access returns 403
- URL cleaned after navigation completes

## Risk Assessment

- **[RED TEAM RESOLVED]** "Message not loaded" edge case: backend returns messages-around-target page, eliminating frontend multi-step async dance
- **URL cleanup via `replaceState`** -- prevents browser back from re-triggering permalink logic. Acceptable UX.
- **[RED TEAM RESOLVED]** Open redirect: redirect param validated to `/`-prefix only

## Security Considerations

- Permalink route MUST check auth + chat access before serving content
- **[RED TEAM]** Check access BEFORE existence -- identical 403 for not-found and forbidden (prevents chat ID enumeration)
- **[RED TEAM]** Validate redirect parameter: reject any value not starting with `/` or containing `//`
- Rate limit single-message context endpoint to prevent bulk enumeration (use `slowapi` library)
