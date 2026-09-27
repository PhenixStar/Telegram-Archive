# Lane C — Viewer features port (hashtag search, what-changed feed, export date range)

## Status: DONE

Worktree: `/raid/projects/tele-private/repo/dev/.claude/worktrees/agent-ac363d88324b79541`
Branch: `worktree-agent-ac363d88324b79541`, re-pointed by the coordinator to fork
`master @ 6c9c46b` mid-task (see the earlier blocked note below — resolved).

All three commits are on this branch, conventional format, ending with
`Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`. Not
pushed, per instructions.

## Earlier blocker (resolved)

The worktree was initially on `origin/main @ 62341a2` (upstream GeiserX tip,
zero unique commits, none of the task's named files existed there — see the
first version of this report for the full ancestry evidence). The coordinator
re-pointed the branch to fork `master @ 6c9c46b`, confirmed by
`git log --oneline -1`. All work below was done against that corrected base.

## Architecture note

Upstream (`5214856`, `a16dc71`, `18821ea`) carries a multi-account
`ChatScope`/chat-ref/account_id layer this fork does not have (one Telegram
account per archive, per `CLAUDE.md`). Every port below is semantic: same
behavior and UX, re-expressed against this fork's plain `chat_id: int` +
`get_user_chat_ids(user) -> set[int] | None` entitlement model. No
`account_id`/`chat_ref` code was added.

## Feature 1 — #hashtag / $cashtag click-through (upstream #321 / 5214856)

