# Research Report: Search UX, DB Indexing, AI Panel

**Date:** 2026-03-11
**Scope:** Telegram Archive Viewer (Vue 3 CDN + FastAPI + SQLite/PostgreSQL)

---

## Topic 1: Search UX - Pinned/Tacked Search Bar

### Current State

- Chat sidebar search exists: `GET /api/chats?search=` uses `ILIKE` on title/first_name/last_name/username
- Message-level text search: `GET /api/chats/{id}/messages?search=` also uses `ILIKE %term%` on `messages.text`
- No FTS, no highlighting, no cross-chat message search
- Search is already in the sidebar for chat filtering; message search is per-chat only

### Recommended Pattern: Sticky Search Bar Pinned to Message Area Top

**Layout:**
```
+---------------------+------------------------------------------+
| Chat List           | [x] Search bar (sticky, top of msg area) |
| [sidebar search]    |------------------------------------------|
|                     | Message 1 (match **highlighted**)         |
|                     | Message 2                                 |
|                     | Message 3 (match **highlighted**)         |
+---------------------+------------------------------------------+
```

**Behavior:**
1. Toggle via `Ctrl+F` / magnifying glass icon in chat header
2. Bar slides down from top of message area (CSS `position: sticky; top: 0; z-index: 10`)
3. Shows match count + up/down arrows to jump between matches
4. Close via Escape or X button
5. Optional "Search all chats" toggle that switches to cross-chat results view

**Debounce vs Submit:**
- Use **debounced search-as-you-type** (300ms debounce) for best UX
- Trigger API call after debounce; show loading spinner
- For cross-chat search (heavier), use explicit submit (Enter key) since it scans more data
- Cancel in-flight requests on new keystrokes (`AbortController`)

**Highlighting Implementation (Vue 3 CDN):**
```javascript
// Utility function for Vue template
function highlightText(text, query) {
  if (!query || !text) return text;
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return text.replace(new RegExp(`(${escaped})`, 'gi'), '<mark>$1</mark>');
}
```
- Apply in message rendering via `v-html` with sanitized input
- CSS: `mark { background: #ffeb3b; color: #000; border-radius: 2px; padding: 0 2px; }`

**Search Scope UX:**
- Default: current chat only (fast, uses per-chat index)
- Toggle: "Search all chats" checkbox/pill in the search bar
- Cross-chat results show as a list: `[ChatName] message preview...` -- clicking navigates to that chat+message

### Key Decisions

| Decision | Recommendation | Rationale |
|----------|---------------|-----------|
| Position | Sticky top of message area | Standard in Telegram/WhatsApp/Slack |
| Trigger | Ctrl+F + icon button | Muscle memory; discoverable |
| Debounce | 300ms for in-chat; submit for cross-chat | Balance responsiveness vs server load |
| Scope default | Current chat | Faster; most common use case |
| Navigation | Up/down arrows + count | Telegram desktop pattern |
| Highlight | Client-side `<mark>` tag | Server returns matches; client highlights |

---

## Topic 2: Smart Indexing / DB Optimization

### Current State (from `adapter.py` + `models.py`)

**Existing indexes on `messages`:**
- `idx_messages_chat_id` (chat_id)
- `idx_messages_date` (date)
- `idx_messages_sender_id` (sender_id)
- `idx_messages_chat_date_desc` (chat_id, date DESC) -- pagination
- `idx_messages_chat_pinned` (chat_id, is_pinned)
- `idx_messages_reply_to` (chat_id, reply_to_msg_id)
- `idx_messages_topic` (chat_id, reply_to_top_id)

**Current search:** `ILIKE %term%` on `messages.text` -- full table scan, cannot use B-tree indexes

**Problem:** `ILIKE '%foo%'` on potentially millions of messages is O(n). Unusable at scale.

### SQLite: FTS5 Virtual Table

**Recommended approach -- separate FTS5 virtual table:**

```sql
-- Virtual table for full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    content='messages',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

-- Triggers to keep FTS in sync with messages table
-- NOTE: messages has composite PK (id, chat_id), so rowid = SQLite internal rowid
CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
```

