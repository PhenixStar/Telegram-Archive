# Phase 1: Always-Visible Profile Selector

## Priority: High
## Status: Planned

## Overview
Make the backup profile selector always visible on the login page, even with a single profile. Show the current instance as a styled card/button above the login form, below the animated logo. When no profiles are configured, auto-generate a default profile from the running instance.

## Key Insights
- Current condition: `v-if="showProfileSelector && backupProfiles.length > 1"` (line 911) — hides with 0-1 profiles
- `/api/profiles` returns `show_selector: false` when no profiles configured
- User wants: "smart looking backup selector as buttons above login input and below animated logo"
- Single profile should still show as a styled badge/card — visual identity for the instance

## Requirements

### Functional
1. Profile selector always visible when >= 1 profile exists
2. When no profiles configured, backend auto-generates a default profile from instance metadata
3. Selected profile shown as a highlighted card with icon, name, description
4. Multiple profiles: clickable cards, selected state with accent border
5. Single profile: shown as a non-interactive badge (no selection needed)

### Non-Functional
- No layout shift on login page
- Smooth transition animations
- Mobile-responsive (stacks vertically on small screens)

## Changes

### Backend: `src/web/main.py` (line 1231-1254)

Update `/api/profiles` to always return at least one profile:

```python
@app.get("/api/profiles")
async def get_profiles():
    raw = os.getenv("BACKUP_PROFILES", "")
    profiles = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                profiles = parsed
        except (json.JSONDecodeError, TypeError):
            pass
    if not profiles:
        profiles_file = Path(config.backup_path) / "profiles.json"
        if profiles_file.exists():
            try:
                data = json.loads(profiles_file.read_text())
                profiles = data if isinstance(data, list) else data.get("profiles", [])
            except Exception:
                pass

    # Auto-generate default profile if none configured
    if not profiles:
        profiles = [{
            "id": "default",
            "name": os.getenv("PROFILE_NAME", "Telegram Archive"),
            "description": os.getenv("PROFILE_DESC", ""),
            "icon": "database",
            "color": "#8774e1",
            "url": "/"
        }]

    return {"profiles": profiles, "show_selector": True}
```

### Frontend: `src/web/templates/index.html`

**Template (line 911):** Change condition to always show:
```html
<!-- Before -->
<div v-if="showProfileSelector && backupProfiles.length > 1" class="mb-5">

<!-- After -->
<div v-if="backupProfiles.length > 0" class="mb-5">
```

**Single profile styling:** When only 1 profile, show as a non-interactive badge:
```html
<div v-if="backupProfiles.length === 1" class="flex items-center justify-center gap-2 px-4 py-2 rounded-xl mb-1"
    style="background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.08);">
    <div class="w-7 h-7 rounded-lg flex items-center justify-center shrink-0"
        :style="{ background: backupProfiles[0].color || 'var(--tg-primary)' }">
        <i :class="'fas fa-' + (backupProfiles[0].icon || 'database')" class="text-white text-[10px]"></i>
    </div>
    <span class="text-sm font-medium text-white/70">{{ backupProfiles[0].name }}</span>
</div>
<!-- Multi-profile selector (existing code, for length > 1) -->
<div v-else class="space-y-1.5 max-h-40 overflow-y-auto custom-scroll">
    <!-- existing v-for cards -->
</div>
```

**JS (line 4743):** Remove `show_selector` dependency:
```javascript
// Before
showProfileSelector.value = !!data.show_selector

// After — always show if profiles exist
showProfileSelector.value = (data.profiles || []).length > 0
```

## Implementation Steps
1. Update `/api/profiles` to auto-generate default profile
2. Change template `v-if` condition from `> 1` to `> 0`
3. Add single-profile badge layout (non-interactive)
4. Keep multi-profile interactive selector for `> 1`
5. Auto-select single profile on load

## Success Criteria
- Login page always shows the profile badge/card
- Single instance: shows "Telegram Archive" badge with database icon
- Multiple instances: shows interactive cards with selection state
- No regression on login functionality

## Risk Assessment
- Low risk — purely cosmetic change + minor API tweak
- Backward compatible: existing `BACKUP_PROFILES` env var still works
