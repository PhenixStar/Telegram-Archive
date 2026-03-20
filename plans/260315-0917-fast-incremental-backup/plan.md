# Fast Incremental Backup System

## Status: Deployed & Verified

## Scope
Optimize backup to be 10-20x faster on incremental runs, fetch newest messages first, trigger OCR immediately per-chat, and add smart early termination.

## Current Bottlenecks
- 2060 chats scanned sequentially even when ~50 have activity
- Forward-order fetch (old→new) delays newest messages
- OCR waits for entire backup to finish before processing new images
- Gap-fill is separate post-backup pass

## Phases

| # | Phase | Priority | Status | File |
|---|-------|----------|--------|------|
| 1 | Smart chat skip (dialog.top_message check) | Critical | Done | [phase-01](phase-01-smart-chat-skip.md) |
| 2 | Early termination after N empty chats | High | Done | [phase-02](phase-02-early-termination.md) |
| 3 | Reverse-first fetch (newest→oldest) | Medium | Done | [phase-03](phase-03-reverse-fetch.md) |

## Key Files
- `src/telegram_backup.py` — backup logic, `_backup_dialog()`, `_fill_gaps()`
- `src/scheduler.py` — backup scheduling, gap-fill trigger
- `src/config.py` — env var configuration
- `src/ocr_worker.py` — image OCR processing

## Risk
- Smart skip relies on `dialog.message.id` accuracy (Telegram API is authoritative)
- Early termination could miss out-of-order activity (mitigated by conservative threshold)
- Reverse fetch needs careful checkpoint handling
