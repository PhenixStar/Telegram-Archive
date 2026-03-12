# Phase 03: Viewer Settings Panel

## Context

- [Research: Context Menu, Links, Keyboard](../reports/researcher-260311-1742-context-menu-links-keyboard.md)
- Existing settings API: `GET /api/admin/settings` and `PUT /api/admin/settings/{key}` (main.py lines 2101-2133)
- `viewerTimezone` ref exists (index.html line 1713), defaults to `Europe/Madrid`, updated from stats endpoint
- `SCHEDULE` env var controls backup cron, default `0 */6 * * *`
- `AppSettings` model used for generic key-value storage

## Overview

- **Priority:** HIGH
- **Status:** Pending
- **Description:** Add a Settings panel with timezone (localStorage-only) and a reusable toast notification system. ~~Backup interval removed from UI per red team~~ (env var only).

## Key Insights

- **[RED TEAM]** Timezone stored in `localStorage` only -- no backend API needed. Eliminates global-write privilege escalation (any viewer changing timezone for all users).
- **[RED TEAM]** Backup interval removed from settings UI. Remains `SCHEDULE` env var only. A non-functional UI control is worse than none.
- `viewerTimezone` is currently loaded from stats endpoint (`/api/stats`), not from settings API
- No toast/notification system exists in the frontend; clipboard copy in admin panel uses inline `navigator.clipboard.writeText()` with no feedback

## Requirements

**Functional:**
- Settings accessible from header gear icon (all users)
- Timezone selector: dropdown of common IANA timezones + "Auto-detect" toggle (`Intl.DateTimeFormat().resolvedOptions().timeZone`)
- Timezone stored in `localStorage` -- no backend API, no global state
- Toast notification component: small popup at bottom-center, auto-dismiss after 3s, supports success/error types
- Timezone changes take effect immediately (update `viewerTimezone` ref + moment default)

**Non-functional:**
- Toast must use CSS custom properties for theme compatibility
- Timezone persists in `localStorage('tg-viewer-timezone')` per browser

## Architecture

```
User clicks Settings icon (gear) in header
  -> Opens settings modal/panel
  -> Timezone section: dropdown + auto-detect toggle
  -> All stored in localStorage (no backend API needed)
```

## Related Code Files

**Modify:**
- `src/web/templates/index.html` -- add settings UI, toast component, timezone dropdown, gear icon in header

## Implementation Steps

1. **Toast component** (frontend-first, reused by all later phases):
   - Add reactive state: `const toast = reactive({ visible: false, message: '', type: 'success' })`
   - Add `showToast(msg, type='success')` function with 3s auto-dismiss via `setTimeout`
   - Template: fixed bottom-center div with transition, green bg for success, red for error
   - Expose in return object for use throughout app

2. **Settings modal** (frontend):
   - Add gear icon button in header bar (near theme selector)
   - Modal with "Display" section: timezone only
   - Timezone: `<select>` with common timezones + "Auto-detect" checkbox

3. **Timezone save** (frontend, localStorage):
   - On save: write to `localStorage('tg-viewer-timezone')`, update `viewerTimezone.value`, call `moment.tz.setDefault()`
   - On page load: read `localStorage('tg-viewer-timezone')` or default to auto-detect
   - **[RED TEAM]** No backend API -- purely client-side. Each user has their own timezone.

4. **Timezone list**: hardcode top 30 common timezones in frontend (US, Europe, Asia, etc.) + full list via `Intl.supportedValuesOf('timeZone')` if browser supports it

## Todo

- [ ] Implement toast notification component (reactive state + template + CSS)
- [ ] Add `showToast()` function exposed in Vue setup return
- [ ] Add gear icon in header bar
- [ ] Build settings modal with timezone dropdown
- [ ] Add "Auto-detect timezone" toggle
- [ ] Save timezone to localStorage (no backend API)
- [ ] Wire timezone save to update `viewerTimezone` ref immediately
- [ ] Test timezone change reflects in message timestamps immediately

## Success Criteria

- Toast appears on clipboard copy, settings save, and errors
- Timezone dropdown shows common timezones + auto-detect option
- Changing timezone updates all visible message timestamps without reload
- Timezone persists in localStorage across browser sessions

## Risk Assessment

- **[RED TEAM RESOLVED]** Timezone is now per-user via localStorage -- no global privilege escalation risk
- **[RED TEAM RESOLVED]** Backup interval removed from UI -- no non-functional control

## Security Considerations

- No backend endpoints needed -- settings are client-side only
- Timezone stored in localStorage, no auth concerns
