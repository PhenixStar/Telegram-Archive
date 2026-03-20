# Phase 2: Backup-Profile Selector System

## Priority: High
## Status: Pending

## Overview
Allow the login page to present a backup-profile selector so users can choose which Telegram backup instance they're logging into. Supports multiple admins, databases, and Telegram accounts running on the same host.

## Key Insights

### Architecture Options Considered
1. **Option A: Frontend-only routing** — Profiles defined in a JSON config, each pointing to a different viewer URL (e.g., `host:8847`, `host:8848`). Login page acts as a launcher.
2. **Option B: Single viewer, multiple DBs** — One viewer instance switches between SQLite databases. Requires significant backend changes.
3. **Option C: Reverse proxy profiles** — Nginx/Caddy routes `/profile-name/` to different viewer containers.

### Decision: Option A (Frontend routing)
- KISS: Each backup already runs as an independent Docker service with its own viewer
- No backend changes to existing auth flow
- Profile config is a simple JSON served by a lightweight config endpoint or embedded in HTML
- The login page becomes a "portal" that redirects to the selected profile's viewer

### Profile Data Model
```json
{
  "profiles": [
    {
      "id": "main",
      "name": "Personal Telegram",
      "description": "Main account backup",
      "url": "/",
      "icon": "user",
      "color": "#8774e1"
    },
    {
      "id": "business",
      "name": "Business Account",
      "description": "Company Telegram",
      "url": "http://host:8848",
      "icon": "briefcase",
      "color": "#3b82f6"
    }
  ],
  "show_selector": true
}
```

## Requirements

### Frontend
1. Profile selector appears ABOVE the login form when `show_selector` is true and `profiles.length > 1`
2. Each profile card shows: icon, name, description, color indicator
3. Selected profile highlighted with accent border
4. Selecting a profile sets the target URL — if external, redirect; if `/`, login here
5. Profile cards use horizontal scroll on mobile, grid on desktop
6. Remember last-selected profile in `localStorage`

### Backend (minimal)
1. New endpoint `GET /api/profiles` returns the profile list
2. Profile config from environment variable `BACKUP_PROFILES` (JSON string) or a `profiles.json` file in data dir
3. If no profiles configured, return `{ profiles: [], show_selector: false }` — login page works as before

### Docker
1. Each viewer instance has its own `BACKUP_PROFILES` env or mounts a shared `profiles.json`
2. Example multi-instance docker-compose snippet in docs

## Implementation Steps

### 1. Backend: Profile endpoint (main.py)
```python
@app.get("/api/profiles")
async def get_profiles():
    """Return backup profiles for the login page selector."""
    raw = os.getenv("BACKUP_PROFILES", "")
    if raw:
        try:
            profiles = json.loads(raw)
            return {"profiles": profiles, "show_selector": len(profiles) > 1}
        except json.JSONDecodeError:
            pass
    # Check file-based config
    profiles_file = Path(config.backup_path) / "profiles.json"
    if profiles_file.exists():
        try:
            data = json.loads(profiles_file.read_text())
            profiles = data if isinstance(data, list) else data.get("profiles", [])
            return {"profiles": profiles, "show_selector": len(profiles) > 1}
        except Exception:
            pass
    return {"profiles": [], "show_selector": False}
```

### 2. Frontend: Profile state (setup())
```javascript
const backupProfiles = ref([])
const selectedProfile = ref(null)
const showProfileSelector = ref(false)

const loadProfiles = async () => {
    try {
        const res = await fetch('/api/profiles')
        if (res.ok) {
            const data = await res.json()
            backupProfiles.value = data.profiles || []
            showProfileSelector.value = data.show_selector
            // Restore last selection
            const lastId = localStorage.getItem('tg-last-profile')
            const last = backupProfiles.value.find(p => p.id === lastId)
            selectedProfile.value = last || backupProfiles.value[0] || null
        }
    } catch {}
}
```