**Query pattern:**
```sql
-- Per-chat search
SELECT m.id, m.chat_id, m.text, m.date, m.sender_id
FROM messages m
JOIN messages_fts fts ON m.rowid = fts.rowid
WHERE fts.text MATCH ? AND m.chat_id = ?
ORDER BY m.date DESC
LIMIT 50;

-- Cross-chat search
SELECT m.id, m.chat_id, m.text, m.date, snippet(messages_fts, 0, '<b>', '</b>', '...', 32) as snippet
FROM messages_fts fts
JOIN messages m ON m.rowid = fts.rowid
WHERE fts.text MATCH ?
ORDER BY rank
LIMIT 50;
```

**Composite PK caveat:** SQLite assigns internal `rowid` even for composite PKs. The content-sync approach above uses that internal rowid. Alternatively, store `message_id` and `chat_id` as extra columns in the FTS table for explicit joins.

### PostgreSQL: tsvector + GIN Index

**Add tsvector column to messages:**
```sql
-- Add column
ALTER TABLE messages ADD COLUMN text_search tsvector;

-- Populate
UPDATE messages SET text_search = to_tsvector('simple', coalesce(text, ''));

-- GIN index for fast lookup
CREATE INDEX idx_messages_text_search ON messages USING GIN (text_search);

-- Trigger to auto-update on insert/update
CREATE OR REPLACE FUNCTION messages_text_search_trigger() RETURNS trigger AS $$
BEGIN
    NEW.text_search := to_tsvector('simple', coalesce(NEW.text, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trig_messages_text_search
    BEFORE INSERT OR UPDATE OF text ON messages
    FOR EACH ROW EXECUTE FUNCTION messages_text_search_trigger();
```

**Why `'simple'` config:** Telegram messages are multilingual, contain slang, emoji, URLs. The `simple` config does basic tokenization without language-specific stemming, which is more predictable for chat text. Alternative: `'english'` if corpus is mostly English.

**Query pattern:**
```sql
-- Per-chat search with ranking
SELECT id, chat_id, text, date,
       ts_headline('simple', text, plainto_tsquery('simple', $1),
                   'StartSel=<mark>, StopSel=</mark>, MaxWords=50, MinWords=20') as highlighted
FROM messages
WHERE chat_id = $2 AND text_search @@ plainto_tsquery('simple', $1)
ORDER BY ts_rank(text_search, plainto_tsquery('simple', $1)) DESC
LIMIT 50;
```

### Background Indexing Worker

**Pattern:** asyncio task that runs at startup and after each backup cycle.

```python
# In web/main.py or a dedicated src/indexer.py

async def rebuild_fts_index(db: DatabaseAdapter):
    """Rebuild FTS index for messages without FTS entries."""
    if db.db_type == "sqlite":
        await db.execute_raw("INSERT INTO messages_fts(messages_fts) VALUES('rebuild');")
    else:  # postgresql
        await db.execute_raw("""
            UPDATE messages SET text_search = to_tsvector('simple', coalesce(text, ''))
            WHERE text_search IS NULL;
        """)

async def index_worker(db: DatabaseAdapter):
    """Background worker: index new messages periodically."""
    while True:
        try:
            await rebuild_fts_index(db)
        except Exception as e:
            logger.error(f"FTS index rebuild failed: {e}")
        await asyncio.sleep(300)  # Re-index every 5 minutes
```

**Integration points:**
- Start as `asyncio.create_task()` in FastAPI `startup` event
- Also trigger after `backup_complete` webhook/event
- For PostgreSQL, the trigger handles real-time; worker is just for catching up

### Schema Decision: Separate FTS vs Inline

