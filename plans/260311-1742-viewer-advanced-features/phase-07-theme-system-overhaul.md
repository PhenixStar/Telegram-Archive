# Phase 07: Theme System Overhaul

## Context

- Existing 6 dark themes defined via CSS custom properties in `index.html` (lines 36-160+)
- Themes: midnight (default), telegram-classic, amoled-black, nord, monokai, solarized-dark
- Theme stored in `localStorage('tg-theme')`, applied before paint via IIFE (line 29-32)
- `<html>` has `class="dark"`, `data-theme="midnight"`, `style="color-scheme: dark"`
- Theme selector exists in frontend (dropdown/picker)
- All UI elements use `var(--tg-*)` custom properties

## Overview

- **Priority:** MEDIUM
- **Status:** Pending
- **Description:** Add 1 light theme (Light Default), update `color-scheme` meta, add theme preview, optional system preference auto-detect. ~~3-4 themes cut to 1 per red team -- no user demand, cuts testing 60-75%~~

## Key Insights

- Light themes need dark text (`--tg-text: #1a1a1a`), light backgrounds (`--tg-bg: #ffffff`)
- CRITICAL: form inputs, selects, textareas inherit from CSS custom properties. Light themes must ensure all interactive elements have proper contrast -- no light text on light background.
- `color-scheme: dark` in `<html style>` affects browser-native form controls (scrollbars, checkboxes, selects). Must toggle to `color-scheme: light` for light themes.
- Message bubbles use `hsla()` with transparency -- light theme equivalents need opaque or semi-transparent light colors
- Login page gradient (`--login-gradient-*`) needs light-appropriate values
- The `class="dark"` on `<html>` is for Tailwind dark mode (not used since all styling is via custom properties). Can be toggled but has no effect currently.

## Requirements

**Functional:**
- **[RED TEAM]** 1 light theme: Light Default only (Warm/Cool/High Contrast deferred until user demand)
- Toggle `color-scheme` meta between `dark` and `light` based on theme type
- Theme preview in selector shows bg color + accent color swatch
- Optional: auto-detect system preference via `prefers-color-scheme` media query
- Store auto-detect preference in localStorage

**Non-functional:**
- All existing dark themes remain unchanged
- Light Default must pass WCAG AA contrast ratios for text
- Form fields (inputs, selects, textareas) must remain readable in light theme

## Architecture

```
Theme data structure:
  themes = [
    { id: 'midnight', label: 'Midnight', type: 'dark', accent: '#3b82f6', bg: '#0f172a' },
    { id: 'light-default', label: 'Light', type: 'light', accent: '#2563eb', bg: '#ffffff' },
    ...
  ]

On theme change:
  1. Set data-theme attribute
  2. Update color-scheme (dark/light)
  3. Update theme-color meta
  4. Save to localStorage
```

## Related Code Files

**Modify:**
- `src/web/templates/index.html`:
  - CSS: add 3-4 `[data-theme="light-*"]` rule blocks with light-appropriate custom properties
  - JS: update theme switcher to handle light/dark type, toggle `color-scheme`
  - JS: add `prefers-color-scheme` media query listener for auto-detect
  - HTML: enhance theme selector with color swatches

## Implementation Steps

1. **Define light theme CSS custom properties**:

   **Light Default:**
   ```css
   [data-theme="light-default"] {
     --tg-bg: #ffffff;
     --tg-sidebar: #f0f2f5;
     --tg-hover: #e4e6eb;
     --tg-active: #d1e3ff;
     --tg-text: #1a1a1a;
     --tg-muted: #65676b;
     --tg-own-msg: hsla(210, 80%, 92%, 0.95);
     --tg-other-msg: hsla(0, 0%, 96%, 0.95);
     --tg-border: #dddfe2;
     --tg-accent: #2563eb;
     --login-gradient-from: #2563eb;
     --login-gradient-via: #3b82f6;
     --login-gradient-to: #60a5fa;
   }
   ```

   ~~Warm Light and Cool Light deferred per red team review~~

2. **Theme type mapping** (JS):
   - Add `themeType` computed: `isDarkTheme(themeId)` based on theme ID prefix or lookup table
   - On theme change: update `document.documentElement.style.colorScheme = isDark ? 'dark' : 'light'`
   - Update `<meta name="theme-color">` content

3. **Theme selector enhancement**:
   - Group themes: "Dark" and "Light" sections
   - Each option shows small color swatch (bg + accent circle)
   - Separator between dark and light groups

4. **Auto-detect system preference**:
   - `const prefersDark = window.matchMedia('(prefers-color-scheme: dark)')`
   - If user enables "Auto" option: listen to `prefersDark.addEventListener('change', ...)`
   - Store `localStorage.setItem('tg-theme-auto', 'true')`
   - IIFE at page top: if auto mode, check system preference and pick default dark/light theme

5. **Form field contrast fix**:
   - Add CSS rule for light themes: input/select/textarea get explicit `color: var(--tg-text); background: var(--tg-bg)` or slightly darker bg
   - Test all form fields: login form, admin panel inputs, search inputs, date picker

6. **Update IIFE** (line 29-32) to handle auto-detect:
   ```js
   (function() {
     var auto = localStorage.getItem('tg-theme-auto') === 'true';
     if (auto) {
       var dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
       var t = dark ? 'midnight' : 'light-default';
     } else {
       var t = localStorage.getItem('tg-theme') || 'midnight';
     }
     document.documentElement.setAttribute('data-theme', t);
     document.documentElement.style.colorScheme = t.startsWith('light') ? 'light' : 'dark';
   })();
   ```

## Todo

- [ ] Add `[data-theme="light-default"]` CSS custom properties
- [ ] Update theme IIFE to handle light/dark `color-scheme` toggle
- [ ] Update theme switcher JS to toggle `color-scheme` on change
- [ ] Update `<meta name="theme-color">` on theme change
- [ ] Add color swatches to theme selector UI
- [ ] Group themes into Dark/Light sections in selector
- [ ] Add "Auto (system)" option in theme selector
- [ ] Add `prefers-color-scheme` media query listener
- [ ] Verify form field contrast in Light Default (login, admin, search)
- [ ] Verify message bubble readability in Light Default
- [ ] Verify lightbox overlay still works in Light Default

## Success Criteria

- **[RED TEAM]** 1 new light theme (Light Default) available in theme selector
- Switching to Light Default shows dark text on light background everywhere
- `color-scheme` toggles between dark/light (affects scrollbars, form controls)
- Form inputs remain readable in all themes
- "Auto" option follows system preference
- All existing dark themes unchanged

## Risk Assessment

- **Form field contrast** -- CRITICAL. Light text on light background makes app unusable.
  - **Mitigation:** Explicit `color: var(--tg-text)` on all input/select/textarea elements
  - **Mitigation:** Test login page, admin panel, search bar, date picker in each light theme
- **Message bubble readability** -- transparent `hsla()` backgrounds may look wrong on white
  - **Mitigation:** Use higher opacity or solid colors for light theme bubbles
- **Third-party components** (Flatpickr, FontAwesome) -- may not respect custom properties
  - **Mitigation:** Override Flatpickr dark theme styles for light themes
