# Phase 1: Backend — Forward Pagination + Date Range

## Priority: HIGH
## Status: COMPLETE
## Depends on: None

---

## Overview

Add `after_date`/`after_id` params for forward (newer) message loading, and `date_from`/`date_to` for date range filtering. Also enrich context API response with boundary flags.

---

## Key Insights

- Current `get_messages_paginated` only supports `before_date`/`before_id` (backward/older)
- Messages are ordered `date DESC, id DESC` — forward loading needs `date ASC, id ASC`
- Context API (`get_messages_around`) returns ~50 messages but doesn't indicate if more exist in either direction
- The `flex-col-reverse` layout means "forward" messages (newer) appear at the bottom of the viewport

---

## Related Code Files

### Modify
- `src/db/adapter.py` — `get_messages_paginated()` (line 1107), `get_messages_around()` (line 1245)
- `src/web/main.py` — `get_messages()` (line 1394), `get_message_context()` (line 1462)

---

## Implementation Steps

### Step 1: Add `after_date`/`after_id` to `get_messages_paginated`

In `src/db/adapter.py`, add parameters:

```python
async def get_messages_paginated(
    self,
    chat_id: int,
    limit: int = 50,
    offset: int = 0,
    search: str | None = None,
    before_date: datetime | None = None,
    before_id: int | None = None,
    after_date: datetime | None = None,    # NEW
    after_id: int | None = None,           # NEW
    date_from: datetime | None = None,     # NEW
    date_to: datetime | None = None,       # NEW
    topic_id: int | None = None,
) -> list[dict[str, Any]]:
```

Add forward cursor logic after the existing `before_date` block (~line 1170):

```python
elif after_date is not None:
    if after_id is not None:
        stmt = stmt.where(
            or_(
                Message.date > after_date,
                and_(Message.date == after_date, Message.id > after_id)
            )
        )
    else:
        stmt = stmt.where(Message.date > after_date)
    # Forward: oldest first so we get the NEXT messages chronologically
    stmt = stmt.order_by(Message.date.asc(), Message.id.asc()).limit(limit)
```

Add date range filtering (applies to any pagination mode):

```python
if date_from is not None:
    stmt = stmt.where(Message.date >= date_from)
if date_to is not None:
    stmt = stmt.where(Message.date <= date_to)
```

### Step 2: Update messages API endpoint

In `src/web/main.py` `get_messages()`, add params:

```python
after_date: str | None = None,
after_id: int | None = None,
date_from: str | None = None,
date_to: str | None = None,
```

Parse and pass to adapter. Validate mutual exclusion of `before_*` and `after_*`.

### Step 3: Enrich context API response

In `get_messages_around`, after building the message list, check boundaries:

```python
# Check if there are older messages beyond our window
has_older = len(before_rows) == half
# Check if there are newer messages beyond our window
has_newer = len(after_rows) == half

return {
    "messages": result,
    "has_more_older": has_older,
    "has_more_newer": has_newer,
    "target_msg_id": msg_id,
}
```

### Step 4: Verify with curl

```bash
# Forward pagination
curl "localhost:8847/api/chats/{id}/messages?after_date=2025-01-01&after_id=100&limit=10"

# Context with boundary flags
curl "localhost:8847/api/chats/{id}/messages/{msg_id}/context"
# Should return: { messages: [...], has_more_older: true, has_more_newer: true }
```

---

## Todo List

- [x] Add `after_date`/`after_id` to `get_messages_paginated`
- [x] Add `date_from`/`date_to` to `get_messages_paginated`
- [x] Update `get_messages` API endpoint with new params
- [x] Enrich `get_message_context` response with boundary flags
- [x] Test forward pagination returns correct chronological order

---

## Success Criteria

- `GET /api/chats/{id}/messages?after_date=X&after_id=Y` returns messages NEWER than cursor
- `GET /api/chats/{id}/messages?date_from=X&date_to=Y` returns messages within range
- Context API returns `has_more_older` and `has_more_newer` booleans
- No regression on existing `before_date` pagination
