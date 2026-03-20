# Phase 3: OCR Infrastructure (Backend)

## Overview
- **Priority:** P1
- **Status:** Planned
- **Effort:** 4 hours
- **Depends on:** Phase 2 (AI Configuration Panel — reads model URLs from `app_settings`)

## Key Insights
- **GLM-OCR** model already available at `~/models/vision/glm-ocr/` (2.5GB, OpenAI-compatible API)
- Serves on port 8080 via `python3.10 serve.py --port 8080 --device cuda:0`
- Accepts base64 images, returns extracted text
- **GLM-OCR SDK** at `~/models/vision/glm-ocr-sdk/` provides layout detection pipeline (port 5002)
- **Alternative:** Qwen3-VL-30B-A3B via Ollama (heavier, but more capable for complex images)

## OCR Strategy Decision
**Recommended: GLM-OCR (lightweight)** for batch OCR of chat pictures
- 2.5GB VRAM — can co-locate on GPU 0 with other always-on services
- Purpose-built for document/image text extraction
- OpenAI-compatible API — simple integration
- Falls back gracefully if model server not running

For complex images (charts, handwriting), optionally escalate to Qwen3-VL via Ollama.

## Requirements

### Database
New table `ocr_results` to store extracted text linked to source media:

```python
class OcrResult(Base):
    __tablename__ = "ocr_results"

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_id = mapped_column(String(255), ForeignKey("media.id"), nullable=False, index=True)
    chat_id = mapped_column(BigInteger, nullable=False, index=True)
    message_id = mapped_column(BigInteger, nullable=False)
    extracted_text = mapped_column(Text, nullable=False)
    confidence = mapped_column(Float, nullable=True)  # optional quality score
    model_used = mapped_column(String(100), default="glm-ocr")
    processing_time_ms = mapped_column(Integer, nullable=True)
    created_at = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())

    __table_args__ = (
        Index("idx_ocr_chat_message", "chat_id", "message_id"),
    )
```

### Chat-level OCR toggle
Store in `app_settings` table (already exists):
- Key: `ocr_enabled:{chat_id}` → Value: `"true"` / `"false"` (default: false)
- Key: `ocr_visible:{chat_id}` → Value: `"true"` / `"false"` (admin controls visibility)

### Background Worker
New module `src/ocr_worker.py`:
1. Polls for chats with OCR enabled
2. Finds media items (type=photo) without OCR results
3. Reads image from disk, converts to base64
4. Calls GLM-OCR API (localhost:8080)
5. Stores result in `ocr_results` table
6. Rate-limited: 1 image per 2 seconds to avoid GPU saturation
7. Persistent: tracks progress, resumes where it left off
8. Runs as async background task within FastAPI (or separate process)

### API Endpoints

```
PUT  /api/admin/chats/{chat_id}/ocr          — toggle OCR on/off (admin only)
GET  /api/admin/chats/{chat_id}/ocr/status    — OCR progress (processed/total)
PUT  /api/admin/chats/{chat_id}/ocr/visibility — toggle OCR text visibility
GET  /api/chats/{chat_id}/messages/{msg_id}/ocr — get OCR text for a message's media
```

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Admin UI   │────→│  FastAPI     │────→│  app_settings│
│  OCR Toggle │     │  /api/admin  │     │  ocr_enabled │
└─────────────┘     └──────┬───────┘     └─────────────┘
                           │
                    ┌──────▼───────┐     ┌─────────────┐
                    │  OCR Worker  │────→│  GLM-OCR    │
                    │  (background)│     │  :8080      │
                    └──────┬───────┘     └─────────────┘
                           │
                    ┌──────▼───────┐
                    │  ocr_results │
                    │  (SQLite)    │
                    └──────────────┘
```

## Implementation Steps
1. Add `OcrResult` model to `src/db/models.py`
2. Add auto-create migration (SQLAlchemy create_all)
3. Add OCR adapter methods to `src/db/adapter.py`:
   - `store_ocr_result(media_id, chat_id, message_id, text, model, time_ms)`
   - `get_ocr_for_message(chat_id, message_id)`
   - `get_ocr_progress(chat_id)` → processed count / total photo count
   - `get_pending_ocr_media(chat_id, limit)` → unprocessed photos
4. Create `src/ocr_worker.py` — async background worker
5. Add API endpoints to `src/web/main.py`
6. Wire worker startup in FastAPI lifespan

## Config
**Primary source:** `app_settings` table (configured via Phase 2 AI Configuration Panel)
- `ai.vision.api_url` → OCR endpoint URL (default `http://localhost:8080/v1`)
- `ai.vision.api_key` → API key (empty for local models)
- `ai.vision.model_name` → model identifier (default `glm-ocr`)
- `ai.vision.fallback_url` → fallback endpoint if primary fails
- `ai.vision.fallback_model` → fallback model name

**Environment variable overrides** in `src/config.py` (for Docker deployments without UI access):
- `OCR_RATE_LIMIT` — default `0.5` (images per second)
- `OCR_ENABLED` — default `false` (global kill switch, overrides per-chat toggles)

## Risk Assessment
- **DB safety:** Only INSERT into new `ocr_results` table. Never modify existing tables.
- **GPU contention:** Rate-limit OCR to avoid starving other GPU tasks
- **Disk I/O:** Read images from media directory — ensure path resolution matches backup paths
- **Graceful degradation:** If GLM-OCR server not running, worker logs warning and retries later

## Todo
- [ ] Add OcrResult model to models.py
- [ ] Add DB adapter methods
- [ ] Create ocr_worker.py background processor
- [ ] Add config variables
- [ ] Add admin API endpoints
- [ ] Wire into FastAPI app startup
- [ ] Test with GLM-OCR running on localhost:8080
