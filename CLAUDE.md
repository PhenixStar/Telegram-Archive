# Telegram Archive - Project Rules

## Stack
- **Backend:** Python FastAPI (async, SQLAlchemy)
- **Frontend:** Vue 3 CDN (single `index.html`, ~4100 lines) + Tailwind CSS CDN
- **Database:** SQLite (default) / PostgreSQL
- **Deployment:** Docker (backup scheduler + standalone viewer)

## Key File Locations
- `src/web/templates/index.html` — entire frontend (Vue 3 app, CSS, HTML)
- `src/web/main.py` — FastAPI app bootstrap, lifespan, background tasks (493 LOC)
- `src/web/dependencies.py` — shared state, auth helpers, ConnectionManager (528 LOC)
- `src/web/routes_auth.py` — login, logout, auth check, profiles (371 LOC)
- `src/web/routes_chat.py` — chat list, messages, search, FTS (726 LOC)
- `src/web/routes_media.py` — media serving, thumbnails, LQIP (188 LOC)
- `src/web/routes_admin.py` — admin CRUD, settings, backup config (867 LOC)
- `src/web/routes_ai.py` — AI chat, OCR, semantic search, embedding (667 LOC)
- `src/web/routes_websocket.py` — WebSocket, broadcast helpers (141 LOC)
- `src/db/adapter.py` — database adapter (mixin composition class, 178 LOC)
- `src/db/adapter_messages.py` — message CRUD, pagination (840 LOC)
- `src/db/adapter_media.py` — media files, reactions (294 LOC)
- `src/db/adapter_viewer.py` — auth, sessions, tokens, audit (433 LOC)
- `src/db/adapter_sync.py` — chats, sync, folders, profiles, stats (1023 LOC)
- `src/db/adapter_settings.py` — app settings (57 LOC)
- `src/db/adapter_search.py` — FTS, OCR, embeddings, semantic search (491 LOC)
- `src/db/models.py` — SQLAlchemy ORM models (522 LOC)
- `src/db/base.py` — database manager, engine setup (312 LOC)
- `src/telegram_backup.py` — backup orchestration (1167 LOC, uses mixins)
- `src/backup_media.py` — media download/processing mixin (382 LOC)
- `src/backup_extraction.py` — data extraction mixin (312 LOC)
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
- **Web routes:** FastAPI APIRouter pattern — each `routes_*.py` imports `from . import dependencies as deps` and accesses shared state via `deps.db`, `deps.config`, `deps.manager` (module-level vars set during lifespan via `set_app_state()`)
- **DB adapter:** Mixin composition — `DatabaseAdapter` inherits 6 domain mixins; each mixin uses `self.db_manager` for DB access
- **Backup:** Mixin composition — `TelegramBackup` inherits `BackupMediaMixin` + `BackupExtractionMixin`

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
