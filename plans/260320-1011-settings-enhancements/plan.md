---
title: "Settings & Admin Enhancements"
description: "Message highlights, AI model fallback, listener UX, backup presets, token chat search"
status: pending
priority: P2
effort: 8h
branch: feat/settings-menu-restructure
tags: [settings, admin, highlights, ai, backup, tokens, ux]
created: 2026-03-20
---

# Settings & Admin Enhancements

## Summary

5 improvements across settings and admin panels, ranging from new features (message highlights) to UX fixes (backup presets, token search).

## Phases

| # | Phase | Status | Effort | Files |
|---|-------|--------|--------|-------|
| 1 | [Message Highlight Presets](phase-01-message-highlight-presets.md) | pending | 3h | index.html |
| 2 | [AI Model Fallback Local/Remote](phase-02-ai-model-fallback-toggle.md) | pending | 1h | index.html, routes_ai.py |
| 3 | [Listener Tab UX](phase-03-listener-tab-ux.md) | pending | 1h | index.html |
| 4 | [Backup Schedule Presets](phase-04-backup-schedule-presets.md) | pending | 1h | index.html |
| 5 | [Token Chat Search](phase-05-token-chat-search.md) | pending | 1h | index.html |

## Concern: Listener Configuration

The listener runs in the **backup Docker container** — separate from the viewer. The viewer only has `GET /api/admin/listener-status` (read-only). Making interval/mode configurable from UI requires inter-container communication (shared DB settings table or Docker API). **Phase 3 improves display UX only** — runtime config is deferred.

## Architecture Notes

- All frontend changes in `index.html` (Vue 3 CDN single-file)
- Message highlights are **client-side only** — CSS classes applied at render time via computed patterns
- AI fallback config already has backend fields (`fallback_*`) — just needs cleaner UI
- Backup presets replace raw cron with friendly buttons (backend already accepts cron strings)
- Token chat search is pure frontend filter on existing `adminChats` array
