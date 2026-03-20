# Phase 3: Mobile Web Responsive Overhaul

## Priority: Medium
## Status: Done (critical items complete, nice-to-haves deferred)

## Overview
Make the Telegram Archive viewer fully usable on mobile browsers. Currently has basic responsive (sidebar hides on chat select) but lacks Telegram-native mobile UX patterns.

## Key Insights from Telegram Web A Reference
- Touch-optimized: `user-scalable=no, viewport-fit=cover`
- Safe area insets: `env(safe-area-inset-*)` for notched phones
- CSS var `--vh` for real viewport height (avoids iOS address bar issues)
- Chat list items: 72px height, 54px avatars
- No horizontal scroll — everything fits in viewport width
- Transition between sidebar and chat view (not side-by-side on mobile)
- Bottom-anchored composer with sticky positioning
- Message bubbles: max-width 85% on mobile

## Current Mobile State
- `hidden md:flex` toggles sidebar vs chat panel (768px breakpoint) -- working
- Some `sm:` responsive sizing on header elements
- Sidebar has no fixed width on mobile (uses `w-full`)
- Message input at bottom -- works but not optimized for mobile keyboard
- Settings modal -- not mobile-optimized
- Profile sidebar -- not mobile-optimized

## Requirements

### Critical (must-have)
1. **Viewport fix**: Real `--vh` calculation for iOS Safari address bar
2. **Safe area insets**: Apply `env(safe-area-inset-*)` to key containers
3. **Touch-friendly targets**: All buttons >= 44px touch target
4. **Message composer**: Sticky bottom, keyboard-aware on mobile
5. **Settings modal**: Full-screen on mobile instead of centered overlay

### Important (should-have)
6. **Swipe gestures**: Swipe right on chat to go back to sidebar (optional, nice-to-have)
7. **Pull-to-refresh**: On chat list (optional)
8. **Image viewer**: Pinch-to-zoom on lightbox images
9. **Chat header**: Compact mode on mobile (hide stats)
10. **PWA**: manifest.json already exists — ensure it works well

### Nice-to-have
11. **Bottom navigation**: Quick access to chats/search/settings on mobile
12. **Haptic feedback**: Using `navigator.vibrate()` on long-press (if available)

## Implementation Steps

### 1. Viewport height fix (CSS + JS)
Add to IIFE at top:
```javascript
// Fix mobile viewport height (iOS Safari address bar)
function setVh() {
    document.documentElement.style.setProperty('--vh', window.innerHeight * 0.01 + 'px');
}
setVh();
window.addEventListener('resize', setVh);
```

CSS usage:
```css
.h-full-mobile { height: calc(var(--vh, 1vh) * 100); }
```

Apply to root app container.

### 2. Safe area insets (CSS)
```css
:root {
    --sat: env(safe-area-inset-top, 0px);
    --sar: env(safe-area-inset-right, 0px);
    --sab: env(safe-area-inset-bottom, 0px);
    --sal: env(safe-area-inset-left, 0px);
}
```
Already present at line 49. Apply to:
- App header: `padding-top: var(--sat)`
- Message composer: `padding-bottom: var(--sab)`
- Settings modal: `padding: var(--sat) var(--sar) var(--sab) var(--sal)`

### 3. Touch-friendly buttons (CSS)
```css
@media (max-width: 768px) {
    .touch-target { min-width: 44px; min-height: 44px; }
    /* Enlarge small icon buttons in header */
    .chat-header-btn { padding: 10px; }
}
```

### 4. Settings modal mobile fullscreen
```css
@media (max-width: 768px) {
    .settings-modal-content {
        width: 100% !important;
        height: 100% !important;
        max-width: 100% !important;
        max-height: 100% !important;
        border-radius: 0 !important;
        margin: 0 !important;
    }
}
```

### 5. Message composer mobile optimization
- On focus: composer scrolls into view above keyboard
- Use `visualViewport` API for keyboard detection:
```javascript
if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', () => {
        const keyboardOpen = window.innerHeight - window.visualViewport.height > 150
        // Adjust composer position
    })
}
```

### 6. Profile/media sidebar mobile
When opened on mobile, overlay full-screen instead of side panel:
```css
@media (max-width: 768px) {
    .profile-sidebar {
        position: fixed !important;
        inset: 0 !important;
        width: 100% !important;
        z-index: 50;
    }
}
```

### 7. Login page mobile polish
- Reduce card padding: `p-6` on mobile vs `p-10` on desktop
- Logo size: `w-16 h-16` on mobile vs `w-20 h-20`
- Stack profile cards vertically
- Keyboard: `inputmode="text"` on username, no autocapitalize

## Files to Modify
- `src/web/templates/index.html` — CSS media queries, JS viewport fix, template responsive classes

## Success Criteria
- App fully usable on iPhone Safari + Chrome Android
- No horizontal overflow on 375px viewport
- Settings modal usable on mobile (full screen)
- Login page looks good on 375px
- Message composer works with on-screen keyboard
- Touch targets >= 44px

## Risk Assessment
- iOS Safari `100vh` bug — mitigated by `--vh` var
- Keyboard overlap on composer — mitigated by `visualViewport` API
- Sidebar resize handle conflicts — only shown on `md:` (768px+), no conflict
