# Plan Review -- Scope & Complexity Critique
**Plan:** 260311-1742-viewer-advanced-features
**Date:** 2026-03-11
**Perspective:** YAGNI enforcer / Scope & Complexity Critic

---

## Finding 1: Phase 10 (AI Assistant Panel) is pure gold-plating with no user value
- **Severity:** Critical
- **Location:** Phase 10, entire phase
- **Flaw:** Builds full AI assistant skeleton (slide-in panel, provider ABC, admin config tab, SSE plumbing, 3 new files, new FastAPI router) that explicitly does nothing: "No functional AI calls in this phase." Provider abstraction (Ollama/OpenAI/Anthropic) is speculative -- no requirements exist for the "functional phase."
- **Failure scenario:** Engineering time spent building non-functional panel, provider ABC, admin config UI, mobile responsive behavior, localStorage persistence, pin/tack state. When actual AI requirements arrive, provider interface/message format/context strategy will need redesign, invalidating skeleton. Meanwhile 10 phases compete for edits to the same 4122-line index.html.
- **Evidence:** "This phase creates the SKELETON only -- functional AI integration comes later" (Phase 10, line 9). Three new files for a stub returning `{"error": "AI assistant not configured."}`.
- **Suggested fix:** Delete Phase 10. Implement AI panel when actual AI requirements and provider are defined. If demo needed, single UI toggle showing "Coming soon" -- no backend, no provider abstraction, no admin config.

## Finding 2: Phase 9 (FTS) dual-database abstraction is premature complexity
- **Severity:** High
- **Location:** Phase 9, Implementation Steps 1-2
- **Flaw:** Two completely different FTS implementations required: SQLite FTS5 (virtual table + content sync triggers + MATCH) and PostgreSQL tsvector (column + GIN + plainto_tsquery + batch backfill). Custom trigger SQL for both. Different query syntax in `search_messages_fts()`. Different snippet functions. Doubles testing surface.
- **Failure scenario:** Bug in SQLite FTS5 `content_rowid='rowid'` sync with composite PKs cannot be reproduced on PostgreSQL environment and vice versa. Plan acknowledges risk but offers only "test with sample data" -- no concrete dual-DB test plan.
- **Evidence:** Key Insights states as fact: "SQLite always has rowid even with composite PKs." Risk Assessment contradicts: "FTS5 content sync relies on internal rowid. Should work but needs verification."
- **Suggested fix:** Determine production DB. Implement FTS for that DB only. If both needed, ship ILIKE with index on `messages.text` as interim; defer FTS until measured performance problem with actual data.

## Finding 3: All 8 code phases edit a single 4122-line monolith with no conflict mitigation
- **Severity:** Critical
- **Location:** plan.md "File Ownership per Phase" + all phase files
- **Flaw:** Phases 3-8 and 10 all modify index.html. Phases 3, 5, 8, 9 all modify main.py. "File Ownership" table claims different regions (JS+HTML vs CSS only) but regions within the same file cannot be enforced. Phase 7 references "lines 36-160+" which become stale after Phases 3-6 insert hundreds of new lines.
- **Failure scenario:** Phase 3 inserts toast+settings modal near line 3800. Phase 4 inserts context menu at similar location. By Phase 7, all line references are shifted. Implementer for Phase 7 finds wrong code at documented line numbers.
- **Evidence:** plan.md line 54: "index.html is 4122 lines; single-file Vue 3 CDN app -- cannot split." Yet 7/10 phases add content to this file.
- **Suggested fix:** Execute strictly sequentially; each phase's PR merged before next begins. Remove all hardcoded line-number references from later phases; use search-based anchors ("after the showToast function definition" not "line 3800").

## Finding 4: Phase 5 (Permalink) "message not loaded" edge case is underspecified
- **Severity:** High
- **Location:** Phase 5, Implementation Steps step 6, Risk Assessment
- **Flaw:** Multi-step async flow (URL parse -> chat selection -> wait for messages -> check presence -> fetch single message -> get date -> jumpToDate -> wait for page load -> scroll) specified in a single bullet. No await/nextTick points, no retry, no timeout fallback.
- **Failure scenario:** Permalink to message in 100K-message chat. Frontend loads latest page, doesn't find target, fetches by ID, jumps to date page. scrollToMessage fires before new messages render; element doesn't exist. User sees random position, no highlight, no error.
- **Evidence:** "use jumpToDate-style logic to load that page, then scroll" -- no await, no nextTick, no fallback.
- **Suggested fix:** (a) Backend returns messages around the target (page centered on it), eliminating frontend multi-step dance, or (b) fully specify async flow with await points, nextTick guards, and "Message not found" toast fallback after timeout.

