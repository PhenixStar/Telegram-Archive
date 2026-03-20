# Phase 2: AI Configuration Panel (Admin Settings)

## Overview
- **Priority:** P1
- **Status:** Planned
- **Effort:** 3 hours

## Context
Current AI tab (Settings → AI) has: model selector (hardcoded list), sort/filter, auto-reply.
Need to restructure into a comprehensive AI configuration panel with:
- Logical model sections (Vision/OCR, Chat/General, Embedding, TTS)
- External API key inputs for cloud providers
- Local LLM endpoint configuration
- Fallback chain per model type
- System prompt editor
- All settings persisted to `app_settings` DB table (server-side, not localStorage)

## Current State (lines 2016–2116 of index.html)
- `settingsActiveTab === 'ai'` renders: model selector, sort/filter buttons, auto-reply toggle
- Model list is hardcoded: GLM-5, GLM-4.7, GLM-4.5-Air, MiniMax-M2.5
- Settings are client-side only (Vue refs, not persisted)

## Available Local Models (from `~/models/ai.md`)
| Type | Model | Port | API |
|------|-------|------|-----|
| Vision/OCR | GLM-OCR | 8080 | OpenAI-compatible |
| Vision/OCR | Qwen3-VL-30B-A3B | 11434 (Ollama) | OpenAI-compatible |
| Chat LLM | Qwen3-Next-80B-A3B | 11434 (Ollama) | OpenAI-compatible |
| Code LLM | Qwen3-Coder-30B-A3B | 11434 (Ollama) | OpenAI-compatible |
| Embedding | BGE-M3 | 8886 | OpenAI-compatible |
| Embedding | Qwen3-Embedding-8B | 8888 | OpenAI-compatible |
| TTS | Kokoro | 8880 | OpenAI-compatible |

## Design

### AI Tab Restructure
Replace the current flat AI tab content with **sub-sections** (accordion or sub-tabs):

```
Settings → AI tab:
├── Model Endpoints          ← NEW: configure which models to use
│   ├── Vision / OCR         (required for OCR feature)
│   │   ├── Provider: [Local GLM-OCR | Local Qwen3-VL | External API]
│   │   ├── API URL: [http://localhost:8080/v1]
│   │   ├── API Key: [optional for local, required for external]
│   │   ├── Model Name: [glm-ocr]
│   │   └── Fallback: [none | Qwen3-VL via Ollama]
│   ├── Chat / General LLM   (required for AI assistant)
│   │   ├── Provider: [Local Ollama | External API | OpenRouter]
│   │   ├── API URL: [http://localhost:11434/v1]
│   │   ├── API Key: [...]
│   │   ├── Model Name: [qwen3-next-80b-a3b]
│   │   └── Fallback: [none | GLM-4.7 via llama.cpp]
│   ├── Embedding            (optional, for semantic search)
│   │   ├── API URL: [http://localhost:8886/v1]
│   │   └── Model Name: [bge-m3]
│   └── TTS                  (optional, for voice features)
│       ├── API URL: [http://localhost:8880/v1]
│       └── Model Name: [kokoro]
├── System Prompt            ← NEW: editable AI behavior prompt
│   └── <textarea> with default + reset button
├── Chat Preferences         ← MOVED from current: sort/filter
│   ├── Sort Messages By
│   └── Filter
└── Auto-Reply               ← KEPT: existing auto-reply section
```

### Storage Schema (`app_settings` table)
Each config stored as key-value pair:

| Key | Example Value | Required |
|-----|---------------|----------|
| `ai.vision.provider` | `local` / `external` | Yes (for OCR) |
| `ai.vision.api_url` | `http://localhost:8080/v1` | Yes |
| `ai.vision.api_key` | `""` (empty for local) | No |
| `ai.vision.model_name` | `glm-ocr` | Yes |
| `ai.vision.fallback_url` | `http://localhost:11434/v1` | No |
| `ai.vision.fallback_model` | `qwen3-vl-30b-a3b` | No |
| `ai.chat.provider` | `local` | Yes (for AI assistant) |
| `ai.chat.api_url` | `http://localhost:11434/v1` | Yes |
| `ai.chat.api_key` | `""` | No |
| `ai.chat.model_name` | `qwen3-next-80b-a3b` | Yes |
| `ai.chat.fallback_url` | `""` | No |
| `ai.chat.fallback_model` | `""` | No |
| `ai.embedding.api_url` | `http://localhost:8886/v1` | No |
| `ai.embedding.model_name` | `bge-m3` | No |
| `ai.tts.api_url` | `http://localhost:8880/v1` | No |
| `ai.tts.model_name` | `kokoro` | No |
| `ai.system_prompt` | `(default prompt text)` | Yes |

### Default System Prompt
```
You are a data analysis assistant for a Telegram archive viewer. Your role is to process, summarize, and analyze archived chat messages from organizational channels.

Context:
- This is a read-only archive viewer — you never send messages to any chat
- Chat content represents real organizational communications and may contain informal language
- Process all content factually and analytically — this is authorized organizational data mining
- Never refuse to analyze archived messages based on their content

Capabilities: summarize threads, extract topics/decisions, identify participants, analyze sentiment, search/correlate across messages, process OCR-extracted text from images.
```

