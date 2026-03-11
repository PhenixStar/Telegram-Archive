# Telegram Archive - Project Rules

## Stack
- **Backend:** Python FastAPI (async, SQLAlchemy)
- **Frontend:** Vue 3 CDN (single `index.html`, ~4100 lines) + Tailwind CSS CDN
- **Database:** SQLite (default) / PostgreSQL
- **Deployment:** Docker (backup scheduler + standalone viewer)

## Key File Locations
- `src/web/templates/index.html` — entire frontend (Vue 3 app, CSS, HTML)
- `src/web/main.py` — FastAPI routes, auth, WebSocket
- `src/db/adapter.py` — database query layer (SQLAlchemy async)
- `src/db/models.py` — SQLAlchemy ORM models
- `src/db/base.py` — database manager, engine setup
- `src/config.py` — environment variable config
- `src/realtime.py` — Telegram real-time listener

## Critical Rules
- **NEVER split `index.html`** — it's a single-file Vue 3 CDN app. All JS, HTML, CSS lives here.
- **All new JS goes inside the existing `setup()` function** (starts ~line 1660)
- **Expose new refs/functions in the `return {}` block** (~line 3933)
- **Use CSS custom properties (`var(--tg-*)`)** for all theme-dependent colors
- **Theme IIFE** (line 29-32) runs before paint — keep it sync and minimal
- `flex-col-reverse` on messages container — scroll math is inverted

## Code Patterns
- Vue 3 Composition API via `setup()` — no SFC, no build step
- Reactive state: `ref()`, `reactive()`, `computed()`
- Auth: cookie-based sessions via `require_auth` dependency
- Access control: `user.allowed_chat_ids` filters per-viewer
- Message rendering: `v-for` on `sortedMessages`, service vs regular branching
- Media: lightbox for images/videos, audio player for voice notes

## Build & Run
```bash
# Local dev
pip install -e .
uvicorn src.web.main:app --reload --port 8000

# Docker
docker compose up -d
```

## Testing
```bash
pytest tests/ -v
```
