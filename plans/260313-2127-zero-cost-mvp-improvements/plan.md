# Zero-Cost MVP Improvements Plan

## Constraint: READ-ONLY archive viewer — no editing, no live features, no reactions

## Branch: `feat/settings-menu-restructure`

## Phases

| # | Feature | Files | Est | Status |
|---|---------|-------|-----|--------|
| 1 | Resizable sidebar width | index.html | 30m | DONE |
| 2 | Jump to first/last message | index.html, adapter.py, main.py | 1h | DONE |
| 3 | Message count in search results | index.html | 1h | DONE |
| 4 | CSV export | main.py, index.html | 1h | DONE |
| 5 | Media gallery grid toggle | index.html | 2h | DONE |
| 6 | Link preview cards from raw_data | index.html | 2h | DONE |
| 7 | Offline mode (SW cache) | sw.js | 2h | DONE |
| 8 | Smart date grouping in search | index.html | 1h | DONE |
| 9 | PWA install prompt | index.html | 30m | DONE |
| 10 | Semantic search (Ollama embeddings) | adapter.py, main.py, index.html | 4h | TODO |
| 11 | Smart highlights (regex tags) | index.html | 1.5h | DONE |

## Implementation Order
Batch by file to minimize context switches:
1. **Frontend-only** (index.html): 1, 5, 6, 8, 9, 11 — DONE
2. **Backend+Frontend**: 2, 3, 4 — DONE
3. **Infrastructure**: 7 — DONE, 10 — TODO

## Key Decisions
- Desktop-first, mobile-snappy (match Telegram's responsive feel)
- All state persistence via localStorage (no new DB tables for UI prefs)
- #10 (semantic search) uses existing Ollama + qwen3-embedding model — no external API cost
- #6 (link previews) parses Telethon's raw_data JSON for webpage info — no new API calls
- Link preview: only render if raw_data contains web_page — graceful fallback to plain text
