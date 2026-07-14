# Port Remaining Upstream Features (#175, #199, #202, #206, #207) + Deploy

Date: 2026-07-14 | Scope: "continue with all the features open" → port the 5 remaining upstream features, validate, deploy to live

## Summary

Ported 5 features onto local `master` in upstream/dependency order, each validated against the full test suite, then rebuilt + deployed both services to live. One deploy regression found and fixed.

| Commit | Feature | Migration | index.html |
|---|---|---|---|
| 51f5c1d | #175 extension-repair | — | — |
| 965a4f2 | #199 soft-delete mode | 014 | yes |
| de16394 | #202 forum topics + disk stat | — | yes |
| b69a29c | #206 message versions | 015 | yes (drawer) |
| 55d573a | #207 realtime — **backend only** | — | — |
| 9e9d988 | fix: ship message_utils.py in viewer image | — | — |

## Per-feature notes

- **#175**: `finalize_atomic_download` rewritten to rename to the intended clean name (old logic left `video.mp4.7.140234` mangled names); new `sanitize_media_filename` (path-traversal guard); new self-contained `repair_media_extensions.py` invoked after each `run_backup`, streaming via new keyset-paginated `iter_media_paths_for_repair` + `update_media_file_path` on `adapter_media`. Adopted upstream `test_atomic_download_helpers.py` (new contract) + updated 1 listener dedup assertion.
- **#199**: migration 014 (`is_deleted`/`deleted_at`); `DELETION_MODE=hard|soft`; `mark_message_deleted` (coalesce keeps first ts); `_message_conflict_update_values` stops a plain upsert resurrecting a soft-deleted row; listener `_apply_message_deletion` routes both realtime paths + scheduled sync; websocket delete/edit broadcasts carry chat_id/deletion_mode/deleted_at; viewer mutes soft-deleted bodies.
- **#202**: `compute_directory_size` (du, symlinks counted once) → Storage stat reflects real disk; paginated `GetForumTopicsRequest` (per-page flood-retry, partial-result-on-failure, skip deleted topics); early per-dialog topic fetch; `backup_in_progress` flag + viewer indicator + "View all messages" empty-forum fallback; binary units.
- **#206** (largest): migration 015 + `MessageVersion` model; message upsert refactored to `_insert_or_update_message` (insert-or-nothing → locked gated update that captures superseded text in a SAVEPOINT); text-gating policies keep re-scans/older evidence from clobbering fresher text; `update_message_text` returns applied|noop|not_found; `/versions` endpoint; JSON export now an object with streamed messages + message_versions; viewer `edited(n)` drawer.
- **#207 backend**: listener `NEW_MESSAGE` WS payload enriched (flat sender fields + nested media) so the viewer renders sender+media immediately.

## Deferred: #207 frontend (viewer realtime rework)

429 lines / 17 interleaved hunks rewriting the viewer's core realtime engine (ordering, dedup, topic filtering, history cursor, infinite-scroll observer, unseen badge, gallery restore). Deferred because: rewrites the **working production viewer's** core message logic; **no runnable tests**; anchors are stale against the fork's diverged + recently-edited `index.html`; only verifiable by driving the live viewer with real WebSocket events. Recommend a dedicated session using the Chrome browser tools to apply + validate live.

## Validation
- Full suite after each feature; final: **1088 pass / 1 fail / 23 skip**. The 1 failure (`test_config_kwargs_include_flood_sleep_threshold_zero`) is pre-existing + stale (contradicts fork tip `0e549a4`).
- Recurring test-adapt pattern: use the **feature-era** commit's test files (e.g. `c456672`, not `origin/main`, whose tests include later-feature cases like is_pinned-preserve) and retarget mocks/patch-paths to the fork's module layout.
- Ruff: new code clean; only pre-existing debt remains (main.py E402/F401, telegram_backup UP037, routes_chat F821 Request).

## Deployment (live)
- Rebuilt both services; migrations **013→014→015 applied clean to the live 33G SQLite DB** (additive: nullable columns + new table). md5sums verified container↔source on both. Backup healthy (gap-filling, no errors); viewer HTTP 200, `/versions` route live, `backup_in_progress` wired.
- **Regression fixed**: `Dockerfile.viewer` copies a *curated* `src/` file list (not all of `src/`) and omitted `src/message_utils.py`; the db layer's new `utcnow_naive`/`compute_directory_size` imports crash-looped the viewer (`ModuleNotFoundError`). Added `COPY src/message_utils.py` (stdlib-only, viewer-safe). **Lesson**: any new `from ..X import` into `src/db/` or `src/web/` requires `X` in `Dockerfile.viewer`'s COPY list.

## Unresolved Questions
1. **#207 frontend** — schedule the browser-validated session now, or leave the viewer on its current (working) realtime behavior?
2. **Enable `PARALLEL_DOWNLOAD_ENABLED`** on live? (Still OFF from the earlier session; needs explicit OK — changes download behavior + FloodWait exposure.)
3. **Push local master to `myfork`?** 8 feature commits are local-only. (v7.11.2 base + these ports; still not pushed.)
4. Fix the pre-existing stale `flood_sleep_threshold_zero` test + accumulated pre-existing lint debt in a cleanup pass?
