# Phase 3: Reverse-First Fetch (Newest to Oldest)

## Overview
- **Priority**: Medium
- **Status**: Todo
- **Effort**: ~45 min
- **Impact**: Latest messages visible in viewer within seconds of backup start

## Key Insight
Current: `iter_messages(entity, min_id=last_message_id, reverse=True)` — fetches from oldest unseen forward. If a chat has 500 new messages, the user waits for all 500 before seeing the latest.

Proposed: Fetch newest first, stop when we reach `last_message_id`. Latest messages appear in viewer immediately.

## Related Code Files
- **Modify**: `src/telegram_backup.py` — `_backup_dialog()` method (line ~615-668)
- **Read**: `src/db/adapter.py` — `update_sync_status()`

## Architecture

### Current Flow
```
DB has messages up to ID 1000
Telegram has messages 1001-1500

Fetch: 1001 → 1002 → 1003 → ... → 1500
User sees 1500 only after all 500 are committed
```

### Proposed Flow
```
DB has messages up to ID 1000
Telegram has messages 1001-1500

Fetch: 1500 → 1499 → 1498 → ... → 1001
User sees 1500 after first batch commit (~100 msgs)
```

## Implementation Steps

### 1. Reverse fetch in `_backup_dialog()`
```python
async def _backup_dialog(self, dialog, *, is_archived=False):
    # ... existing setup ...

    last_message_id = await self.db.get_last_message_id(chat_id)

    # Fetch newest messages first (default Telethon order)
    batch_data: list[dict] = []
    grand_total = 0
    running_max_id = last_message_id

    async for message in self.client.iter_messages(entity):
        # Stop when we reach already-synced messages
        if message.id <= last_message_id:
            break

        msg_data = await self._process_message(message, chat_id)
        batch_data.append(msg_data)
        running_max_id = max(running_max_id, message.id)

        if len(batch_data) >= batch_size:
            await self._commit_batch(batch_data, chat_id)
            grand_total += len(batch_data)
            logger.info(f"  → Processed {grand_total} messages...")
            batch_data = []

    # Flush remaining
    if batch_data:
        await self._commit_batch(batch_data, chat_id)
        grand_total += len(batch_data)

    # Update sync_status with highest message ID
    if grand_total > 0:
        await self.db.update_sync_status(chat_id, running_max_id, grand_total)

    return grand_total
```

### 2. Checkpoint handling
With reverse fetch, `running_max_id` is known from the first message. Intermediate checkpoints can still work:

```python
# First message gives us the max ID immediately
if grand_total == 0 and batch_data:
    running_max_id = batch_data[0]["id"]  # Newest message
```

If backup crashes mid-chat, `sync_status` still has the old `last_message_id`. Next run will re-fetch from newest down to that point again. Some duplicates, but `INSERT OR IGNORE` handles them. No data loss.

### 3. Gap-fill still needed
Reverse fetch + crash could leave gaps (e.g., fetched 1500-1300, crashed, next run fetches from 1500 again but sync_status says 1000). Gap-fill handles this correctly since it detects ID gaps in the messages table.

## Todo
- [ ] Change `iter_messages()` call to newest-first (remove `min_id` + `reverse=True`)
- [ ] Add `break` when `message.id <= last_message_id`
- [ ] Simplify checkpoint: single update after all messages committed
- [ ] Test: verify no messages lost on normal run
- [ ] Test: verify crash recovery works (re-fetch + INSERT OR IGNORE)
- [ ] Test: verify gap-fill catches any holes

## Success Criteria
- Latest messages visible in viewer within 10-30s of backup start
- No data loss compared to forward-fetch
- Crash recovery works without manual intervention

## Risk
- Slightly more duplicate inserts on crash recovery (acceptable — INSERT OR IGNORE)
- Messages appear out of order in DB briefly (viewer sorts by date, not insert order)
- Per-batch checkpoint is less precise (max_id known from first batch, not progressive)
