# Voice Note Auto-Transcription

## Status: Complete

## Scope
Automatically transcribe all voice notes (OGG) using Whisper via Voicebox, storing transcriptions in the same `ocr_text` field used by the OCR worker. Follows identical pattern to OCR processing.

## Trigger Behavior (matches OCR exactly)
- Runs as **background polling loop** in the viewer container (same as OcrWorker)
- Polls every N seconds for voice notes with `ocr_text IS NULL`
- **Post-backup**: new voice notes appear after backup → next poll cycle picks them up automatically
- **Search**: stored in `ocr_text` → FTS5 indexes it → appears in global search + in-chat search
- **Embedding**: `get_unembedded_messages()` already combines `text + ocr_text` (adapter.py:2982) → voice transcriptions get embedded automatically when embedding is triggered
- No manual trigger needed — same fire-and-forget as OCR

## Data Analysis (2026-03-15)
- Total voice notes: 2,276
- Transcribed: 0
- All downloaded: yes (OGG format)
- Average size: ~50-200KB each

## Infrastructure (verified working)
- **Voicebox** running at `http://172.24.0.1:8080` (container: voicebox, GPU 0)
- **Endpoint**: `POST /transcribe/file` — accepts file upload, returns `{text, duration, language, segments}`
- **Model**: faster-whisper (base/small/medium/large available via model management)
- **Response format**: `{"text": "...", "duration": 1.99, "language": "en", "segments": [...]}`
- **Tunnel**: voice.nulled.ai via Cloudflare

## Phases

| # | Phase | Priority | Status | File |
|---|-------|----------|--------|------|
| 1 | Transcription worker (new module) | Critical | Pending | [phase-01](phase-01-transcription-worker.md) |
| 2 | DB query + settings integration | High | Pending | [phase-02](phase-02-db-settings.md) |
| 3 | Viewer UI — playback + transcript display | Medium | Pending | [phase-03](phase-03-viewer-ui.md) |

## Architecture

```
messages + media (type='voice', downloaded=1)
         │
         v
  DB query: get_messages_needing_transcription()
  (ocr_text IS NULL, type='voice')
         │
         v
  TranscriptionWorker._process_one()
    1. Read OGG file from disk
    2. POST /transcribe/file to Voicebox (:8080)
    3. Store result in messages.ocr_text
         │
         v
  Viewer: show transcript below voice player
  (same rendering as OCR text)
```

## Key Design Decisions

### Reuse `ocr_text` field (not a new column)
- Both OCR and transcription serve the same purpose: extracting searchable text from non-text media
- FTS5 index already covers `ocr_text` — transcriptions become instantly searchable
- Existing embedding pipeline will embed transcriptions too
- Viewer already renders `ocr_text` — no UI changes needed for basic display

### Separate worker module (not merged into ocr_worker.py)
- Different API format (file upload vs base64 JSON)
- Different rate limiting needs (Whisper is faster per-item)
- Different settings namespace (`ai.transcription.*` vs `ai.vision.*`)
- Can run independently — OCR worker doesn't need to know about audio

### Settings (app_settings)
```
ai.transcription.api_url = http://172.24.0.1:8080
ai.transcription.model_size = large       # base|small|medium|large
ai.transcription.enabled = true
ai.transcription.rate_limit = 2           # items/sec (Whisper is fast)
ai.transcription.batch_size = 50
ai.transcription.poll_interval = 60       # seconds
```

## Key Files

### Modify
- `src/db/adapter.py` — add `get_messages_needing_transcription()` query
- `src/web/main.py` — start/stop transcription worker, add settings defaults
- `src/web/templates/index.html` — show transcript text below voice player
- `src/config.py` — add transcription config env vars

### Create
- `src/transcription_worker.py` — background worker (~120 lines, mirrors ocr_worker.py pattern)

## Implementation Details

### Phase 1: TranscriptionWorker (~120 lines)
```python
class TranscriptionWorker:
    """Background worker that transcribes voice notes via Whisper."""

    async def _process_one(self, client, item, cfg) -> bool:
        abs_path = resolve_media_path(item["file_path"])
        if not os.path.exists(abs_path):
            return False

        # Upload OGG file to Voicebox
        with open(abs_path, "rb") as f:
            files = {"file": (os.path.basename(abs_path), f, "audio/ogg")}
            resp = await client.post(f"{cfg['api_url']}/transcribe/file", files=files)

        resp.raise_for_status()
        data = resp.json()
        text = data.get("text", "").strip()

        if text:
            # Prefix with metadata for clarity
            lang = data.get("language", "")
            duration = data.get("duration", 0)
            transcript = f"[Voice {duration:.0f}s, {lang}] {text}"
            await self.db.update_ocr_text(item["chat_id"], item["message_id"], transcript)
            return True
        return False
```

### Phase 2: DB Query
```python
async def get_messages_needing_transcription(self, chat_id, limit=50):
    """Get voice messages without transcriptions."""
    stmt = (
        select(Message.id, Message.chat_id, Media.file_path)
        .join(Media, ...)
        .where(
            Message.chat_id == chat_id,
            Message.ocr_text.is_(None),
            Media.type == "voice",
            Media.downloaded == 1,
        )
        .order_by(Message.date.desc())
        .limit(limit)
    )
```

### Phase 3: Viewer UI
- Voice notes already have audio player in index.html
- Add transcript text display below player (same `ocr_text` rendering as images)
- Transcript is searchable via existing FTS5

## Backfill Strategy
- 2,276 voice notes at ~2/sec = ~19 minutes for full backfill
- Whisper large on V100 processes ~30x real-time
- Rate-limited to avoid competing with OCR worker for GPU
- Process newest first (ORDER BY date DESC)

## Risk
- Voicebox GPU contention with Kokoro TTS (both on GPU 0) — mitigated by rate limiting
- Some voice notes may be very short (<1s) or noisy — Whisper handles gracefully
- OGG codec compatibility — faster-whisper handles OGG Opus natively
- Old file paths (/home/dgx/...) — reuse same path fallback from OCR fix

## Effort Estimate
- Phase 1: ~2 hours (worker + wiring)
- Phase 2: ~30 min (DB query + settings)
- Phase 3: ~1 hour (UI transcript display)
- Total: ~3.5 hours
