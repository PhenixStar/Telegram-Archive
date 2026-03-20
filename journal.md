# Telegram Archive - Project Journal

## Overview

Telegram Archive: automated backup with Docker + web viewer.
Stack: Python FastAPI + Vue 3 CDN (single `index.html`) + SQLite/PostgreSQL + Tailwind CSS

## Completed Features

### v7.2.0 (2026-03-10)
- Share tokens with scoped chat access
- Download restrictions (no_download flag)
- On-demand WebP thumbnails with disk cache
- App settings key-value table
- Token management UI + login UI
- Session persistence across container restarts

### Viewer UX & Preferences (2026-03-10)
- 6 dark themes via CSS custom properties (midnight, telegram-classic, amoled-black, nord, monokai, solarized-dark)
- Theme IIFE pre-paint flash prevention
- Lazy loading images + LQIP blur-up
- Album grid layout (2-4 items)
- Lightbox with keyboard nav (Arrow, Escape)
- Date picker (Flatpickr) + jump-to-date
- Scroll-to-bottom button
- Infinite scroll with cursor-based pagination
- Push notifications (basic + Web Push via VAPID)
- WebSocket real-time message sync
- Admin panel: viewers, audit logs, tokens
- Folder navigation, forum topics, archived chats
- Pinned messages viewer
- Chat search (client-side filter)
- Message search (ILIKE backend)
- Real-time listener auto-activation

### Gap-Fill Backup Feature (2026-03-10)
- Backup gap detection and fill

### Viewer Advanced Features (2026-03-11)
- [x] Phase 1: Project rules & journal (CLAUDE.md, journal.md, .gitignore)
- [x] Phase 2: Pending cleanup (msg-date-group class, content-visibility)
- [x] Phase 3: Settings panel (toast notifications, timezone selector)
- [x] Phase 4: Context menu (message, chat, lightbox; Shift+right-click passthrough)
- [x] Phase 5: Permalink & copy link (/chat/{id}?msg={id} URL, context API, get_messages_around)
- [x] Phase 6: Keyboard navigation (global handler, Escape cascade, Ctrl+F, PageUp/Down, chat list arrows)
- [x] Phase 7: Light theme (light-default + auto system detect, color-scheme toggle)
- [x] Phase 8: FTS5 indexing (contentless FTS5, background worker, /api/search, query sanitizer)
- [x] Phase 9: Enhanced search UI (sticky search bar, DOM TreeWalker highlight, match navigation)
- [x] Phase 10: AI panel stub ("Coming Soon" tooltip)

### UX Bug Fixes (2026-03-12)
- [x] Fix: Permalink scroll centering in flex-col-reverse (manual getBoundingClientRect + instant/smooth threshold)
- [x] Fix: DOM index mismatch — added data-msg-id attribute, querySelector by attribute instead of index
- [x] Fix: Advanced search filter UX — date fields inline, confirm button, no auto-trigger on @change

### UX/UI Improvements v2 (2026-03-12)
Plan: `plans/260312-2228-ux-ui-improvements-v2/`

| # | Phase | Effort | Status |
|---|-------|--------|--------|
| 1 | Floating date badge (scroll-aware sticky pill) | 2h | Done |
| 2 | Enhanced context menus (6 new types + mobile long-press) | 3h | Done |
| 3 | Message grouping (consecutive sender bubble merging) | 3h | Done |
| 4 | Search keyword highlighting (header search → message text) | 1.5h | Done |
| 5 | Media grid panel (right-side panel, tabbed, backend endpoints) | 4h | Done |
| 6 | Profile sidebar (chat/user info, members, backend endpoints) | 4h | Done |
| 7 | Timeline scrubber / heatmap (message density, backend endpoints) | 4h | Done |
| 8 | Visual polish bundle (animations, skeletons, transitions) | 3h | Done |

All 8 phases implemented (2026-03-12)

### Bidirectional Loading + Advanced Search (2026-03-12)
Plan: `plans/260312-1948-bidirectional-message-loading-advanced-search/`
- [x] Backend: forward pagination (after_date/after_id)
- [x] Frontend: bidirectional scroll from reference point
- [x] Advanced search: date range filtering
- [x] Permalink navigation fix

### UX Fixes + AI Config + OCR Worker (2026-03-13)
Commit: `ab638bf`
- [x] AI configuration panel (model URLs, settings via app_settings)
- [x] Background OCR worker (batch processing, progress tracking)
- [x] Various UX bug fixes

### Zero-Cost MVP Improvements (2026-03-13)
Plan: `plans/260313-2127-zero-cost-mvp-improvements/`

| # | Feature | Status |
|---|---------|--------|
| 1 | Resizable sidebar width | Done |
| 2 | Jump to first/last message | Done |
| 3 | Message count in search results | Done |
| 4 | CSV export | Done |
| 5 | Media gallery grid toggle | Done |
| 6 | Link preview cards from raw_data | Done |
| 7 | Offline mode (service worker cache) | Done |
| 8 | Smart date grouping in search | Done |
| 9 | PWA install prompt | Done |
| 10 | Semantic search (Ollama embeddings) | Done |
| 11 | Smart highlights (regex tags) | Done |

All 11 features complete. Semantic search uses qwen3-embedding:8b via Ollama.

### Semantic Search Bug Fixes (2026-03-14)
Commit: `bbe4d66`
- [x] Fix result click for unloaded messages (use targetMsgId navigation)
- [x] Fix embedding config fallback (env vars ignored, seeded wrong URL)
- [x] Fix key collision in embed response (batch_stored vs embedded)
- [x] Fix early "all embedded" response missing counts

