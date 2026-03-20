---
phase: 4
title: "Backup Schedule Presets"
status: pending
priority: P2
effort: 1h
---

# Phase 4: Backup Schedule Presets

## Context
- [Parent plan](plan.md)
- Backup UI: `index.html:3397-3449`
- Backend: `routes_admin.py:621-664` — GET/PUT `/api/admin/backup-config`
- Current: cron input field + preset buttons (30m, 1h, 2h, 6h, 12h, Daily)

## Overview
The raw cron input (`*/30 * * * *`) is confusing for non-technical users. Presets already exist as buttons but the cron field is still prominent. Redesign to make presets the primary UI, hide cron behind "Advanced" toggle.

## Key Insights
- Backend accepts any valid cron string via PUT `/api/admin/backup-config`
- Current presets (lines 3413-3425): 30min, 1h, 2h, 6h, 12h, Daily — already send cron strings
- `active_boost` toggle (lines 3430-3442): drops to 2min interval when viewer is active
- Timer effectively restarts after each backup completes (scheduler polls every 30s)

## Requirements
- Preset buttons as PRIMARY selection (radio-style, one active)
- Active preset visually highlighted (accent border)
- Hide raw cron input behind "Advanced / Custom" toggle
- Add "Next backup" estimated time display
- Clarify: "Timer starts when last backup completes"

## Architecture
```
┌─────────────────────────────────────┐
│ Backup Schedule                     │
│                                     │
│ [30min] [1h] [2h] [6h] [12h] [24h] │ ← radio-style presets
│                                     │
│ ▸ Custom (cron)                     │ ← collapsed by default
│                                     │
│ ☑ Active Viewer Boost (2min)        │
│                                     │
│ Last: Today 2:00 AM                 │
│ Next: ~Today 8:00 AM               │
└─────────────────────────────────────┘
```

## Implementation Steps
1. Change preset buttons from "click to fill cron" to "click to select + auto-save"
2. Highlight active preset with accent border/bg
3. Wrap raw cron input in a collapsible "Custom" section
4. Add "Next backup" estimate (last_backup + interval)
5. Add note: "Timer resets after each backup completes"

## Todo
- [ ] Redesign presets as radio-style selection (one active, highlighted)
- [ ] Auto-save on preset click (no separate Save button needed)
- [ ] Collapse cron input behind "Custom" toggle
- [ ] Add next backup estimate
- [ ] Clarify timer reset behavior

## Success Criteria
- Non-technical user can set backup interval with one click
- Raw cron hidden unless explicitly needed
- Active preset visually clear
- Timer behavior documented inline
