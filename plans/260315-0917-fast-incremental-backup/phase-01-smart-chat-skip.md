# Phase 1: Smart Chat Skip

## Overview
- **Priority**: Critical
- **Status**: Todo
- **Effort**: ~30 min
- **Impact**: 10-20x faster incremental backups

## Key Insight
Telegram's `dialog.message` contains the latest message object for each chat. Its `.id` can be compared against `sync_status.last_message_id` to instantly determine if a chat has new messages — no need to call `iter_messages()` at all for chats with no activity.

## Related Code Files
- **Modify**: `src/telegram_backup.py` — `run()` method (line ~310 loop)
- **Read**: `src/db/adapter.py` — `get_last_message_id()`

## Implementation Steps

### 1. Pre-load all sync_status in bulk
Currently `get_last_message_id()` is called per-chat inside `_backup_dialog()`. For skip logic, we need it earlier — during dialog enumeration.

```python
# In run(), after filtering dialogs, before the backup loop:
# Bulk-load all last_message_ids to avoid N+1 queries
sync_map = await self.db.get_all_last_message_ids()
# Returns dict[int, int] = {chat_id: last_message_id}
```

Add `get_all_last_message_ids()` to adapter:
```python
async def get_all_last_message_ids(self) -> dict[int, int]:
    """Return {chat_id: last_message_id} for all synced chats."""
    async with self._session() as session:
        result = await session.execute(
            select(SyncStatus.chat_id, SyncStatus.last_message_id)
        )
        return {row.chat_id: row.last_message_id for row in result}
```

### 2. Skip chats with no new messages
In the backup loop, compare `dialog.message.id` with `sync_map.get(chat_id, 0)`:

```python
skipped = 0
for i, dialog in enumerate(filtered_dialogs, 1):
    entity = dialog.entity
    chat_id = self._get_marked_id(entity)

    # Smart skip: if dialog's latest message ID <= our last synced ID, skip
    last_synced = sync_map.get(chat_id, 0)
    dialog_top_id = dialog.message.id if dialog.message else 0

    if has_synced_before and last_synced > 0 and dialog_top_id <= last_synced:
        skipped += 1
        continue  # No new messages

    # ... existing backup logic ...

if skipped:
    logger.info(f"Skipped {skipped} chats with no new messages")
```

### 3. Still update chat metadata for skipped chats
Even when skipping message fetch, we should update chat name/photo/archived status. Add a lightweight metadata-only update:

```python
if has_synced_before and last_synced > 0 and dialog_top_id <= last_synced:
    # Update chat metadata only (name, photo, archived status)
    chat_data = self._extract_chat_data(entity, is_archived=is_archived)
    await self.db.upsert_chat(chat_data)
    skipped += 1
    continue
```

## Todo
- [ ] Add `get_all_last_message_ids()` to `src/db/adapter.py`
- [ ] Pre-load sync_map in `run()` before backup loop
- [ ] Add skip logic with `dialog.message.id` comparison
- [ ] Keep metadata update for skipped chats
- [ ] Log skip count at end
- [ ] Test: verify no messages are missed after skip

## Success Criteria
- Incremental backup with ~50 active chats completes in 1-3 min instead of 15-20 min
- All new messages still captured
- Chat metadata still updated for inactive chats

## Risk
- `dialog.message` could theoretically be `None` for empty chats — handle with `dialog.message.id if dialog.message else 0`
- Edited/deleted messages won't trigger `dialog.message.id` change — acceptable since `SYNC_DELETIONS_EDITS` is a separate feature
