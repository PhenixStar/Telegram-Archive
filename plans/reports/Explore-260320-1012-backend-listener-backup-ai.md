# Backend Listener, Backup Configuration & AI Model Endpoints Exploration

**Date:** 2026-03-20  
**Thoroughness:** Medium  
**Repository:** /home/phenix/projects/tele-private/repo/dev

---

## 1. LISTENER ENDPOINTS & STATUS

### Endpoint: `/api/admin/listener-status`
**File:** `/home/phenix/projects/tele-private/repo/dev/src/web/routes_admin.py` (lines 34-46)  
**Method:** GET  
**Auth:** Master role required  
**Response:**
```json
{
  "mode": "auto|always|off",
  "status": "running|paused|viewer-only",
  "grace_period": 300,
  "viewer_count": <int>,
  "listener_available": true|false
}
```
**Current Behavior:** Returns listener mode (LISTENER_MODE env var), status based on ListenerManager state, and viewer count from active connections.

---

## 2. LISTENER IMPLEMENTATION & CONFIGURATION

### Listener Modes (config.py, lines 165-175)
Three modes controlled by `LISTENER_MODE` environment variable:

| Mode | Behavior | Control |
|------|----------|---------|
| `auto` | Starts when viewers connect, stops after grace period if all disconnect | `LISTENER_GRACE_PERIOD=300` (default) |
| `always` | Listener always runs | No grace period |
| `off` | Listener disabled | Default if neither set |

**Default:** `off`

**Backward Compat:** If `ENABLE_LISTENER=true`, maps to `LISTENER_MODE=always`

### Listener Granular Controls (config.py, lines 181-203)
Only apply when listener is running:

| Setting | Env Var | Default | Purpose |
|---------|---------|---------|---------|
| `listen_edits` | `LISTEN_EDITS` | `true` | Apply message text edits in real-time |
| `listen_deletions` | `LISTEN_DELETIONS` | `true` | Delete messages from backup when deleted on Telegram |
| `listen_new_messages` | `LISTEN_NEW_MESSAGES` | `true` | Save new messages to backup in real-time |
| `listen_new_messages_media` | `LISTEN_NEW_MESSAGES_MEDIA` | `false` | Download media immediately (if true) or on next scheduled backup |
| `listen_chat_actions` | `LISTEN_CHAT_ACTIONS` | `true` | Track chat photo/title/member changes |

### Mass Operation Protection (config.py, lines 227-229)
Zero-footprint buffering — if burst detected, operations discarded WITHOUT writing to DB:

| Setting | Env Var | Default |
|---------|---------|---------|
| `mass_operation_threshold` | `MASS_OPERATION_THRESHOLD` | `10` |
| `mass_operation_window_seconds` | `MASS_OPERATION_WINDOW_SECONDS` | `30` |
| `mass_operation_buffer_delay` | `MASS_OPERATION_BUFFER_DELAY` | `2.0` |

**Protection Logic:** If >10 operations in 30-second window, ALL buffered operations are discarded, preventing database corruption from mass attacks.

### Listener Implementation (listener.py)
**Class:** `TelegramListener` (line 206)  
**Key Methods:**
- `create()` (line 297): Factory to create listener with database initialized
- `connect()` (line 311): Connect to Telegram, register event handlers
- `run()` (line 1085): Background task listening for events
- `stop()` (line 1137): Gracefully stop listener

**Event Handlers Registered (lines 630-1031):**
- `on_message_edited`: Handles `MessageEdited` events (line 634)
- `on_message_deleted`: Handles `MessageDeleted` events (line 688)
- `on_new_message`: Handles `NewMessage` events (line 751)
- `on_chat_action`: Tracks chat metadata changes (line 885)
- `on_pinned_messages`: Handles pinned message updates (line 1031)

**Rate Limiting:** `MassOperationProtector` class (line 44) uses sliding time window to count operations per chat. Once threshold exceeded, chat is blocked for remainder of window.

### ListenerManager (dependencies.py, lines 146-243)
**Purpose:** Manages Telegram listener lifecycle based on viewer presence in `LISTENER_MODE=auto`

