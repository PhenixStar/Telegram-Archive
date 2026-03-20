# Transaction Detection & Data Entry System

## Status: Planning (Deferred — awaiting user data/requirements)

## Scope
Detect, classify, and extract structured data from transactional messages (text + OCR) in the Telegram archive. Provide a verification UI for human review.

## Data Analysis (from live DB, 2026-03-15)
- Total messages: 568,162
- OCR-processed images: 2,885
- Text matches (PHP/USDT/peso/gcash): ~4,995
- OCR matches (receipts/transfers): ~1,133
- Estimated unique transactions: ~3,000-5,000

## Transaction Types Identified

| Type | Source | Pattern | Example |
|------|--------|---------|---------|
| Bank transfer | OCR screenshot | GoTyme, BPI, Metrobank receipts | `Transferred P249,000.00 From RUSTOM JAY T.` |
| USDT crypto | OCR screenshot | Bybit withdrawal confirmations | `Quantity 9,104 USDT Internal Transfer` |
| GCash | OCR screenshot + text | GCash send/receive screenshots | `Amount 9,000.00 Sent via GCash` |
| Ledger entry | Text message | Shorthand `Nk - name /date` | `50k - mhd /02/17` |
| Cash reference | Text message | Informal amounts + "cash" | `118k - cash`, `435k cash` |
| Mixed summary | Text message | Multi-line ledger totals | `150g/300k` (150 grams / 300k PHP) |

## Phases

| # | Phase | Priority | Status | File |
|---|-------|----------|--------|------|
| 1 | DB schema — `transactions` table + migration | Critical | Pending | [phase-01](phase-01-db-schema.md) |
| 2 | Pre-filter — SQL/regex candidate detection | High | Pending | [phase-02](phase-02-pre-filter.md) |
| 3 | LLM extraction worker — structured JSON output | High | Pending | [phase-03](phase-03-llm-extraction.md) |
| 4 | Viewer UI — transaction ledger + verification | Medium | Pending | [phase-04](phase-04-viewer-ui.md) |
| 5 | Reporting — cross-chat summaries, totals | Low | Pending | [phase-05](phase-05-reporting.md) |

## Key Files (will modify)
- `src/db/models.py` — new Transaction model
- `src/db/adapter.py` — transaction CRUD + query methods
- `src/transaction_worker.py` — new background extraction worker
- `src/web/main.py` — API endpoints for transactions
- `src/web/templates/index.html` — transaction ledger UI
- `alembic/versions/` — new migration

## Model Selection
- **Extraction**: qwen3-next-80b (accuracy) or gemma3:27b (speed)
- **Classification pre-filter**: regex/SQL only (no LLM needed)
- **Embedding**: existing bge-m3 (no change needed)

## Architecture

```
Messages DB ──> Pre-filter (SQL LIKE/regex) ──> Candidate queue
                                                    │
                                                    v
                                          LLM Extraction Worker
                                          (qwen3-next-80b)
                                                    │
                                                    v
                                          transactions table
                                                    │
                                                    v
                                          Viewer UI (ledger)
                                          + Human verification
```

## Open Questions (awaiting user input)
- [ ] What additional transaction patterns exist beyond those sampled?
- [ ] Should ledger shorthand codes (mhd, jsep) be resolved to real names?
- [ ] Cross-chat deduplication — same transaction posted in multiple chats?
- [ ] Historical backfill scope — all messages or only recent N months?
- [ ] Reporting requirements — daily/weekly totals? per-person? per-method?
- [ ] Currency conversion — track PHP/USDT separately or normalize?
- [ ] Access control — which viewer accounts can see transactions?

## Risk
- Shorthand ledger entries (50k - mhd) need domain-specific parsing rules
- OCR quality varies — some receipt screenshots are low-res/cropped
- Model hallucination on ambiguous amounts ("50gs" = 50 grams? 50 grand?)
- Large backfill batch could saturate GPU if not rate-limited