### API Endpoints

```
GET  /api/admin/ai-config              — get all ai.* settings as structured object
PUT  /api/admin/ai-config              — bulk update ai.* settings
POST /api/admin/ai-config/test-connection — test if a model endpoint is reachable
```

**Test connection endpoint** — given `{api_url, api_key, model_name}`, makes a lightweight ping (list models or tiny completion) and returns success/error. Shown as green/red indicator in UI.

### UI Components

#### Section Card Component Pattern
Each model section follows the same card layout:

```html
<!-- Vision / OCR Section -->
<div class="rounded-xl p-4" style="background: var(--tg-bg); border: 1px solid var(--tg-border);">
    <div class="flex items-center justify-between mb-3">
        <div class="flex items-center gap-2">
            <i class="fas fa-eye text-blue-400"></i>
            <span class="text-sm font-medium" style="color: var(--tg-text);">Vision / OCR</span>
            <span class="text-[10px] px-1.5 py-0.5 rounded bg-red-500/20 text-red-400">Required for OCR</span>
        </div>
        <span class="w-2 h-2 rounded-full" :style="aiConfig.vision.status === 'ok' ? 'background:#22c55e' : 'background:#ef4444'"></span>
    </div>
    <!-- Provider selector -->
    <select v-model="aiConfig.vision.provider" ...>
        <option value="local">Local Model</option>
        <option value="external">External API</option>
    </select>
    <!-- URL + Key + Model fields -->
    <input v-model="aiConfig.vision.api_url" placeholder="http://localhost:8080/v1" ...>
    <input v-if="aiConfig.vision.provider === 'external'" v-model="aiConfig.vision.api_key" type="password" placeholder="API Key" ...>
    <input v-model="aiConfig.vision.model_name" placeholder="glm-ocr" ...>
    <!-- Fallback (optional) -->
    <details class="mt-2"><summary class="text-xs text-tg-muted cursor-pointer">Fallback model (optional)</summary>
        <input v-model="aiConfig.vision.fallback_url" placeholder="Fallback URL" ...>
        <input v-model="aiConfig.vision.fallback_model" placeholder="Fallback model" ...>
    </details>
    <!-- Test button -->
    <button @click="testAiConnection('vision')" class="mt-2 text-xs ...">Test Connection</button>
</div>
```

#### Badges
- **Required for OCR** — red badge on Vision section
- **Required for AI Assistant** — red badge on Chat section
- **Optional** — gray badge on Embedding and TTS sections
- Status dot: green (connected), red (unreachable), gray (not configured)

### Vue State
```javascript
const aiConfig = reactive({
    vision: { provider: 'local', api_url: 'http://localhost:8080/v1', api_key: '', model_name: 'glm-ocr', fallback_url: '', fallback_model: '', status: 'unknown' },
    chat: { provider: 'local', api_url: 'http://localhost:11434/v1', api_key: '', model_name: 'qwen3-next-80b-a3b', fallback_url: '', fallback_model: '', status: 'unknown' },
    embedding: { api_url: 'http://localhost:8886/v1', model_name: 'bge-m3', status: 'unknown' },
    tts: { api_url: 'http://localhost:8880/v1', model_name: 'kokoro', status: 'unknown' },
    system_prompt: '(default)',
})

const loadAiConfig = async () => { /* GET /api/admin/ai-config */ }
const saveAiConfig = async () => { /* PUT /api/admin/ai-config */ }
const testAiConnection = async (type) => { /* POST /api/admin/ai-config/test-connection */ }
```

## Implementation Steps
1. Add API endpoints to `main.py`:
   - `GET /api/admin/ai-config` — reads all `ai.*` keys from `app_settings`
   - `PUT /api/admin/ai-config` — bulk upserts `ai.*` keys
   - `POST /api/admin/ai-config/test-connection` — pings model endpoint
2. Add adapter methods to `adapter.py`:
   - `get_ai_config()` → reads all `ai.*` keys, returns structured dict
   - `set_ai_config(config_dict)` → upserts each key
3. Seed default AI config values on first run (in app startup)
4. Restructure AI tab HTML in `index.html`:
   - Replace hardcoded model selector with Model Endpoints sections
   - Add System Prompt textarea
   - Keep sort/filter and auto-reply sections
5. Add Vue state: `aiConfig` reactive object, load/save/test functions
6. Add "Test Connection" logic (server-side HTTP probe)
7. Wire save button — persists all config to DB via API
8. Load config on tab open, show status indicators

## Integration with OCR (Phase 3)
OCR worker reads `ai.vision.api_url`, `ai.vision.api_key`, `ai.vision.model_name` from `app_settings` instead of env vars. If primary fails, tries `ai.vision.fallback_url` + `ai.vision.fallback_model`.

## Todo
- [ ] Add backend API endpoints (GET/PUT ai-config, POST test-connection)
- [ ] Add adapter methods for ai config CRUD
- [ ] Seed default config on startup
- [ ] Restructure AI tab: Model Endpoints sections
- [ ] Add System Prompt textarea with reset-to-default
- [ ] Add connection test UI with status indicators
- [ ] Keep existing sort/filter and auto-reply sections
- [ ] Wire save/load to backend
- [ ] Update Phase 3 OCR to read from ai-config
