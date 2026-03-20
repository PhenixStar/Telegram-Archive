### Backend Feature Delivered -- CSV Export, Boundary Endpoint, Chat Total (2026-03-13)

**Stack Detected** : Python FastAPI (async) + SQLAlchemy
**Files Modified** : `src/web/main.py`, `src/db/adapter.py`
**Files Added**    : none

**Key Endpoints/APIs**

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/chats/{chat_id}/export?format=csv | CSV streaming export (new format param) |
| GET | /api/chats/{chat_id}/export?format=json | Existing JSON export (unchanged default) |
| GET | /api/chats/{chat_id}/boundary?direction=first | Returns oldest message ID |
| GET | /api/chats/{chat_id}/boundary?direction=last | Returns newest message ID |
| GET | /api/chats | Already returns `total` count -- no change needed |

**Design Notes**

- CSV export: streaming via `io.StringIO` + `csv.writer` per row; proper quoting/escaping; includes media columns (`media_type`, `media_file`) by calling `get_messages_for_export(include_media=True)`
- Boundary endpoint: lightweight single-row query ordered by `date asc/desc` with `LIMIT 1`
- `/api/chats` already returns `{ chats, total, limit, offset, has_more }` -- confirmed, no backend change needed for feature 3

**Changes Summary**

1. `main.py`: added `import csv, io`; extended export endpoint with `format` query param and CSV streaming branch; added `/api/chats/{chat_id}/boundary` endpoint
2. `adapter.py`: added `get_boundary_message_id(chat_id, direction)` method returning oldest/newest message ID

**Validation**
- `ast.parse` syntax check: both files pass
- Input validation: `format` restricted to `json`/`csv`; `direction` restricted to `first`/`last`
- Access control: both endpoints use `require_auth` + `get_user_chat_ids` checks
- CSV export respects `no_download` flag same as JSON
