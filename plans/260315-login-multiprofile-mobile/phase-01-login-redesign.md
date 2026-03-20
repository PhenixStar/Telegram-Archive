# Phase 1: Login Page Visual Redesign

## Priority: High
## Status: Pending

## Overview
Redesign the login page to be more visually appealing, following Telegram Web A aesthetic patterns while keeping the existing auth flow intact.

## Key Insights
- Current login already has orb animations, glassmorphism card, and gradient button
- Telegram Web A uses: dark bg (#212121), purple accent (#8774e1), clean typography, minimal decoration
- Current page is functional but feels generic — needs brand identity and polish
- Mobile already somewhat supported (max-w-[420px], sm: breakpoints) but can be improved

## Current State (lines 841-941 in index.html)
- Glassmorphism card with backdrop-blur
- SVG Telegram logo with glow
- Password/Token tab toggle
- Floating orb background animations
- CSS in lines 258-350 (login-* keyframes + classes)

## Requirements

### Visual Improvements
1. **Background**: Replace generic orbs with subtle animated gradient mesh (Telegram-native dark tones)
2. **Card**: Sharper glassmorphism — increase contrast, add subtle border glow matching theme accent
3. **Logo**: Keep Telegram SVG but add theme-aware accent ring (uses `--tg-primary`)
4. **Typography**: Use Inter 600 for title, 400 for subtitle — match Telegram Web spacing
5. **Inputs**: Rounded-xl with better focus states, themed border glow
6. **Button**: Gradient matching `--tg-primary` shade, add hover scale + shimmer effect
7. **Footer**: Add version tag + "Powered by Telegram Archive" with subtle branding

### Theme Awareness
- Login page must respect the 9-theme system's `--login-gradient-*` vars (already defined)
- Card background should use theme-derived semi-transparent colors
- Button gradient should use `var(--tg-primary)` family

### Accessibility
- Focus-visible outlines on inputs and buttons
- aria-labels on toggle buttons
- Error messages with aria-live (already present)

## Implementation Steps

### 1. Update background (CSS)
Replace `.login-orb` colors with theme-aware values using CSS vars:
```css
.login-orb-1 { background: var(--tg-primary); }
.login-orb-2 { background: var(--login-gradient-from); }
.login-orb-3 { background: var(--login-gradient-to); }
```

### 2. Enhance card styling
- Add `backdrop-filter: blur(20px)` (increase from current)
- Border: `1px solid rgba(var(--tg-primary-rgb, 135,116,225), 0.2)`
- Inner shadow for depth

### 3. Theme-aware logo ring
Add accent ring around Telegram logo:
```html
<div class="absolute inset-0 rounded-full"
     style="background: radial-gradient(circle, var(--tg-primary) 0%, transparent 70%);
            filter: blur(12px); opacity: 0.4;"></div>
```

### 4. Improve input focus states
```css
.login-input:focus {
    border-color: var(--tg-primary) !important;
    box-shadow: 0 0 0 3px rgba(var(--tg-primary-rgb, 135,116,225), 0.2) !important;
}
```

### 5. Button gradient from theme
Replace hardcoded `#8774e1, #6c5ce7, #5b4cdb` with:
```css
background: linear-gradient(135deg, var(--login-gradient-from), var(--login-gradient-via), var(--login-gradient-to));
```

### 6. Add version footer
```html
<p class="text-[10px] tracking-widest uppercase" style="color: rgba(255,255,255,0.2);">
    Telegram Archive <span class="opacity-50">v{{ appVersion }}</span>
</p>
```

## Files to Modify
- `src/web/templates/index.html` — lines 258-350 (CSS), 841-941 (template)

## Success Criteria
- Login page uses theme-aware colors throughout
- Visual improvement visible without breaking functionality
- Password + Token login still work identically
- Responsive on mobile (tested 375px viewport)