**Key Properties:**
- `_listener_available` (line 160): Can import `TelegramListener` (true if Telethon available, false in viewer-only containers)
- `status` property (line 170): Returns `"running"`, `"paused"`, or `"viewer-only"`
- Grace period timer: Cancels on last viewer disconnect, waits `LISTENER_GRACE_PERIOD` before stopping (line 187-195)

**Public Methods:**
- `on_viewer_connected()` (line 198-214): Starts listener if auto-mode
- `on_all_viewers_disconnected()` (line 217-240): Arms grace period timer, then stops listener

---

## 3. BACKUP CONFIGURATION ENDPOINTS

### Endpoint: `/api/admin/backup-config`
**File:** `/home/phenix/projects/tele-private/repo/dev/src/web/routes_admin.py`

#### GET (lines 621-632)
**Method:** GET  
**Auth:** Master role required  
**Response:**
```json
{
  "schedule": "0 */6 * * *",
  "default_schedule": "0 */6 * * *",
  "active_boost": false,
  "viewer_heartbeat": "2026-03-20T14:30:00+00:00"
}
```
**Current Behavior:** Returns cron schedule (from DB or default), active_boost flag, and last viewer heartbeat timestamp.

#### PUT (lines 635-665)
**Method:** PUT  
**Auth:** Master role required  
**Request Body:**
```json
{
  "schedule": "0 */6 * * *",
  "active_boost": true|false
}
```
**Validation:** Cron format enforced — must have exactly 5 fields (minute hour day month dow)

**Behavior:** 
- Updates `backup.schedule` and `backup.active_boost` in app_settings table
- Creates audit log with action `backup_config_updated:schedule,active_boost`
- Returns updated keys

### Endpoint: `/api/admin/backup-heartbeat`
**File:** `/home/phenix/projects/tele-private/repo/dev/src/web/routes_admin.py` (lines 668-673)

**Method:** POST  
**Auth:** Any authenticated user  
**Behavior:** Records current UTC timestamp to `backup.viewer_heartbeat` setting. Used to detect viewer activity for backup boost logic.

### Configuration Storage
**Location:** SQLite/PostgreSQL `app_settings` table

**Keys:**
- `backup.schedule` — Cron schedule (5-field format)
- `backup.active_boost` — Boolean flag for boost mode
- `backup.viewer_heartbeat` — ISO datetime of last heartbeat

**Default Schedule:** `0 */6 * * *` (every 6 hours at minute 0)

---

## 4. AI MODEL CONFIGURATION

### Endpoint: `/api/ai/config`
**File:** `/home/phenix/projects/tele-private/repo/dev/src/web/routes_ai.py` (lines 242-285)

**Method:** GET  
**Auth:** Any authenticated user  
**Response (Non-Master):**
```json
{
  "enabled": true|false,
  "model": "qwen3-next-80b"
}
```
**Response (Master/Admin):**
```json
{
  "vision": {
    "provider": "local|remote",
    "api_url": "http://...",
    "api_key_set": true|false,
    "model_name": "glm-ocr",
    "fallback_url": "...",
    "fallback_model": "gemma3:27b"
  },
  "chat": {
    "provider": "local|remote",
    "api_url": "http://...",
    "api_key_set": true|false,
    "model_name": "qwen3-next-80b",
    "fallback_url": "",
    "fallback_model": ""
  },
  "embedding": {
    "api_url": "http://host.docker.internal:11434",
    "model_name": "qwen3-embedding:8b"
  },
  "tts": {
    "api_url": "http://host.docker.internal:8880/v1",
    "model_name": "kokoro"
  },
  "transcription": {
    "api_url": "http://host.docker.internal:8080",
    "enabled": true,
    "rate_limit": "2",
    "batch_size": "50"
  },
  "vault": {
    "api_url": "http://host.docker.internal:8200",
    "api_token_set": true|false,
    "enabled": false
  },
  "system_prompt": "You are a data analysis assistant..."
}
```

