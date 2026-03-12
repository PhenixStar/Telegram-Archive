# Viewer Advanced Features - Implementation Plan

**Created:** 2026-03-11
**Stack:** Python FastAPI + Vue 3 CDN (single `index.html`) + SQLite/PostgreSQL + Tailwind CSS

## Execution Order

| # | Phase | Priority | Status | Branch |
|---|-------|----------|--------|--------|
| 1 | [Project Rules & Journal](phase-01-project-rules-journal.md) | META | Done | main |
| 2 | [Pending Cleanup Tasks](phase-02-pending-cleanup.md) | LOW | Done | main |
| 3 | [Viewer Settings Panel](phase-03-viewer-settings-panel.md) | HIGH | Done | main |
| 4 | [Context Menu](phase-04-context-menu.md) | HIGH | Done | main |
| 5 | [Permalink & Copy Link](phase-05-permalink-copy-link.md) | HIGH | Done | main |
| 6 | [Keyboard Navigation](phase-06-keyboard-navigation.md) | MEDIUM | Done | main |
| 7 | [Theme System Overhaul](phase-07-theme-system-overhaul.md) | MEDIUM | Done | main |
| 8 | [Smart Indexing / FTS](phase-08-smart-indexing-fts.md) | HIGH | Done | main |
| 9 | [Enhanced Message Search](phase-09-enhanced-message-search.md) | HIGH | Done | main |
| 10 | [AI Assistant Side Panel](phase-10-ai-assistant-panel.md) | LOW | Done | main |

## Key Dependencies

```
Phase 1 (rules) -> all others
Phase 2 (cleanup) -> no blockers
Phase 3 (settings) -> Phase 4+ (context menu uses toast from here)
Phase 4 (context menu) -> Phase 5 (permalink reuses menu items)
Phase 8 (FTS) -> Phase 9 (search UX consumes FTS backend)  [SWAPPED per red team]
Phase 10 (AI panel) -> Phase 8 (AI search tool uses FTS)
```

## File Ownership per Phase

| Phase | `index.html` | `main.py` | `adapter.py` | `models.py` | New Files |
|-------|:---:|:---:|:---:|:---:|---|
| 1 | - | - | - | - | `.gitignore`, `journal.md`, `CLAUDE.md` |
| 2 | CSS only | - | - | - | - |
| 3 | JS+HTML | - | - | - | - |
| 4 | JS+HTML | - | - | - | - |
| 5 | JS+HTML | permalink route | `get_messages_around` | - | - |
| 6 | JS only | - | - | - | - |
| 7 | CSS only | - | - | - | - |
| 8 | - | FTS search API | FTS methods | - | `src/db/fts.py` |
| 9 | JS+HTML | search match count | - | - | - |
| 10 | JS only | - | - | - | - |

## Research Reports

- [Context Menu, Permalinks, Keyboard](../reports/researcher-260311-1742-context-menu-links-keyboard.md)
- [Search, Indexing, AI Panel](../reports/researcher-260311-1743-search-indexing-ai-panel.md)

## Red Team Review

**Date:** 2026-03-11 | **Reports:** `reports/code-reviewer-260311-1756-*.md`

Round 1: 18 findings (3 Critical, 4 High, 2 Medium from each reviewer). 15 accepted, 1 deferred, 2 rejected.
Round 2: 6 additional findings (1 Critical, 2 High, 3 Medium). All accepted.

**Key changes applied:**
1. Swapped Phase 8/9 execution order -- FTS backend before search UX (was dependency violation)
2. Phase 10 demoted to "Coming Soon" UI stub -- no backend, no provider ABC, no admin config
3. Phase 7 cut to 1 light theme (Light Default) -- was 3-4 with no user demand
4. Phase 3: timezone → localStorage-only (not global API); backup interval removed from UI
5. Phase 5: redirect validated (`/`-prefix only); backend returns messages around target; uniform 403
6. Phase 8: highlight via DOM TreeWalker on text nodes (not regex on HTML output)
7. Phase 9: SQLite FTS5 only (defer PostgreSQL); `allowed_chat_ids` mandatory; MATCH terms quoted; rate limited
8. Phase 4: Shift+right-click passes through to browser default
9. Phase 8: Contentless FTS5 (`content=''`) to avoid rowid instability with composite PK + VACUUM
10. Phase 9: Explicit fetch call instead of `loadMessages()` (signature mismatch)
11. Phase 4: Scroll listener registered only when menu open (leak prevention)
12. Phase 8: FTS worker crash recovery via `fts_last_indexed_rowid` checkpoint
13. Phase 5: `chat_id` typed as `int` in path param to prevent route collision
14. plan.md: Corrected "Phases 4-6 frontend-only" note (Phase 5 has backend)

## Validation Summary

**Validated:** 2026-03-11
**Questions asked:** 6

### Confirmed Decisions
- **Production DB:** SQLite only. FTS5 implementation correct as planned.
- **Rate limiting:** Use `slowapi` library (pip install). Per-user rate limiting on search + permalink context endpoints.
- **Phase 2 (msg-date-group):** Keep -- apply content-visibility optimization. Test thoroughly.
- **File naming:** Renamed phase-08/09 files to match swapped execution order.
- **Phase 3 cleanup:** Removed all leftover backup interval references.
- **Execution strategy:** Sequential (one phase at a time, each PR merged before next). No batching Phases 4-6.

### Action Items
- [ ] Add `slowapi` to requirements/dependencies (Phases 5, 8)
- [ ] Phase 3 step 2: cleaned up backup interval references (done during validation)
- [ ] Phase files renamed to match execution order (done during validation)

## Notes

- `index.html` is 4122 lines; single-file Vue 3 CDN app -- cannot split
- Phases 4 and 6 are frontend-only; Phase 5 has backend routes (permalink + context API)
- Phase 10 (AI) on separate branch to avoid bloating main