### 3. Frontend: Profile selector UI (login template)
Insert between title and form:
```html
<!-- Profile Selector -->
<div v-if="showProfileSelector && backupProfiles.length > 1" class="mb-6">
    <label class="block text-xs font-medium mb-2"
           style="color: rgba(255,255,255,0.5);">Select Backup</label>
    <div class="space-y-2 max-h-48 overflow-y-auto custom-scroll">
        <div v-for="profile in backupProfiles" :key="profile.id"
             @click="selectProfile(profile)"
             class="flex items-center gap-3 p-3 rounded-xl cursor-pointer transition-all duration-200"
             :style="{
                 background: selectedProfile?.id === profile.id
                     ? 'rgba(255,255,255,0.12)' : 'rgba(255,255,255,0.04)',
                 border: selectedProfile?.id === profile.id
                     ? '1px solid ' + (profile.color || 'var(--tg-primary)')
                     : '1px solid rgba(255,255,255,0.08)'
             }">
            <div class="w-8 h-8 rounded-lg flex items-center justify-center"
                 :style="{ background: profile.color || 'var(--tg-primary)' }">
                <i :class="'fas fa-' + (profile.icon || 'database')"
                   class="text-white text-xs"></i>
            </div>
            <div class="min-w-0 flex-1">
                <div class="text-sm font-medium text-white truncate">
                    {{ profile.name }}
                </div>
                <div class="text-xs truncate"
                     style="color: rgba(255,255,255,0.4);">
                    {{ profile.description }}
                </div>
            </div>
            <div v-if="selectedProfile?.id === profile.id"
                 class="w-2 h-2 rounded-full"
                 :style="{ background: profile.color || 'var(--tg-primary)' }">
            </div>
        </div>
    </div>
</div>
```

### 4. Profile selection logic
```javascript
const selectProfile = (profile) => {
    selectedProfile.value = profile
    localStorage.setItem('tg-last-profile', profile.id)
    // If external URL, redirect login to that instance
    if (profile.url && profile.url !== '/' && profile.url !== window.location.origin) {
        window.location.href = profile.url
    }
}
```

### 5. Docker-compose example (multi-instance)
```yaml
# Instance 1: Personal Telegram
telegram-viewer-personal:
  build: { context: ., dockerfile: Dockerfile.viewer }
  ports: ["8847:8000"]
  environment:
    VIEWER_USERNAME: admin
    VIEWER_PASSWORD: ${VIEWER_PASSWORD}
    BACKUP_PROFILES: '[{"id":"personal","name":"Personal","url":"/","icon":"user","color":"#8774e1"},{"id":"business","name":"Business","url":"http://host:8848","icon":"briefcase","color":"#3b82f6"}]'
  volumes:
    - ./data-personal:/data/backups

# Instance 2: Business Telegram
telegram-viewer-business:
  build: { context: ., dockerfile: Dockerfile.viewer }
  ports: ["8848:8000"]
  environment:
    VIEWER_USERNAME: admin
    VIEWER_PASSWORD: ${VIEWER_PASSWORD_BIZ}
    BACKUP_PROFILES: '[{"id":"personal","name":"Personal","url":"http://host:8847","icon":"user","color":"#8774e1"},{"id":"business","name":"Business","url":"/","icon":"briefcase","color":"#3b82f6"}]'
  volumes:
    - ./data-business:/data/backups
```

## Files to Modify
- `src/web/main.py` — add `/api/profiles` endpoint (~15 lines)
- `src/web/templates/index.html` — profile selector UI + JS (~80 lines)

## Files to Create
- None (profiles.json is user-created, optional)

## Success Criteria
- No profiles configured: login page unchanged (backward compatible)
- Profiles configured: selector appears, clicking external profile redirects
- Local profile: login form targets current instance
- Last-selected profile remembered across sessions
- Mobile: profile cards scroll vertically, touch-friendly

## Risk Assessment
- Cross-origin profile links: cookies don't transfer. Each instance has independent auth. This is correct behavior.
- CORS: no CORS issues since we redirect (full page navigation), not AJAX.