### Endpoint: `/api/admin/ai-config`
**File:** `/home/phenix/projects/tele-private/repo/dev/src/web/routes_ai.py` (lines 288-297)

**Method:** PUT  
**Auth:** Master role required  
**Request Body:** Any key-value pairs with prefixes:
- `ai.vision.*`
- `ai.chat.*`
- `ai.embedding.*`
- `ai.tts.*`
- `ai.transcription.*`
- `ai.vault.*`
- `ai.system_prompt`

**Behavior:** Bulk updates all matching keys to `app_settings` table. Non-matching keys ignored.

### Endpoint: `/api/admin/ai-config/test`
**File:** `/home/phenix/projects/tele-private/repo/dev/src/web/routes_ai.py` (lines 300-336)

**Method:** POST  
**Auth:** Master role required  
**Request Body:**
```json
{
  "api_url": "http://localhost:8080/v1",
  "api_key": "...",
  "test_type": "openai|whisper|vault"
}
```
**Behavior:**
- Tests connectivity to AI endpoint
- For `openai`: Tries `/models` or `/health` endpoint
- For `whisper`: GETs `/health` endpoint
- For `vault`: GETs `/profiles` endpoint
- Returns `{status: "ok"|"error", message: "..."}`

### AI Model Configuration Defaults (routes_ai.py, lines 32-71)

**Vision (OCR) Model:**
- Primary: `glm-ocr` at `http://host.docker.internal:8081/v1`
- Fallback: `gemma3:27b` at Ollama URL

**Chat Model:**
- Primary: `qwen3-next-80b` at Ollama URL
- Fallback: None (empty)

**Embedding Model:**
- URL: Ollama URL (default `http://host.docker.internal:11434`)
- Model: `qwen3-embedding:8b` (from `OLLAMA_EMBED_MODEL` env var)
- Batch size: 50 (from `OLLAMA_EMBED_BATCH` env var)

**Transcription:**
- URL: `http://host.docker.internal:8080` (Whisper-compatible)
- Rate limit: 2 requests/sec
- Batch size: 50

**TTS:**
- URL: `http://host.docker.internal:8880/v1`
- Model: `kokoro`

**System Prompt (lines 60-70):**
```
"You are a data analysis assistant for a Telegram archive viewer. 
Your role is to process, summarize, and analyze archived chat messages 
from organizational channels..."
```

---

## 5. RELATED AI ENDPOINTS

| Endpoint | Method | Purpose | Lines |
|----------|--------|---------|-------|
| `/api/ai/chat` | POST | Proxy chat requests to configured LLM | 159 |
| `/api/ai/ocr/{chat_id}/{message_id}` | POST | OCR single message | 344 |
| `/api/ai/ocr-batch/{chat_id}` | POST | Batch OCR messages | 422 |
| `/api/ai/annotate/{chat_id}/{message_id}` | POST | Annotate message with AI | 496 |
| `/api/semantic/embed` | POST | Trigger embedding for messages | 587 |
| `/api/semantic/search` | GET | Search using embeddings | 624 |

---

## 6. CONFIG FILE ENVIRONMENT VARIABLES

### File: `/home/phenix/projects/tele-private/repo/dev/src/config.py`

**Listener Vars:**
```bash
LISTENER_MODE=auto|always|off       # Activation mode (line 169)
LISTENER_GRACE_PERIOD=300           # Seconds before stopping after all viewers disconnect (line 179)
LISTEN_EDITS=true                   # Apply text edits (line 183)
LISTEN_DELETIONS=true               # Delete messages (line 188)
LISTEN_NEW_MESSAGES=true            # Save new messages real-time (line 193)
LISTEN_NEW_MESSAGES_MEDIA=false     # Download media real-time (line 198)
LISTEN_CHAT_ACTIONS=true            # Track chat metadata (line 202)
```

**Backup Vars:**
```bash
SCHEDULE=0 */6 * * *                # Cron schedule (line 28)
CHECKPOINT_INTERVAL=1               # Batch checkpoint frequency (line 39)
```