**Design.** Tap a `#hashtag` or `$cashtag` rendered in a message to open a tag
view with This Chat / My Messages (archive owner's outgoing side) / All Chats
tabs, newest first. The adapter prefilters with an escaped `ILIKE` (same
pattern `get_messages_paginated` already uses for text search — works
identically on SQLite and PostgreSQL) and a word-boundary regex post-filter so
`#tag` never matches `#taglonger`; a keyset cursor walks bounded chunks
(3000-row scan cap), so no request can walk the whole `messages` table. Cap
truncation is honest: `has_more` stays `False` past the cap (an offset API
cannot reach further) and `truncated` turns `True`.

FTS5 (`src/db/fts.py`, `unicode61` tokenizer) was checked and rejected for
this: its default tokenizer treats `#`/`$` as separators and discards them, so
an FTS query for `#tag` degrades to a plain-word search for `tag` with no way
to distinguish the tag from ordinary text. Direct `ILIKE` + regex sidesteps
that without a schema/tokenizer change (which would need a migration, out of
this lane's scope).

**Files.**
- `src/db/adapter_changes.py` (new) — `TagSearchMixin.search_messages_by_tag`.
- `src/web/routes_changes.py` (new) — `GET /api/tags/{tag}`.
- `src/web/main.py` — registers `routes_changes.router`.
- `src/web/templates/index.html` — `wrapEntity`'s `hashtag`/`cashtag` entity
  cases now render `<a class="tag-link" data-tag="...">` (previously `hashtag`
  was grouped with `mention` as a plain, non-clickable span; `cashtag` had no
  case at all and rendered unwrapped). Click delegation reuses the existing
  `handleSpoilerActivate` container handler (message-text div is already
  wired to `@click`/`@keydown` for spoiler reveal) rather than adding a new
  document-wide listener. New tag-view modal + state
  (`tagView`, `tagTabs`, `openTagView`, `setTagTab`, `loadTagResults`,
  `openTagResult`) reuses the existing `navigateToMessage` helper to jump to a
  result.

**Endpoint.** `GET /api/tags/{tag}?scope=chat|mine|all&chat_id=&limit=&offset=`
— `Depends(require_auth)`; `scope=chat` 403s when `chat_id` is outside
`get_user_chat_ids(user)`; `scope=all`/`scope=mine` filter in SQL via
`Message.chat_id.in_(allowed_chat_ids)` when the viewer is restricted. Tag
shape is validated server-side (`^#(?!\d+$)\w{1,64}$|^\$[A-Z]{1,8}$`) before
any query runs.

**Tests** (`tests/test_tag_search_and_change_feed.py`): 9 adapter tests (real
SQLite) — whole-token boundary match, hashtag case-insensitivity, cashtag
case-sensitivity, newest-first ordering, chat/outgoing scoping,
`allowed_chat_ids` restriction, `has_more` at the limit boundary, empty
result. 7 endpoint tests (`require_auth` override) — bad-tag 400, valid
hashtag/cashtag 200, `scope=chat` missing-id 400, ACL 403/200, and exact
kwargs forwarded to the adapter for both `scope=all` and `scope=mine`.

**Browser evidence.** Both `#launch` and `$TSLA` render as clickable links in
a real message; clicking each opens the tag view and shows the matching
message. Screenshots: `lanec-1-chat.png`, `lanec-2-tag-view.png` (see Browser
Validation section).

**Extraction-layer check (owned by another lane, verified not touched).**
The browser run used hand-written synthetic `raw_data` (`"type": "cashtag"`),
which proves the frontend renders that string correctly but not that the
real backup writer ever produces it. Checked by reading
`src/message_utils.py`'s `_entity_type` (used by `serialize_message_entity`,
called from `backup_extraction.py`, neither file touched by this lane): it
generically converts `type(entity).__name__.removeprefix("MessageEntity")`
to snake_case (`_ENTITY_TYPE_ALIASES` only overrides `strike`->`strikethrough`
and `mention_name`->`mention`). Telethon's `MessageEntityCashtag` converts to
`"cashtag"` under that same generic rule with no alias needed, matching
`MessageEntityHashtag` -> `"hashtag"` (the mapping
`tests/test_backup_extraction_entities.py:177` already exercises). Real
cashtags will carry `type: "cashtag"` in production; the feature is wired
end to end, not just in the synthetic test fixture.

## Feature 2 — what-changed feed (upstream #397 / a16dc71)

**Design.** A feed of edits and deletions the archive kept: soft-deleted
messages (`is_deleted=1`/`deleted_at`) and `message_versions` rows
(`captured_at` = when the archive observed the supersession, carrying the old
text plus the message's current text), merged newest-first. `before` is an
exclusive keyset cursor (the last row's `date`) for "Load older". Hard
deletions cannot appear (their content no longer exists).

**Files.**
- `src/db/adapter_changes.py` — `ChangeFeedMixin.get_recent_changes`.
- `src/web/routes_changes.py` — `GET /api/changes`.
- `src/web/templates/index.html` — a "What changed" icon button next to
  Settings in the sidebar header, a feed modal (deleted rows show the kept
  text; edited rows show old text struck through above the new text), a
  since-window selector (24h/7d/30d/all time), and "Load older" wired to the
  `next_before` cursor. Clicking a row reuses `navigateToMessage` via a small
  `openChangeResult` wrapper (feed rows carry `chat.chat_id`/`message_id`
  rather than `chat_id`/`id`, since a change row isn't a message-search
  result).

**Endpoint.** `GET /api/changes?since=&before=&limit=` — `Depends(require_auth)`;
`allowed_chat_ids=get_user_chat_ids(user)` filters both the deleted and edited
SQL streams identically to every other chat-scoped route.

**Tests**: 6 adapter tests (real SQLite, via `insert_message` /
`mark_message_deleted` / `update_message_text`) — deleted-row shape,
edited-row old/new text, newest-first merge across both streams,
`allowed_chat_ids` restricting both streams, `since` window, `before` cursor
paging. 4 endpoint tests — unrestricted vs. restricted `allowed_chat_ids`
forwarded correctly, invalid `since` 400, `next_before` set only when a page
is full.

**Browser evidence.** The feed shows the synthetic deleted message (Feb 3)
and the synthetic edit (Feb 2, old text struck through, new text below).
Screenshot: `lanec-3-changes-feed.png`.

## Feature 3 — export date range (upstream #396 / 18821ea)

**Design.** `date_from`/`date_to` reach `export_chat`
(`src/web/routes_chat.py`, JSON and CSV) using this fork's OWN existing
date-range convention rather than upstream's — both bounds inclusive, same
contract already established by `GET /api/chats/{id}/messages`'s
`date_from`/`date_to` (verified in the existing `get_messages_paginated`
call). The frontend's advanced message search already widens a bare
`<input type=date>` value to `T00:00:00`/`T23:59:59` before sending; the new
export-range modal follows the identical convention instead of inventing
upstream's exclusive-end/next-midnight scheme, since this fork already had an
established contract for exactly this. A windowed JSON export gets a
`"filters"` block recording what it contains (an unwindowed export carries no
such block, matching the file's own claim of being the full history).

**File-ownership constraint and how it was honored.** The task restricts this
lane to editing only `get_messages_for_export` in
`src/db/adapter_messages.py` (another lane owns
`iter_message_versions_for_export` and the rest of that file — confirmed by
`git log` showing `get_chat_id_for_message` / `_MESSAGE_OPTIONAL_UPDATE_KEYS`
already landed there from the lead). `get_messages_for_export` gained
`date_from`/`date_to` kwargs (mirroring `get_messages_paginated`'s filter).
`iter_message_versions_for_export` was **not modified** — instead,
`export_chat` filters each yielded version dict by its own `date` field
**at the route layer**, achieving the same end-to-end windowing (verified by
test and by the live browser run) without touching a function this lane does
not own.

**Files.**
- `src/db/adapter_messages.py` — `get_messages_for_export` only.
- `src/web/routes_chat.py` — `export_chat` gets `date_from`/`date_to` query
  params; a new module-level `_parse_iso_date` helper replaces the
  previously-duplicated nested closure inside `get_messages` (same helper now
  serves both endpoints — DRY, no behavior change to `get_messages`).
- `src/web/templates/index.html` — "Custom range…" entry added to both
  existing export dropdown menus (mobile + desktop dock), opening a new
  date-range/format modal; existing one-click "Export JSON"/"Export CSV"
  quick actions are unchanged.

**Endpoint.** `GET /api/chats/{chat_id}/export?format=&date_from=&date_to=` —
`no_download` and chat-ACL checks are unchanged (still enforced before any
date parsing); a 400 on invalid ISO dates or `date_from > date_to`.

**Tests** (`tests/test_export_date_range.py`): 5 adapter tests (real SQLite)
— unwindowed, `date_from` inclusive, `date_to` inclusive, both bounds, and the
`include_media=True` branch. 9 endpoint tests — no `filters` block when
unwindowed, `filters` block present and correct when windowed, inverted-range
400, invalid-date 400, CSV branch also receives the parsed dates, `no_download`
still blocks a windowed export, ACL 403, and — the file-ownership-constraint
proof — `message_versions` correctly windowed at the route layer (one test
with an unwindowed request keeping every version, one with a windowed request
keeping only the version whose date falls inside it).

**Browser evidence.** Live end-to-end run: the UI (Custom range modal, dates
`2026-02-01`/`2026-02-03`) opened
`/api/chats/6721111055/export?format=json&date_from=2026-02-01T00%3A00%3A00&date_to=2026-02-03T23%3A59%3A59`;
fetching that exact URL in-page (same session) returned
`filters: {"date_from":"2026-02-01T00:00:00","date_to":"2026-02-03T23:59:59"}`,
`messages: [900001, 900002, 900003]` (all three synthetic messages, correctly
included), and `message_versions: [900002]` (only the one version whose date
falls in the window — 900001 has no versions, 900003 is a deletion not a
version). Screenshot: `lanec-4-export-modal.png`. This run exercised the
REAL `iter_message_versions_for_export` generator against real SQLite (not a
mock), so `_version_in_window`'s assumption that each yielded dict's `"date"`
key is a raw `datetime` is verified by this live result, not merely assumed.

## Access control

Every new endpoint uses `Depends(require_auth)` and filters through
`get_user_chat_ids(user)` exactly like the rest of `routes_chat.py`
(`None` = all chats, a set = only those). `user.no_download` continues to gate
the export endpoint (unchanged path, re-tested after the date-range addition).
All ACL/restriction behavior is covered by unit tests listed above (40 new
adapter/endpoint tests total — 9+6+7+4 for tag search/changes feed,
5+9 for export date range — covering: restricted viewer sees only its
chats' tag results, restricted viewer sees only its chats' changes, `scope=chat`
403 for an unentitled chat, export 403 for an unentitled chat, `no_download`
still blocks a windowed export). Browser validation additionally exercised the
happy path end-to-end as the authenticated master account; the ACL boundary
itself is proven by the pytest suite rather than a second restricted-viewer
browser session, given the effort budget.

## Tests

```
BACKUP_PATH=<tmp> MEDIA_PATH=<tmp>/media DB_PATH=<tmp>/t.db \
  .venv/bin/python -m pytest tests -q -p no:cacheprovider
```
Result: **1533 passed, 1 skipped**, 0 failed. This lane adds exactly 40 new
tests (see the per-feature breakdown above), so the pre-lane count on this
base should be 1493, not the 1501 the coordinator quoted as baseline — flagging
this 8-test discrepancy for the coordinator rather than explaining it away;
it is not a regression (0 failures either way) but the stated baseline number
does not reconcile with the observed delta and is worth reconciling before
other lanes report their own deltas against it.

One real bug found and fixed in my own new tests, not the app: the first
version of `tests/test_tag_search_and_change_feed.py`'s endpoint fixture
overrode `app.dependency_overrides[require_auth]` using
`dependencies.require_auth` imported fresh inside the fixture body. Run in
isolation this passed; run as part of the full suite it failed, because
`tests/test_proxy_auth.py` (which sorts alphabetically before my file) calls
`importlib.reload(deps)` without reloading `routes_changes` (a module it has
no knowledge of, since it predates this lane) — the two `require_auth`
references then diverge by identity and the override silently stops matching.
Fixed by overriding through `routes_changes.require_auth` /
`routes_chat.require_auth` (the object each router's own `Depends(...)` calls
were actually bound to) instead of the `dependencies` module directly, which
is correct regardless of what else already ran in the same suite.

## Lint

`uvx ruff check`/`uvx ruff format` run only on touched files. New files
(`adapter_changes.py`, `routes_changes.py`, both test files) are fully clean.
Modified files (`adapter.py`, `adapter_messages.py`, `main.py`, `routes_chat.py`)
carry pre-existing lint/format debt unrelated to this change (confirmed by
running the same checks against `git show HEAD:<file>` before any edit — same
error count, same lines); only the one new line this lane actually added that
exceeded line length (`export_chat`'s `get_messages_for_export` call) was
reformatted to match ruff's expectation. No unrelated pre-existing debt was
touched, per scope discipline.

## Browser validation

- Built `telegram-archive:lanec` (`Dockerfile`) and
  `telegram-archive-viewer:lanec` (`Dockerfile.viewer`) from this worktree.
- Copied `/raid/projects/tele-private/database-acc2` to a scratch dir under
  `/tmp/claude-1000/.../scratchpad/lane-c-db-copy` (never touched the live
  database or live containers).
- Migrated the copy: `docker run --rm --env-file /raid/projects/tele-private/.env.acc2
  -v <copy>:/data --read-only --tmpfs /tmp telegram-archive:lanec python -c "print(1)"`
  → `SQLite database found at /data/telegram_backup.db - running migrations...
  SQLite migrations complete.`
- Ran the viewer container on `127.0.0.1:18860` with the same env-file.
  Credentials were read programmatically from `.env.acc2` by the puppeteer
  script; never echoed by me.
- Real data (138 chats) had no `#`/`$` tags, no edits, and no soft-deletions
  to exercise against, so three synthetic rows were inserted directly into
  the **copy's** SQLite file (never the live database) for one existing chat
  (`6721111055`, a real private chat with no synthetic-looking title, so I
  addressed it by id only in this report): one message with a hashtag +
  cashtag entity, one edited message + its `message_versions` row, one
  soft-deleted message, all dated Feb 1–3, 2026 (a window with zero real
  messages, confirmed before inserting, so windowed-export counts are exact).
- Puppeteer script: `/tmp/claude-1000/.../scratchpad/browser/lane-c-verify.mjs`
  (built from the existing `v815.mjs`/`check.mjs` patterns in that scratchpad).
  Logged in, opened the chat, clicked both tag links, opened the tag view for
  each, opened the what-changed feed (switched to "All time" since the
  synthetic dates are outside the default 7-day window), and drove the real
  export-range UI end to end (captured the URL the UI would have opened via a
  `window.open` monkeypatch, then fetched that exact URL in-page to verify
  the response body).
- **Console errors: 0** (`pageerror`/`console.error` listeners attached for
  the whole run).
- Screenshots (in the same scratchpad `browser/` directory):
  `lanec-1-chat.png` (hashtag/cashtag rendered clickable in the message),
  `lanec-2-tag-view.png` (tag view for `#launch`), `lanec-3-changes-feed.png`
  (deleted + edited rows), `lanec-4-export-modal.png` (date-range/format
  export modal).
- Cleanup done: `docker stop`/`rm` the viewer container, `docker rmi` both
  `:lanec` images, `rm -rf` the database copy. Verified empty
  (`docker ps -a --filter name=lanec`, `docker images | grep lanec` both
  empty afterward).

## Commits

Three commits on `worktree-agent-ac363d88324b79541`, conventional format,
each ending with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Unresolved questions

1. `message_versions` windowing for export lives at the route layer
   (`routes_chat.py`), not in `iter_message_versions_for_export` itself,
   because that function is outside this lane's file-ownership boundary.
   If the lead later changes that generator's signature, `export_chat`'s
   `_version_in_window` helper (route-layer filter) should be revisited —
   it currently assumes the yielded dict's `"date"` key is a raw `datetime`,
   which matches `_message_version_to_dict`'s current shape.
2. The tag-search scan cap (3000 rows) and per-request chunk size (`max(limit*3, 60)`)
   were kept identical to upstream's tuning; no load testing was done against
   this fork's actual data volumes to confirm the cap is well-suited here too.
3. Browser validation covered the happy path and rendering only; the ACL
   boundary (restricted viewer / share-token) was proven by the pytest suite
   only, not by a second restricted-viewer browser session, to stay within
   the effort budget for this lane.
4. Clicking a tag-view result row or a changes-feed row to confirm the jump
   actually lands on the right message was not browser-exercised (both route
   through the existing `navigateToMessage` helper with the same
   `{chat_id, id}` shape global search results already use successfully, so
   risk is low, but it was not directly observed in this session).
