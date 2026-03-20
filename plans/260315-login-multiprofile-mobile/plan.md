# Login Redesign + Multi-Backup Profile + Mobile Web

## Status: All Phases Complete

## Scope
Three interconnected frontend improvements:
1. **Login page redesign** — more appealing, Telegram-native look
2. **Backup-profile selector** — multi-instance support (multiple admins/DBs/Telegram accounts)
3. **Mobile web viewer** — responsive mobile experience matching Telegram Web A patterns

## Architecture Decision
**Frontend-first approach.** Backend changes minimal (profile discovery endpoint). Each Docker viewer instance already runs independently — the profile selector is a frontend routing layer.

## Phases

| # | Phase | Status | File |
|---|-------|--------|------|
| 1 | Login page visual redesign | Done | [phase-01](phase-01-login-redesign.md) |
| 2 | Backup-profile selector system | Done | [phase-02](phase-02-profile-selector.md) |
| 3 | Mobile web responsive overhaul | Done (critical items complete) | [phase-03](phase-03-mobile-web.md) |

## Key Files
- `src/web/templates/index.html` — all frontend changes (lines 841-941 login, CSS, JS setup)
- `src/web/main.py` — auth endpoints (`/api/auth/check`, `/api/login`, `/auth/token`)
- `docker-compose.yml` — multi-instance deployment model
- `docs/telegram-web-a-reference.html` — saved Telegram Web A HTML (design reference)

## Dependencies
- Phase 1 is standalone
- Phase 2 builds on Phase 1 (adds profile selector to login card)
- Phase 3 is independent but touches same files (coordinate carefully)

## Risk
- `index.html` is ~7000 lines — all phases touch it. Coordinate edits to avoid conflicts.
- Mobile responsive may interact with sidebar resize handle (recently fixed).
