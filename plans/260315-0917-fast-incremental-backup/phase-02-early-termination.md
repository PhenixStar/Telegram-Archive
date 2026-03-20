# Phase 2: Early Termination

## Overview
- **Priority**: High
- **Status**: Todo
- **Effort**: ~10 min
- **Impact**: Safety net for sorted dialog list — stops scanning after N consecutive empty chats

## Key Insight
Dialogs are already sorted by recency (line 287). After the most active chats are processed, the remaining chats are progressively less likely to have new messages. Once we hit 20 consecutive chats with 0 new messages, we can safely stop.

## Related Code Files
- **Modify**: `src/telegram_backup.py` — backup loop in `run()` (line ~310)
- **Modify**: `src/config.py` — add `EARLY_STOP_THRESHOLD` env var

## Implementation Steps

### 1. Add config option
```python
# src/config.py
self.early_stop_threshold = int(os.getenv("EARLY_STOP_THRESHOLD", "20"))
```

### 2. Add counter in backup loop
```python
consecutive_empty = 0

for i, dialog in enumerate(filtered_dialogs, 1):
    # ... existing backup logic ...
    message_count = await self._backup_dialog(dialog, is_archived=is_archived)
    total_messages += message_count

    if message_count == 0:
        consecutive_empty += 1
        if (has_synced_before
            and consecutive_empty >= self.config.early_stop_threshold
            and i > 10):  # Never early-stop in first 10 chats
            logger.info(
                f"Early stop at chat {i}/{len(filtered_dialogs)}: "
                f"{consecutive_empty} consecutive empty chats"
            )
            break
    else:
        consecutive_empty = 0
```

### 3. Interaction with Phase 1 (Smart Skip)
If Phase 1 is implemented, skipped chats count as "empty" for early termination purposes. The two features compound: skip checks top_message first (fast), then early termination catches the rest.

## Todo
- [ ] Add `EARLY_STOP_THRESHOLD` to `src/config.py`
- [ ] Add consecutive_empty counter + break in backup loop
- [ ] Guard: never early-stop in first 10 chats
- [ ] Only apply when `has_synced_before` is True
- [ ] Log the early stop decision

## Success Criteria
- Backup stops scanning after ~50-70 chats instead of 2060
- Combined with Phase 1, total incremental time under 2 min
- No messages missed for active chats

## Risk
- A chat that's low on the recency list but received a message could be missed
- Mitigated by: threshold of 20 is conservative, gap-fill catches any missed messages later
- Set to 0 to disable
