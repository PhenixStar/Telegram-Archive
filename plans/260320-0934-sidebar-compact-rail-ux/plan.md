---
title: "Sidebar Compact Rail UX"
description: "Compact sidebar rail showing only avatars when dock is open, with logo ring, hover tooltips, and stats relocation"
status: pending
priority: P1
effort: 3h
branch: feat/settings-menu-restructure
tags: [ui, sidebar, compact, dock, ux]
created: 2026-03-20
---

# Sidebar Compact Rail UX

## Summary

When the right dock canvas is open, the left sidebar compacts to a narrow rail. Currently it just shrinks width to 64px but still shows the full header, search, folders, and truncated chat rows — messy. This plan makes the compact state intentionally designed.

## Phases

| # | Phase | Status | Effort |
|---|-------|--------|--------|
| 1 | [Syncing tooltip + header cleanup](phase-01-syncing-tooltip-header-cleanup.md) | pending | 0.5h |
| 2 | [Compact logo with role ring](phase-02-compact-logo-role-ring.md) | pending | 1h |
| 3 | [Compact chat list (avatars only)](phase-03-compact-chat-list-avatars-only.md) | pending | 1h |
| 4 | [Stats relocation to profile panel](phase-04-stats-relocation-profile-panel.md) | pending | 0.5h |

## Key Decisions

1. **Two sidebar states:** Normal (full width) and compact (64px rail). Driven by existing `sidebarFocusCompact` computed.
2. **Compact header:** Replace title+icons with a single circular logo element — avatar-sized (48px), with a colored ring (gold for super_admin, blue otherwise).
3. **Compact chat list:** Only avatars in a centered vertical column. No names, no message previews, no timestamps. Tooltip on hover for chat name.
4. **"Syncing..." text:** Hidden by default — shown as tooltip on hover over the spinner icon, in both normal and compact modes.
5. **Chat header stats:** Removed from header. Already present in profile panel (Messages + Media counts). Add storage size to profile panel for parity.
6. **Search + folders:** Hidden in compact mode (`v-show="!sidebarFocusCompact"`).

## Architecture

```
Normal sidebar (220-600px):
┌──────────────────────┐
│ [icon] Title   [⚙][↗]│
│ [Search............] │
│ [All Chats ▼]        │
│ [avatar] Chat Name   │
│          Preview...   │
│ [avatar] Chat Name   │
│          Preview...   │
└──────────────────────┘

Compact rail (64px):
┌────┐
│[◉] │  ← Logo with role ring (gold/blue)
│    │
│ ○  │  ← Chat avatar (tooltip: chat name)
│ ○  │
│ ○  │
│ ○  │
└────┘
```

## File touched

- `repo/dev/src/web/templates/index.html` — all changes in this single file

## Dependencies

- `sidebarFocusCompact` computed (already implemented in dock feature)
- `rightDockOpen` ref (already implemented)
