# Journal: DB Path Fix + Backup Architecture Brainstorm
**Date:** 2026-03-15 09:17 (Asia/Manila)

## What Happened

### DB Path Mismatch (Critical Bug Fixed)
- **Symptom**: Viewer showed no messages after March 13 despite backup running
- **Root cause**: Two containers used **separate database files**
  - `telegram-backup` wrote to `repo/dev/data/telegram_backup.db` (208MB, env `DB_PATH=/data/telegram_backup.db`)
  - `telegram-viewer` read from `database/telegram_backup.db` (930MB, hardcoded `DB_PATH=/data/backups/telegram_backup.db`)
  - Volume mounts pointed at different host directories
- **Fix**: Changed backup container's volume from `./data:/data` to absolute path pointing at shared `database/` dir
- **Result**: Both containers now share same SQLite file. Incremental backup immediately started pulling 1,369+ new messages (March 13-15)

### Super Admin System
- All 5 UX fixes from plan complete (token chat list, copy link, avatar profile click, global search, number-normalized search)
- Profile dropdown selector working
- `profile_names.join` crash fixed (backend returns string, not array)

## Current State
- Backup running: 910/2060 chats processed, ~1,369 new messages synced
- OCR worker: trying to process but images not yet on disk (500 errors expected)
- Next scheduled backup: 2026-03-15 06:00 UTC

---

## Brainstorm: Faster Incremental Backup Architecture

### Current Problems
1. **Slow full scan**: 2060 chats processed sequentially, even when only ~50 have new messages
2. **Forward order (old→new)**: `iter_messages(min_id=last, reverse=True)` — starts from oldest unseen, walks forward
3. **OCR waits for backup completion**: OCR worker polls independently, doesn't know when new images arrive
4. **Gap-fill runs AFTER full backup**: Additional pass over all chats to detect/fill gaps
5. **No early termination**: Even if top-N most-active chats have 0 new messages, still scans all 2060

### Proposed Improvements

#### A. Reverse-First Fetch (Newest → Oldest)
**Idea**: For incremental backups, fetch from newest message backward to `last_message_id` instead of forward.

**Why**: User cares about latest messages first. With reverse fetch:
- Latest messages appear in viewer within seconds
- Media files for recent messages download first (OCR can start immediately)
- If backup crashes/interrupts, the most valuable data is already saved

**Implementation**:
```python
# Instead of: iter_messages(entity, min_id=last_message_id, reverse=True)
# Use:        iter_messages(entity, offset_id=0, reverse=False)  # newest first
# Then stop when message.id <= last_message_id
```

**Tradeoff**: Checkpoint logic needs adjustment — can't use running_max_id linearly. Need to track "newest fetched" separately from "gap-free up to".

#### B. Smart Chat Prioritization
**Idea**: Query Telegram for dialog list, compare `dialog.top_message` ID with `sync_status.last_message_id`. Only backup chats where `top_message > last_message_id`.

**Why**: Avoids iterating 2000+ chats when only 50 have activity.

**Implementation**:
```python
# During dialog enumeration, skip chats with no new messages:
for dialog in filtered_dialogs:
    chat_id = self._get_marked_id(dialog.entity)
    last_synced = await self.db.get_last_message_id(chat_id)
    if dialog.message and dialog.message.id <= last_synced:
        continue  # No new messages, skip entirely
```

**Impact**: Reduces 2060-chat scan to ~50-100 chats in typical incremental run. 10-20x faster.

#### C. Streaming OCR Trigger
**Idea**: After each chat's batch commit, emit a signal/flag that new images are available for OCR. OCR worker picks them up immediately instead of waiting for full backup.

**Options**:
1. **DB flag**: Set `ocr_pending=true` on new media rows → OCR worker polls this
2. **In-process callback**: If OCR worker runs in same process, call it directly per-batch
3. **File marker**: Touch a `/data/.ocr_trigger` file → OCR worker watches with inotify

**Simplest**: Option 1 — already how it works (OCR queries `messages_needing_ocr`). The bottleneck is that images aren't downloaded yet. Need to ensure media download happens within the batch commit.

#### D. Two-Phase Backup
**Phase 1 — Fast incremental** (30s-2min):
- Only chats with `dialog.top_message > last_synced`
- Fetch newest→oldest (reverse)
- Download media inline
- Trigger OCR per-chat

**Phase 2 — Gap-fill + integrity** (background, low priority):
- Run gap detection on all chats
- Fill gaps for chats that had issues
- Verify media file integrity
- Can run on idle schedule

#### E. Early Termination for Sorted Dialogs
**Idea**: Since dialogs are sorted by recency, once we hit N consecutive chats with 0 new messages, stop scanning.

**Implementation**:
```python
consecutive_empty = 0
EARLY_STOP_THRESHOLD = 20  # Stop after 20 consecutive empty chats

for i, dialog in enumerate(filtered_dialogs, 1):
    message_count = await self._backup_dialog(dialog)
    if message_count == 0:
        consecutive_empty += 1
        if has_synced_before and consecutive_empty >= EARLY_STOP_THRESHOLD:
            logger.info(f"Early stop: {consecutive_empty} consecutive empty chats")
            break
    else:
        consecutive_empty = 0
```

**Risk**: Could miss chats that received messages out of recency order (rare).

### Recommended Priority
1. **B (Smart chat skip)** — Biggest win, simplest change, no behavior change
2. **E (Early termination)** — Safety net, 5 lines of code
3. **A (Reverse fetch)** — Best UX improvement, moderate complexity
4. **D (Two-phase)** — Architectural, builds on A+B
5. **C (Streaming OCR)** — Already mostly works, just needs media download timing fix

### Risk Assessment
- **B** is safe: `dialog.message.id` is authoritative from Telegram API
- **E** has false-negative risk but threshold of 20 is very conservative
- **A** needs careful checkpoint handling to avoid re-fetching on restart
- SQLite concurrent writes (backup + viewer reads) already use WAL mode — no issue
