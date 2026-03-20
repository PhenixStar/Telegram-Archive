# Super Admin + Backup Profile Management System

## Status: Complete (Docker tested 2026-03-14)

## Scope
Role-based access control overhaul with super admin tier, dynamic backup profile management, and always-visible profile selector on login page.

## Architecture Decision
**Database-first approach.** Profiles stored in DB (not env vars/files). Super admin is the new top-level role. Current "master" maps to "admin". All within single Docker instance — profiles point to different DB paths or Docker instances.

## Current State
- Roles: `master`, `viewer`, `token` — flat hierarchy
- Auth: `VIEWER_USERNAME`/`VIEWER_PASSWORD` env vars for master, DB `viewer_accounts` for viewers
- Profile selector: exists in HTML but hidden when `backupProfiles.length <= 1`
- Profiles source: `BACKUP_PROFILES` env var (JSON) or `profiles.json` file — currently neither set
- Admin panel: 6 tabs (AI Models, Listener, Backup, Viewers, Tokens, Audit)

## Phases

| # | Phase | Priority | Status | File |
|---|-------|----------|--------|------|
| 1 | Always-visible profile selector | High | Done | [phase-01](phase-01-profile-selector-visible.md) |
| 2 | Role hierarchy + super admin backend | High | Done | [phase-02](phase-02-role-hierarchy-backend.md) |
| 3 | Super admin config UI | Medium | Done | [phase-03](phase-03-super-admin-ui.md) |
| 4 | Admin profile management UI | Medium | Done | [phase-04](phase-04-admin-profile-ui.md) |

## Key Files
- `src/web/templates/index.html` — all frontend (login page, settings, admin panel)
- `src/web/main.py` — auth, API endpoints, role checks
- `src/db/models.py` — SQLAlchemy ORM models
- `src/db/adapter.py` — database query layer
- `docker-compose.yml` — multi-instance deployment

## Role Hierarchy (New)
```
super_admin  →  Full system access, profile CRUD, login page design, all config
    admin    →  Access assigned profiles, rename profiles, create viewers/tokens
    viewer   →  Read-only access to assigned chats
    token    →  Scoped read-only access via share link
```

## Risk
- `index.html` is ~7100 lines — coordinate edits carefully
- Migration path: existing `master` sessions must map to `super_admin` seamlessly
- SQLite schema migration must be non-destructive (ALTER TABLE only)