**Mass Operation Protection:**
```bash
MASS_OPERATION_THRESHOLD=10         # Max ops before block (line 227)
MASS_OPERATION_WINDOW_SECONDS=30    # Sliding window (line 228)
MASS_OPERATION_BUFFER_DELAY=2.0     # DEPRECATED (line 229)
```

**AI Vars:**
```bash
AI_API_KEY=                         # API key (line 258)
AI_BASE_URL=https://api.z.ai/...    # Base URL (line 259)
AI_MODEL=GLM-5                      # Model name (line 260)
OLLAMA_URL=http://host.docker.internal:11434  # Embeddings URL (line 266)
OLLAMA_EMBED_MODEL=qwen3-embedding:8b  # Embedding model (line 267)
OLLAMA_EMBED_BATCH=50               # Batch size (line 269)
```

---

## 7. REAL-TIME NOTIFICATIONS

### Module: `/home/phenix/projects/tele-private/repo/dev/src/realtime.py`

**RealtimeNotifier (lines 40-153):**
- Detects database type (PostgreSQL vs SQLite)
- PostgreSQL: Uses NOTIFY/LISTEN
- SQLite: Uses HTTP webhook to `/internal/push` endpoint

**RealtimeListener (lines 155-250):**
- Listens for PostgreSQL NOTIFY or HTTP push events
- Callback support for real-time updates to viewer

**Notification Types (lines 31-37):**
- `NEW_MESSAGE`
- `EDIT`
- `DELETE`
- `CHAT_UPDATE`

---

## 8. DATABASE STORAGE

**Table:** `app_settings` (key-value store)

**Listener-Related Keys:**
- `backup.schedule` — Cron string
- `backup.active_boost` — "true"/"false"
- `backup.viewer_heartbeat` — ISO datetime

**AI Config Keys (Prefixes):**
- `ai.vision.*` (provider, api_url, api_key, model_name, fallback_url, fallback_model)
- `ai.chat.*` (provider, api_url, api_key, model_name, fallback_url, fallback_model)
- `ai.embedding.*` (api_url, model_name)
- `ai.tts.*` (api_url, model_name)
- `ai.transcription.*` (api_url, enabled, rate_limit, batch_size)
- `ai.vault.*` (api_url, api_token, enabled)
- `ai.system_prompt`

---

## 9. KEY FINDINGS

### Listener Status
✓ Listener has three modes: `auto` (viewer-triggered), `always` (always on), `off` (disabled)  
✓ Grace period (default 300s) allows listener to stay alive after viewers disconnect  
✓ No direct endpoint to start/stop listener — only status check  
✓ ListenerManager checks viewer count from WebSocket ConnectionManager  

### Backup Config
✓ Schedule stored in DB, defaults to `0 */6 * * *` (every 6 hours)  
✓ `active_boost` flag indicates boost mode but no details on what it does  
✓ Heartbeat endpoint tracks viewer presence (used for backup boost?)  
✓ Cron validation enforced (5-field format)  

### AI Model Configuration
✓ Three-tier approach: primary model, fallback model, system prompt  
✓ Vision (OCR), Chat, Embedding, TTS, Transcription, Vault all configurable  
✓ Supports local (Ollama) and remote (OpenAI-compatible) models  
✓ Test endpoint available to validate connectivity  
✓ All config stored in app_settings table (runtime configurable, persisted)  
✓ Non-master users only see model name and enabled status  

### Zero-Footprint Protection
✓ Mass operation detector uses sliding window (default 10 ops in 30s)  
✓ Blocked chats are rate-limited per window period  
✓ Operations held in buffer before applying (prevents half-applied batches)  

---

## Unresolved Questions

1. What does `active_boost` flag actually control? No scheduler config for boost found.
2. How does `backup_heartbeat` trigger boost logic? Need to check backup scheduler code.
3. Are AI config fallback models actually used if primary fails? Need to check chat/vision request handlers.
4. How is embedding API format auto-detected (Ollama vs OpenAI)? `/api/embed` vs `/v1/embeddings`?
5. Does `LISTEN_DELETIONS=true` actually delete messages from DB, or just log them?

