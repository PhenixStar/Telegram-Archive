# Phase 01: Project Rules & Journal Setup

## Context

- [README.md](../../README.md)
- [.gitignore](../../.gitignore)

## Overview

- **Priority:** META (do first)
- **Status:** Pending
- **Description:** Establish project journal, update `.gitignore`, document completed features in project CLAUDE.md

## Key Insights

- No `journal.md` exists yet
- `.gitignore` exists with standard Python/DB patterns but no `journal.md` entry
- Project `CLAUDE.md` at repo root is auto-generated boilerplate from claude-flow, not project-specific

## Requirements

**Functional:**
- Create `journal.md` as living roadmap/changelog (gitignored)
- Update `.gitignore` to exclude `journal.md`
- Update project `CLAUDE.md` with project-specific rules

**Non-functional:**
- Journal must document all previously completed features (token login, viewer UX enhancements)

## Related Code Files

**Modify:**
- `.gitignore` -- add `journal.md`
- `CLAUDE.md` (project root) -- replace boilerplate with project-specific rules

**Create:**
- `journal.md` -- project journal

## Implementation Steps

1. Create `journal.md` in project root with sections: Overview, Completed Features, Current Sprint, Backlog, Changelog
2. Document all completed features from previous plans (token login, thumbnails, lazy loading, listener auto-activation, admin chat editing, accessibility)
3. Add `journal.md` to `.gitignore`
4. Rewrite project `CLAUDE.md` with:
   - Stack description (FastAPI + Vue 3 CDN + SQLite/PostgreSQL)
   - Key file locations
   - Build/test commands
   - Code patterns (single-file Vue app, CSS custom properties for themes, `setup()` pattern)
   - Rule: never split `index.html` into multiple files
   - Rule: use existing CSS custom properties for theming
   - Rule: all new JS goes inside existing `setup()` function

## Todo

- [ ] Create `journal.md` with completed features documented
- [ ] Add `journal.md` to `.gitignore`
- [ ] Rewrite project `CLAUDE.md` with project-specific rules

## Success Criteria

- `journal.md` exists with accurate feature history
- `git status` does not show `journal.md` as untracked
- `CLAUDE.md` contains actionable project-specific guidance

## Risk Assessment

- **Low risk** -- metadata-only changes, no code affected
