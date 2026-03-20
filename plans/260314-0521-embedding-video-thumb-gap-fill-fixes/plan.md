# Fix: Embeddings, Video Thumbnails, Gap-Fill, Post-Backup Thumbnails

## Context
User testing Docker deployment found 3 bugs + 1 enhancement request.

## Root Causes

### Bug 1: Embedding toggle doesn't work
**File:** `src/web/main.py:442`
`_AI_CONFIG_DEFAULTS` seeds `http://localhost:11434/v1` into `app_settings.ai.embedding.api_url`.
`_get_embedding_config()` (line 484) reads DB first — gets `localhost` — never falls through to
`config.ollama_url` which correctly defaults to `http://host.docker.internal:11434`.

**Fix:** Change all `localhost` references in `_AI_CONFIG_DEFAULTS` to use `config.ollama_url`
(which reads `OLLAMA_URL` env var, defaulting to `host.docker.internal:11434`).
Also add migration to fix existing seeded values in DB.

### Bug 2: Video thumbnails broken
**File:** `Dockerfile.viewer` — `ffmpeg` is NOT installed.
`thumbnails.py:184` checks `shutil.which("ffmpeg")` — returns None — silently skips.
Videos render `<video preload="metadata">` with no `poster` attribute, showing broken/blank.

**Fix A:** Install `ffmpeg` in Dockerfile.viewer.
**Fix B:** Add `poster` attribute to all `<video>` tags pointing to thumbnail endpoint.

### Bug 3: Fill gaps not running
**File:** `src/config.py:153` — `FILL_GAPS` env var defaults to `false`.
Not set in `docker-compose.yml` backup service.

**Fix:** Add `FILL_GAPS: ${FILL_GAPS:-true}` to docker-compose.yml backup service.

### Enhancement: Run thumbnail builder after backup
**File:** `src/scheduler.py` — after backup completes, no thumbnail pre-generation.

**Fix:** Add post-backup thumbnail pre-generation step in `_run_backup_job()` that
scans for videos without cached thumbnails and generates them.

## Files to Modify

| File | Changes |
|------|---------|
| `src/web/main.py` | Fix `_AI_CONFIG_DEFAULTS` localhost → dynamic; add DB migration for existing values |
| `Dockerfile.viewer` | Add `ffmpeg` to apt-get install |
| `src/web/templates/index.html` | Add `poster` attr to `<video>` tags |
| `docker-compose.yml` | Add `FILL_GAPS: true` to backup service |
| `src/scheduler.py` | Add post-backup thumbnail generation |
| `src/web/thumbnails.py` | Add batch pre-generation function |

## Phases

| # | Phase | Status |
|---|-------|--------|
| 1 | Fix embedding URL seeding (main.py) | Todo |
| 2 | Install ffmpeg + video poster attrs | Todo |
| 3 | Enable fill_gaps in docker-compose | Todo |
| 4 | Post-backup thumbnail generation | Todo |

## Implementation Order
1 → 2 → 3 → 4 (no dependencies between them, can parallelize)