| Approach | SQLite | PostgreSQL |
|----------|--------|------------|
| **Recommended** | Separate FTS5 virtual table | Inline tsvector column + GIN index |
| **Reason** | SQLite FTS5 is purpose-built, very fast, supports `MATCH` and ranking | tsvector is native PG; GIN index is standard approach |
| **Sync method** | Triggers (INSERT/UPDATE/DELETE) | BEFORE INSERT/UPDATE trigger |
| **Rebuild** | `INSERT INTO fts(fts) VALUES('rebuild')` | `UPDATE SET text_search = to_tsvector(...)` |
| **Storage overhead** | ~30-50% of text column size | ~20-30% of text column size |

### Migration Strategy

1. Add migration in `src/db/migrate.py` to create FTS table/column
2. Run full rebuild on first startup (can be slow for large DBs -- show progress)
3. Triggers keep it in sync after initial build
4. Fall back to `ILIKE` if FTS is not yet built (graceful degradation)

### New API Endpoint

```
GET /api/search?q=term&chat_id=123&limit=50&offset=0
```
- `chat_id` optional: if omitted, search all chats (cross-chat)
- Returns: `{ results: [{id, chat_id, chat_title, text, date, snippet, sender_name}], total, has_more }`
- Use FTS if available, fall back to ILIKE

---

## Topic 3: AI Assistant Side Panel (Skeletal Architecture)

**Note:** This should be developed on a separate feature branch.

### UX Design

```
+---------------------+-----------------------------------+------------------+
| Chat List           | Messages                          | AI Panel (slide) |
|                     |                                   |                  |
|                     |                                   | [AI Chat]        |
|                     |                                   | User: summarize  |
|                     |                                   | AI: Here's a...  |
|                     |                                   |                  |
|                     |                                   | [input] [send]   |
+---------------------+-----------------------------------+------[pin icon]--+
```

**Panel behavior:**
- Toggle: icon button in top-right of message area header
- Slide-in from right, 350px wide, over or pushing message area
- "Pin/tack" button (thumbtack icon): when pinned, panel stays open across chat switches
- When unpinned, panel closes on chat switch
- Panel state persisted to localStorage: `{open, pinned, width}`
- Resizable via drag handle on left edge

### Frontend Component Structure (Vue 3 CDN)

```
components/
  ai-panel/
    ai-panel-container.js    -- Main panel wrapper, open/close/pin logic
    ai-chat-messages.js      -- Chat message list (user + AI messages)
    ai-chat-input.js         -- Text input + send button
    ai-tool-results.js       -- Renders tool outputs (OCR results, math, etc.)
    ai-settings-tab.js       -- Provider config (model selection, API key input)
```

### Backend Architecture

**New FastAPI router:** `src/web/ai_routes.py`

```python
# Endpoints:
POST /api/ai/chat          # Send message, get AI response (streaming SSE)
GET  /api/ai/config        # Get current AI provider config
PUT  /api/ai/config        # Update AI provider config (admin only)
POST /api/ai/tools/ocr     # OCR a specific media file
POST /api/ai/tools/math    # Evaluate math expression
```

**Request flow:**
1. Frontend sends user message + current chat context (last N messages)
2. Backend constructs prompt with chat context
3. Backend calls configured AI provider
4. Response streamed back via SSE (Server-Sent Events)

### Chat Context Integration

```python
async def build_chat_context(db, chat_id: int, message_count: int = 50) -> str:
    """Build context string from recent chat messages."""
    messages = await db.get_messages(chat_id, limit=message_count, offset=0)
    context_lines = []
    for msg in messages:
        sender = msg.get("sender_name", "Unknown")
        text = msg.get("text", "")
        date = msg.get("date", "")
        context_lines.append(f"[{date}] {sender}: {text}")
    return "\n".join(context_lines)
```

**Context window management:**
- Default: last 50 messages from current chat
- User can select specific messages to include (shift+click to select range)
- Truncate to model's context limit (estimate tokens: chars / 4)

### Tool Architecture

| Tool | Implementation | Use Case |
|------|---------------|----------|
| **OCR** | Tesseract (local) or cloud API (Google Vision) | Extract text from images/screenshots in chat |
| **Math** | `sympy` (local) | Evaluate math expressions, solve equations |
| **Summarize** | AI model itself | Summarize long conversations |
| **Translate** | AI model or `deep-translator` | Translate messages |
| **Search** | Internal FTS (Topic 2) | "Find messages about X" via function calling |
| **Long Memory** | Future: vector store (ChromaDB/pgvector) | Remember past conversations across sessions |