### OCR Model Benchmark (2026-03-14)
Tested 3 vision models for OCR performance:

| Model | Size | Avg/img | Success | Notes |
|-------|------|---------|---------|-------|
| gemma3:27b (Ollama) | 17GB | **5.1s** | 20/20 | Best overall — fast, accurate, multilingual |
| GLM-OCR (Flask) | 0.9B | 60.7s | 20/20 | Reliable but 12x slower |
| qwen3-vl-30b (Ollama) | 18GB | N/A | 0/20 | Conv3D crash on V100 (compute 7.0 bug) |

**Decision:** gemma3:27b as primary OCR, GLM-OCR as fallback.
GPU layout: 4x V100-DGXS-32GB (128GB total). gemma3 on GPU0, GLM-OCR on GPU0 (3.6GB).

## Current Sprint

### Telegram Native UI Redesign (2026-03-14)
Full visual overhaul to match Telegram Web K/A aesthetics.

| # | Phase | Status |
|---|-------|--------|
| 1 | Color System — 9 themes, new CSS vars (--tg-primary, --tg-green, --tg-link, --tg-chat-hover, --tg-chat-active, --tg-msg-service) | Done |
| 2 | Chat List — peer-colored avatars, theme-aware borders, accent-colored unread dots | Done |
| 3 | Message Bubbles — purple own-msg (#8774e1), 12px radius, Telegram-style corner grouping, inline timestamps | Done |
| 4 | Header/Toolbar — flat header (no shadow), theme borders, peer-colored header avatar | Done |
| 5 | Typography — system font stack (-apple-system, BlinkMacSystemFont, ...), date separator pill badges | Done |
| 6 | Peer Colors — 21-color Telegram palette, replaces HSL hash; used in avatars, sender names, mini-avatars | Done |

Key decisions:
- Default theme "Telegram Dark": bg #181818, sidebar #0f0f0f, accent #8774e1
- Old blue accent preserved as "Classic Blue" theme
- Added "Light Blue" theme for classic blue in light mode
- Inline timestamps float inside bubble text (Telegram-native style)
- All `border-gray-700` replaced with `border-[color:var(--tg-border)]`
- Login page updated to purple gradient (#8774e1)

### UX Fixes Batch (2026-03-14)
All 5 fixes from plan `steady-inventing-orbit` verified as implemented:

| # | Fix | Status |
|---|-----|--------|
| 1 | Share Token — chat list loads on tab click (loadAdminChats added) | Done |
| 2 | Share Token — copy link button on existing tokens | Done |
| 3 | Profile — avatar click opens profile sidebar | Done |
| 4 | Global Search — cross-chat message content search via /api/search | Done |
| 5 | Number-normalized search (1,000.00 matches 1000) | Done |

### Session: 2026-03-20 — Right Dock + Compact Rail + Settings Enhancements

**Implemented (frontend):**
- Right dock: 48px tool strip + 380px expandable canvas (Timeline/Media/AI) with FSM
- Per-chat AI threads keyed by chat ID, cleared on logout
- Sidebar compact rail: logo with role ring, avatar-only chat list, v-show for perf
- Message highlight presets: Transactions/Links/Media/Mentions/Forwarded toggles
- Backup tab: merged Stats into Backup tab, removed Stats tab, preset grid replaces raw cron
- Listener tab: added env var documentation for configuration
- Token chat search: filter input above chat checkboxes
- Sidebar default width: fixed 300px (was viewport-based, could be 600px)
- models.py: added `from __future__ import annotations` to fix forward reference errors

**Reviewed by:** Claude Opus (3 reviewers), Codex GPT-5.4 (3 reviewers), Gemini 2.5 Pro (1 reviewer)

## Deferred Backend Work

Items that need backend changes (Python/Docker) before frontend can be fully wired:

| # | Item | Why Deferred | What's Needed |
|---|------|-------------|---------------|
| 1 | **Backup Now button** | No `POST /api/admin/backup-now` endpoint exists | Add endpoint in `routes_admin.py` that triggers an immediate backup run in the scheduler. Frontend button is commented out in backup tab, ready to uncomment. |
| 2 | **Listener runtime config** | Listener runs in backup container; viewer has no write endpoint | Option A: Add `PUT /api/admin/listener-config` that writes to `app_settings` table, backup container polls it. Option B: Shared config via DB. Needs `LISTENER_MODE`, `GRACE_PERIOD`, event flags (`LISTEN_EDITS`, etc.) to be writable. |
| 3 | **AI model fallback toggle** | Backend `aiConfig` has `fallback_*` fields but no clear local/remote distinction | Add `fallback_strategy` field (none/local/remote) to AI config. When "local" selected, auto-set `fallback_api_url` to Ollama default. PUT endpoint already exists at `/api/admin/ai-config`. |
| 4 | **Listener interval reduction** | User reported listener tab "not working at all" — likely no frontend-to-backend bridge for changing intervals | Backend `routes_admin.py` only has GET status. Need PUT endpoint for interval/grace period. The backup container's `ListenerManager` reads env vars, not DB — needs refactor to poll `app_settings`. |
| 5 | **Next backup estimate** | No API returns "next scheduled backup time" | Add `next_run` field to `GET /api/admin/backup-config` response. Scheduler knows the next run from cron parsing — expose it. |

## Backlog

- PostgreSQL tsvector FTS
- Full AI assistant panel
- Mega Improvements v3 phases 5-7 (PG FTS, AI panel)
- Mobile: search icon-only with full-width expand on tap (Gemini suggestion)
- Compact sidebar: arrow-key nav with WAI-ARIA toolbar pattern
- Custom user-defined highlight presets (regex builder UI)
