# Planner Report: Viewer Advanced Features

**Date:** 2026-03-11
**Plan dir:** `plans/260311-1742-viewer-advanced-features/`

## Summary

Created comprehensive 10-phase implementation plan for the Telegram Archive Viewer advanced features. All phase files written with full context, requirements, implementation steps, todo checklists, success criteria, and risk assessments.

## Plan Structure

```
plans/260311-1742-viewer-advanced-features/
  plan.md                              -- Overview (execution order, dependencies, file ownership)
  phase-01-project-rules-journal.md    -- META: journal.md, .gitignore, CLAUDE.md updates
  phase-02-pending-cleanup.md          -- Apply .msg-date-group CSS class
  phase-03-viewer-settings-panel.md    -- Toast system + timezone/backup settings UI + API
  phase-04-context-menu.md             -- Right-click context menu on messages/chats/lightbox
  phase-05-permalink-copy-link.md      -- Message permalinks + hover link icon + backend route
  phase-06-keyboard-navigation.md      -- Global keydown handler, PageUp/Down, chat nav, Escape cascade
  phase-07-theme-system-overhaul.md    -- 3 light themes + color-scheme toggle + auto-detect
  phase-08-enhanced-message-search.md  -- Sticky search bar + highlight + match navigation
  phase-09-smart-indexing-fts.md       -- FTS5/tsvector backend + background worker + search API
  phase-10-ai-assistant-panel.md       -- Skeleton side panel (SEPARATE BRANCH)
```

## Execution Order

1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8 -> 9 -> 10

Rationale:
- Phase 1 (meta) first -- establishes project rules
- Phase 2 (cleanup) -- quick win, clears tech debt
- Phase 3 (settings) -- creates toast system reused by 4, 5, 8
- Phase 4 (context menu) before 5 (permalink) -- menu includes "Copy Link" item
- Phase 6 (keyboard) after 4 -- integrates Escape cascade with context menu
- Phase 7 (themes) standalone but after 6 -- no cross-dependencies
- Phase 9 (FTS) before 8 (search UX) -- backend must exist before frontend consumes it
- Phase 10 (AI) last, separate branch

## Parallelization Opportunities

- Phases 4 + 7 could run in parallel (different sections of index.html: JS vs CSS)
- Phases 6 + 7 could run in parallel (JS handlers vs CSS themes)
- Phase 9 (backend-only) could run in parallel with Phase 7 (frontend-only)

## File Ownership Matrix

All phases primarily touch `index.html`. To avoid conflicts during parallel execution:
- Phase 2: CSS section only
- Phase 3: JS (setup) + HTML (settings modal) + API routes
- Phase 4: JS (setup) + HTML (menu template + contextmenu attrs)
- Phase 5: JS (setup) + HTML (link icon) + API routes + adapter
- Phase 6: JS only (keyboard handler)
- Phase 7: CSS only (theme definitions + IIFE)
- Phase 8: JS + HTML (search bar) + API routes
- Phase 9: No index.html changes -- backend only (adapter.py, main.py, new fts.py)
- Phase 10: Separate branch entirely

## Key Decisions Made

1. **Timezone is global, not per-user** -- per-user requires schema extension, deferred
2. **Backup interval change requires restart** -- no hot-reload mechanism exists
3. **Context menu: mobile long-press deferred** -- native mobile behavior acceptable for now
4. **Permalink URL cleaned after navigation** -- `history.replaceState({}, '', '/')` prevents confusion
5. **FTS tokenizer: `unicode61`/`simple`** -- best for multilingual chat text, no stemming
6. **AI panel: skeleton only** -- functional AI on separate branch, separate plan
7. **Cross-chat search disabled until FTS ready** -- ILIKE on all messages too slow
8. **Light theme form fields: explicit color/background** -- prevents invisible text bug

## Unresolved Questions

1. Should permalink URLs persist in browser history or be replaced after navigation? (Decision: replace -- documented in Phase 5)
2. FTS5 rowid + composite PK: needs verification with real data. Fallback: store message_id+chat_id as FTS columns.
3. AI panel: overlay vs push layout? (Decision: overlay with `position: fixed` -- no layout shift)
4. Per-user timezone vs global timezone? (Decision: global for now -- simpler)
5. Backup interval hot-reload mechanism? (Deferred -- toast message warns about restart)
