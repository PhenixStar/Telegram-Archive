# Mega Improvements v3 — Master Plan

**Date:** 2026-03-13
**Status:** Planning
**Branch:** `feat/settings-menu-restructure` (development branch)
**Supersedes:** v2 plan (complete), brainstorm plan (superseded)

## Phases Overview

| # | Phase | Effort | Impact | Status |
|---|-------|--------|--------|--------|
| 1 | [Profile Sidebar Bug Fix](#phase-1) | 15min | Quick Fix | Planned |
| 2 | [AI Configuration Panel](#phase-2) | 3h | High | Planned |
| 3 | [OCR Infrastructure](#phase-3) | 4h | High | Planned |
| 4 | [OCR Frontend Toggle & Viewer](#phase-4) | 3h | High | Planned |
| 5 | [PostgreSQL tsvector FTS](#phase-5) | 3h | Medium | Backlog |
| 6 | [Additional Light Themes](#phase-6) | 2h | Low | Backlog |
| 7 | [Full AI Assistant Panel](#phase-7) | 6h | High | Backlog |

## Dependencies

```
Phase 1 (profile bug)     → independent, quick fix
Phase 2 (AI config panel) → independent (restructure existing AI tab + add model configs)
Phase 3 (OCR backend)     → benefits from Phase 2 (reads model URLs from AI config)
Phase 4 (OCR frontend)    → depends on Phase 3
Phase 5 (PG tsvector)     → independent (only when running PostgreSQL)
Phase 6 (light themes)    → independent
Phase 7 (AI panel)        → depends on Phase 2 config, benefits from Phase 3 OCR data
```

## Recommended Order
1 → 2 → 3 → 4 → (backlog: 5, 6, 7)

## Key Files

| File | Purpose |
|------|---------|
| `src/web/templates/index.html` | Entire frontend |
| `src/web/main.py` | FastAPI routes |
| `src/db/adapter.py` | DB queries |
| `src/db/models.py` | SQLAlchemy ORM models |
| `src/config.py` | Environment config |

## Detailed Phase Files
- [phase-01-profile-sidebar-bugfix.md](phase-01-profile-sidebar-bugfix.md)
- [phase-02-ai-configuration-panel.md](phase-02-ai-configuration-panel.md)
- [phase-03-ocr-infrastructure.md](phase-03-ocr-infrastructure.md)
- [phase-04-ocr-frontend-toggle.md](phase-04-ocr-frontend-toggle.md)