## Finding 5: Phase 8 highlight composition with linkify is a known XSS/DOM-corruption vector
- **Severity:** High
- **Location:** Phase 8, Implementation Steps step 5, Risk Assessment bullet 3
- **Flaw:** `highlightText(linkifyText(msg.text), searchHighlightTerm)` applies regex on HTML output. Regex will match inside `<a href="">` attributes when search term matches URL fragments ("com", "http", "class").
- **Failure scenario:** User searches "com". linkifyText produces `<a href="https://example.com">`. highlightText injects `<mark>` inside href attribute: `<a href="https://example.<mark>com</mark>">`. Breaks link; potential XSS vector with crafted search terms.
- **Evidence:** Risk Assessment: "apply after linkify but only on text nodes (simpler: apply after, test edge cases)" -- the "simpler" broken approach is what the implementation steps specify.
- **Suggested fix:** Specify text-node-only approach: DOM-based highlight walking text nodes after render, or apply highlight before linkify on raw text. Do not regex-replace on HTML output.

## Finding 6: Phase 3 backup interval setting has no runtime effect
- **Severity:** Medium
- **Location:** Phase 3, Implementation Steps step 5, Risk Assessment
- **Flaw:** UI saves interval to DB, but scheduler reads SCHEDULE env var on startup. No reload mechanism exists or is planned. Env var overrides DB on restart.
- **Failure scenario:** Admin changes backup interval from 6h to 1h. Toast confirms success. Backups still run every 6h. Server restart ignores DB value because env var takes precedence.
- **Evidence:** "scheduler reads SCHEDULE from env on startup. DB override requires restart or a reload mechanism" -- no step implements either.
- **Suggested fix:** Remove backup interval from settings UI (leave as env var), or implement scheduler hot-reload. A non-functional UI control is worse than none.

## Finding 7: Phase 7 scope exceeds value -- 3-4 light themes + auto-detect when zero demand cited
- **Severity:** Medium
- **Location:** Phase 7, entire phase
- **Flaw:** 3-4 new light themes, WCAG AA verification, form field contrast fixes across all elements, Flatpickr overrides, auto-detect, IIFE modification, color swatches, grouped selector. MEDIUM priority feature with HIGH-priority scope. ~10 total themes means every UI feature (Phases 4-8, 10) needs 10x theme testing.
- **Failure scenario:** Testing surface explodes combinatorially. Each of Phases 4, 5, 6, 8 must verify rendering across 10 themes. No test automation exists.
- **Evidence:** "3-4 light themes: Light Default, Warm Light, Cool Light, (optional: High Contrast Light)" -- 13 todo items, 3 risk categories, no user demand cited.
- **Suggested fix:** Ship ONE light theme (Light Default). Omit Warm/Cool/High Contrast. Omit auto-detect (manual toggle sufficient). Cuts testing surface 60-75%.

## Finding 8: Dependency ordering violated -- Phase 8 before Phase 9 causes rework
- **Severity:** High
- **Location:** plan.md Key Dependencies vs Execution Order
- **Flaw:** Dependencies say "Phase 9 (FTS) -> Phase 8 (search UX consumes FTS backend)". But execution order puts Phase 8 at #8, Phase 9 at #9. Phase 8 will be built against ILIKE backend, then must be rewired after Phase 9 introduces `/api/search` with different response shape.
- **Failure scenario:** Phase 8 implements search using existing `?search=` param. Phase 9 creates `GET /api/search` with `{results, method, has_more}` response. Cross-chat toggle is stubbed. After Phase 9, Phase 8 code must be reworked to swap endpoint, handle new response, enable cross-chat. Rework not tracked in plan.
- **Evidence:** plan.md: "Phase 9 (FTS) -> Phase 8 (search UX consumes FTS backend)". Execution: Phase 8 = #8, Phase 9 = #9.
- **Suggested fix:** Swap execution order: Phase 9 (FTS backend) before Phase 8 (search UX). Build search UX directly against FTS API; eliminate stub and rework.

## Finding 9: Phase 4 browser right-click override lacks escape hatch in implementation
- **Severity:** Medium
- **Location:** Phase 4, Risk Assessment vs Implementation Steps
- **Flaw:** Risk Assessment mentions "hold Shift+right-click to get browser default" but Implementation Steps (6 steps) and Todo (12 items) do not include it. Every `@contextmenu.prevent` unconditionally suppresses browser menu including Inspect Element.
- **Failure scenario:** Developer debugging rendering right-clicks message. Custom menu appears. No way to reach DevTools via context menu. Shift+right-click was discussed but never implemented.
- **Evidence:** Risk Assessment: "Consider: hold Shift+right-click to get browser default." Absent from Implementation Steps 1-6 and all 12 todo items.
- **Suggested fix:** Add implementation step: "In openContextMenu, check `if (event.shiftKey) return` to allow browser default." Add corresponding todo item.

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 2 |
| High     | 4 |
| Medium   | 3 |

**Top 3 actions to reduce risk:**
1. Delete Phase 10 (AI skeleton) entirely -- pure YAGNI
2. Swap Phase 8/9 execution order to match declared dependencies
3. Ship 1 light theme instead of 3-4; cut scope of Phase 7

## Unresolved Questions
- Which database (SQLite or PostgreSQL) is used in production? This determines whether dual-FTS implementation in Phase 9 is justified.
- Has any user requested light themes? If not, Phase 7 should be demoted to LOW priority or cut.
- What is the expected message volume? ILIKE with a B-tree index on `messages.text` may be sufficient for the actual dataset size, making Phase 9 unnecessary complexity.