**Tool calling pattern:**
```python
TOOLS = [
    {"name": "ocr", "description": "Extract text from an image", "parameters": {"media_id": "string"}},
    {"name": "math", "description": "Evaluate math expression", "parameters": {"expression": "string"}},
    {"name": "search_messages", "description": "Search chat messages", "parameters": {"query": "string", "chat_id": "int"}},
]
```

### Model Configuration

**DB storage:** Use existing `app_settings` table for AI config.

```python
# Settings keys:
# ai_provider: "ollama" | "openai" | "anthropic" | "disabled"
# ai_model: model name (e.g., "llama3", "gpt-4o-mini", "claude-sonnet-4-20250514")
# ai_api_key: encrypted API key (for cloud providers)
# ai_base_url: custom endpoint (for ollama: "http://localhost:11434")
# ai_max_context: max messages to include as context (default: 50)
# ai_system_prompt: custom system prompt override
```

**Provider abstraction:**
```python
class AIProvider(ABC):
    @abstractmethod
    async def chat(self, messages: list[dict], tools: list[dict] = None) -> AsyncIterator[str]: ...

class OllamaProvider(AIProvider): ...
class OpenAIProvider(AIProvider): ...    # Works with any OpenAI-compatible API
class AnthropicProvider(AIProvider): ...
```

### Admin Settings UI

- New tab in admin panel: "AI Assistant"
- Fields: provider dropdown, model name, API key (masked), base URL, test connection button
- Toggle to enable/disable AI panel globally
- Per-user setting: admin can allow/disallow AI access per viewer account

### Implementation Phases (for branch planning)

1. **Phase 1:** Panel skeleton (open/close/pin, empty chat UI) -- no backend
2. **Phase 2:** Backend routes + provider abstraction + Ollama support
3. **Phase 3:** Chat context integration + streaming responses
4. **Phase 4:** Tool calling (OCR, math, search)
5. **Phase 5:** Admin config UI + cloud provider support
6. **Phase 6:** Vector store for long memory (if needed)

### Security Considerations

- API keys stored encrypted in DB (use Fernet symmetric encryption with env-var master key)
- AI endpoints require authentication (same auth as viewer)
- Rate limit AI requests per user (e.g., 20/min)
- Sanitize AI responses before rendering (XSS prevention)
- Admin-only access to config endpoints
- Chat context respects per-user chat whitelists (viewer accounts only see allowed chats)

---

## Cross-Topic Dependencies

```
Topic 2 (FTS) ──> Topic 1 (Search UX uses FTS backend)
Topic 2 (FTS) ──> Topic 3 (AI search tool uses FTS)
Topic 1 (Search bar) ──> Topic 3 (AI panel can reuse search results display)
```

**Implementation order:** Topic 2 (FTS indexing) first, then Topic 1 (search UX), then Topic 3 (AI panel on separate branch).

---

## Unresolved Questions

1. **FTS language config:** Should we support per-chat language detection for better stemming, or stick with `simple` tokenizer universally?
2. **SQLite FTS5 + composite PK:** Need to verify that `rowid`-based content sync works correctly with the `(id, chat_id)` composite PK. May need to store `message_id` + `chat_id` as explicit FTS columns instead.
3. **AI panel scope:** Should the AI panel be available to all viewer accounts or admin-only initially?
4. **OCR provider:** Tesseract (free, local, lower quality) vs cloud API (cost, better quality)? Could default to Tesseract with cloud as optional upgrade.
5. **Cross-chat search performance:** For very large archives (1M+ messages), even FTS cross-chat search may be slow. Consider pagination + async result streaming.
6. **AI streaming:** SSE vs WebSocket for AI responses? SSE is simpler and sufficient for unidirectional streaming; WebSocket already used for real-time message updates -- could reuse.
