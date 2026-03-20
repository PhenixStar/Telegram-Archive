### Backend Feature Delivered -- Semantic Search via Ollama Embeddings (2026-03-13)

**Stack Detected**   : Python FastAPI (async) + SQLAlchemy async ORM + SQLite/PostgreSQL
**Files Added**      : none
**Files Modified**   :
- `src/db/models.py` -- added `MessageEmbedding` model
- `src/db/adapter.py` -- added 4 methods: `get_unembedded_messages`, `store_embeddings`, `get_embedding_count`, `semantic_search`; added `selectinload` import and `MessageEmbedding` model import
- `src/db/__init__.py` -- exported `MessageEmbedding`
- `src/web/main.py` -- added `_get_embedding_config()` helper + 3 endpoints

**Key Endpoints/APIs**

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/semantic/status?chat_id= | Embedding progress (total vs embedded count) |
| POST | /api/semantic/embed?chat_id=&limit= | Trigger batch embedding generation (master only) |
| GET | /api/semantic/search?q=&chat_id=&limit= | Semantic search using cosine similarity |

**Design Notes**
- Pattern: Read embedding config from `app_settings` (keys `ai.embedding.api_url`, `ai.embedding.model_name`) -- same pattern as vision/chat AI config
- Ollama URL: `_get_embedding_config()` strips `/v1` suffix from stored URL to call native `/api/embed` endpoint
- Embedding storage: JSON-serialized float arrays in TEXT column (portable across SQLite/PostgreSQL)
- Cosine similarity: pure-Python computation (no numpy dependency)
- Batch embedding: sends multiple texts in single Ollama request for efficiency
- Text truncated to 2000 chars before embedding to stay within model context window
- Access control: status+search require `require_auth`, embed trigger requires `require_master`
- Composite PK (message_id, chat_id) with FK cascade to messages table

**Model: `MessageEmbedding`**
- Composite PK: `(message_id, chat_id)`
- FK cascade to `messages` table
- Index on `chat_id` for per-chat queries
- Auto-creates via `Base.metadata.create_all(checkfirst=True)` on startup

**Validation**
- All 4 files pass `ast.parse()` syntax check (Python 3.12 on host; 3.14 syntax compatible)
