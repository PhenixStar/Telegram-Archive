# Phase 10: AI Assistant Side Panel (SEPARATE BRANCH)

**[RED TEAM] DEMOTED to "Coming Soon" UI stub. No backend, no provider ABC, no admin config tab.**

## Context

- [Research: Search UX, DB Indexing, AI Panel](../reports/researcher-260311-1743-search-indexing-ai-panel.md)
- **Develop on separate feature branch: `feature/ai-panel`**

## Overview

- **Priority:** LOW (separate branch)
- **Status:** Pending
- **Description:** ~~Full skeleton with backend routes, provider ABC, admin config, SSE plumbing~~ **Demoted per red team:** Single "Coming Soon" UI toggle only. Implement actual AI panel when requirements and provider are defined.

## Key Insights

- **[RED TEAM]** Original plan built 3 new files, backend router, provider ABC, admin config tab, SSE plumbing for a stub that returns `{"error": "AI not configured"}`. Pure YAGNI -- when actual AI requirements arrive, provider interface/message format/context strategy will need redesign.
- Reduced to: AI icon in header that opens a "Coming Soon" overlay. No backend. No new files.

## Requirements

**Functional:**
- AI icon button in header area (brain or sparkle icon)
- Clicking opens a small overlay/tooltip: "AI Assistant -- Coming Soon"
- Click again to dismiss
- That's it. No panel, no backend, no config.

**Non-functional:**
- Icon + overlay must work with all themes (CSS custom properties)
- Must not break existing layout

## Related Code Files

**Modify:**
- `src/web/templates/index.html` -- AI icon in header + "Coming Soon" tooltip/overlay (CSS + 10 lines JS)

**~~Create~~ (REMOVED per red team):**
- ~~`src/web/ai_routes.py`~~
- ~~`src/ai/__init__.py`~~
- ~~`src/ai/provider.py`~~

## Implementation Steps

1. **AI icon in header** (index.html):
   - Add icon button near theme selector: `<button @click="showAiComingSoon = !showAiComingSoon">`
   - Brain or sparkle FA icon
   - Styled with `color: var(--tg-muted)`, hover: `var(--tg-accent)`

2. **"Coming Soon" overlay** (index.html):
   - `const showAiComingSoon = ref(false)`
   - Small absolutely-positioned div below the icon
   - Text: "AI Assistant" heading + "Coming soon. Configure when requirements are defined." subtext
   - Dismiss on click outside or Escape
   - Theme-aware: `bg: var(--tg-sidebar); color: var(--tg-text); border: var(--tg-border)`

## Todo

- [ ] Add AI icon button in header area
- [ ] Add `showAiComingSoon` ref + toggle
- [ ] Add "Coming Soon" overlay with dismiss behavior
- [ ] Style for all themes
- [ ] Test icon + overlay in dark and light themes

## Success Criteria

- AI icon visible in header
- Clicking shows "Coming Soon" overlay
- Clicking again (or outside) dismisses it
- Works in all themes

## Risk Assessment

- **[RED TEAM RESOLVED]** No backend code, no provider ABC, no admin config tab = zero wasted engineering time
- **[RED TEAM RESOLVED]** API key storage concern eliminated -- no API keys stored in this phase
- Functional AI panel deferred to when actual requirements exist
