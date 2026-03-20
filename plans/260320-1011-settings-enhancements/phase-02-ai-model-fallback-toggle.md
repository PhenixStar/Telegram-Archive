---
phase: 2
title: "AI Model Fallback Local/Remote Toggle"
status: pending
priority: P2
effort: 1h
---

# Phase 2: AI Model Fallback — Local/Remote Toggle

## Context
- [Parent plan](plan.md)
- AI config UI: `index.html:2925-3163`
- Backend: `routes_ai.py` — `/api/ai/config` GET, `/api/admin/ai-config` PUT
- `aiConfig` reactive object already has per-service fields: provider, api_url, api_key, model_name, fallback_*

## Overview
Add a clear "Fallback Strategy" control to each AI service section (Vision, Chat, Embedding, TTS, Transcription). Currently fallback fields exist but aren't prominently exposed. Add a toggle: "On primary failure, fallback to: [Local / Remote / None]".

## Key Insights
- Backend already persists fallback_provider, fallback_api_url, fallback_model in `app_settings` table via `aiConfig`
- Current UI shows these as separate input fields — hard to understand the relationship
- Simplification: group primary + fallback into a clear "Primary → Fallback" visual flow

## Requirements
- Per-service: toggle between "No fallback", "Fallback to Local (Ollama)", "Fallback to Remote"
- When "Local" selected: auto-fill fallback_api_url with `http://host.docker.internal:11434/v1` (common Ollama endpoint)
- When "Remote" selected: show fallback_api_url + fallback_api_key inputs
- Visual: primary config on top, fallback below with indented/dimmed style

## Implementation Steps
1. For each AI service section (Vision, Chat), add a "Fallback" subsection below primary config
2. Add radio buttons: None / Local (Ollama) / Remote
3. Conditional fields based on selection
4. Wire to existing `aiConfig.{service}_fallback_*` fields
5. Save via existing `saveAiConfig()` — no backend changes needed

## Todo
- [ ] Add fallback radio group to Vision section
- [ ] Add fallback radio group to Chat section
- [ ] Auto-fill Ollama URL when "Local" selected
- [ ] Show/hide fallback fields based on selection
- [ ] Test save + reload preserves fallback config

## Success Criteria
- User can set "Fallback to Local" and see Ollama URL pre-filled
- Saving config persists fallback settings
- Clear visual hierarchy: primary → fallback

## Risk
- Ollama URL may differ per deployment — the auto-fill is a sensible default, not guaranteed
