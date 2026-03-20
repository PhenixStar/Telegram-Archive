---
phase: 3
title: "Listener Tab UX Improvement"
status: pending
priority: P3
effort: 1h
---

# Phase 3: Listener Tab UX Improvement

## Context
- [Parent plan](plan.md)
- Listener UI: `index.html:3350-3395`
- Backend: `routes_admin.py:34-45` — GET `/api/admin/listener-status` (read-only)
- `src/realtime.py` — TelegramListener, ListenerManager

## Overview
The listener tab is read-only because the listener runs in a **separate Docker container**. The viewer has no write endpoint. Improve the display to be more informative and less confusing.

**IMPORTANT: No runtime configuration from UI.** Listener mode/interval are env vars in the backup container.

## Key Insights
- Backend: `LISTENER_MODE` env var (auto/always/off), `LISTENER_GRACE_PERIOD` (default 300s)
- Granular flags: `LISTEN_EDITS`, `LISTEN_DELETIONS`, `LISTEN_NEW_MESSAGES`, `LISTEN_CHAT_ACTIONS`
- Status values: running, grace_period, starting, viewer-only, stopped
- Current UI: 4-field grid (status, viewers, mode, grace period) + info note

## Requirements
- Better status visualization (colored badges, not just text)
- Explain what each mode means (auto/always/off) with tooltips
- Show which event types are being listened to (if API provides this)
- Clear messaging: "Configured via Docker environment variables"

## Implementation Steps
1. Replace plain text status with colored status badges (green=running, yellow=grace, red=stopped)
2. Add mode explanation tooltips
3. Add "Monitored Events" section showing active event types (if available from API)
4. Improve info box: list the env vars and explain each
5. Optional: add auto-refresh every 30s for status

## Todo
- [ ] Colored status badges for listener state
- [ ] Mode tooltips (auto: starts when viewer connects, always: 24/7, off: disabled)
- [ ] Event types display (if API returns them)
- [ ] Better info box with env var documentation
- [ ] Optional auto-refresh polling

## Success Criteria
- User understands listener status at a glance (colored badge)
- User knows how to configure (env var documentation in UI)
- No false expectation of UI-based configuration

## Risk
- None — display-only changes, no backend modifications
