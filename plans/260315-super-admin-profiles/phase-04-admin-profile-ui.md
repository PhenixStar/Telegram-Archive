# Phase 4: Admin Profile Management UI

## Priority: Medium
## Status: Planned
## Depends on: Phase 2, Phase 3

## Overview
Enable admin-level users to manage their assigned profiles: rename, change description/icon, and create viewers/tokens scoped to their profiles. Admins see a filtered view of the existing admin panel — only their assigned profiles' data.

## Key Insights
- Admin role has `allowed_profile_ids` — JSON array of profile IDs they can manage
- Existing viewers/tokens CRUD endpoints use `require_master()` — need to accept `admin` role
- Admin should NOT see: Super Admin tab, Listener config, Backup config, Login Design
- Admin SHOULD see: AI Models (read-only?), Viewers (scoped), Tokens (scoped), Audit (own actions)

## Requirements

### Functional
1. Admin can rename profiles they're assigned to (name, description only)
2. Admin can create viewers within their assigned profile scope
3. Admin can create share tokens within their assigned profile scope
4. Admin sees their audit log entries only
5. Admin CANNOT access: Super Admin tab, Backup config, Listener config

### Non-Functional
- Seamless UX — admin shouldn't feel "locked out", just sees their scope
- Profile rename reflected on login page immediately

## Changes

### Backend: `src/web/main.py`

**Update existing admin endpoints to accept `admin` role:**

Replace `require_master` with `require_admin_or_above` on these endpoints:
- `GET /api/admin/viewers` — filter by admin's profile scope
- `POST /api/admin/viewers` — validate chat_ids within admin's scope
- `PUT /api/admin/viewers/{id}` — only if viewer belongs to admin's scope
- `DELETE /api/admin/viewers/{id}` — only if viewer belongs to admin's scope
- `GET /api/admin/tokens` — filter by admin's scope
- `POST /api/admin/tokens` — validate chat_ids within scope
- `PUT /api/admin/tokens/{id}` — only own tokens
- `DELETE /api/admin/tokens/{id}` — only own tokens
- `GET /api/admin/chats` — filter by admin's profile scope
- `GET /api/admin/audit` — filter to own actions only for admin role

**Keep `require_master` (super_admin only) on:**
- Listener config endpoints
- Backup config endpoints
- AI config endpoints (or make read-only for admin)

**Profile rename endpoint — admin scoping:**
```python
@app.put("/api/admin/profiles/{profile_id}")
async def update_profile(profile_id: str, request: Request, user: UserContext = Depends(require_admin_or_above)):
    data = await request.json()
    if user.role == "admin":
        # Admin can only edit name/description of profiles they're assigned to
        if profile_id not in (user.allowed_profile_ids or []):
            raise HTTPException(403, "Not assigned to this profile")
        allowed_fields = {"name", "description"}
        data = {k: v for k, v in data.items() if k in allowed_fields}
    # super_admin can edit all fields
    await db.update_backup_profile(profile_id, **data)
    return {"success": True}
```

**Add `allowed_profile_ids` to UserContext:**
```python
@dataclass
class UserContext:
    username: str
    role: str  # "super_admin", "admin", "viewer", "token"
    allowed_chat_ids: set[int] | None = None
    allowed_profile_ids: list[str] | None = None  # NEW: admin profile scope
    no_download: bool = False
```

**Add to SessionData and session creation:**
```python
@dataclass
class SessionData:
    ...
    allowed_profile_ids: list[str] | None = None  # NEW
```

### Frontend: `src/web/templates/index.html`

**Admin tab sub-tab visibility:**
```javascript
// Computed: which admin sub-tabs to show
const visibleAdminTabs = computed(() => {
    const role = userRole.value
    const tabs = []
    if (role === 'super_admin' || role === 'master') {
        tabs.push('ai-models', 'listener', 'backup', 'viewers', 'tokens', 'audit')
    } else if (role === 'admin') {
        tabs.push('viewers', 'tokens', 'audit')  // scoped view
    }
    return tabs
})
```

**Admin sub-tab buttons — conditionally render:**
```html
<button v-if="visibleAdminTabs.includes('ai-models')" @click="adminActiveTab = 'ai-models'" ...>AI Models</button>
<button v-if="visibleAdminTabs.includes('listener')" @click="adminActiveTab = 'listener'" ...>Listener</button>
<button v-if="visibleAdminTabs.includes('backup')" @click="adminActiveTab = 'backup'" ...>Backup</button>
<button v-if="visibleAdminTabs.includes('viewers')" @click="adminActiveTab = 'viewers'" ...>Viewers</button>
<button v-if="visibleAdminTabs.includes('tokens')" @click="adminActiveTab = 'tokens'" ...>Tokens</button>
<button v-if="visibleAdminTabs.includes('audit')" @click="adminActiveTab = 'audit'" ...>Audit</button>
```

**Profile rename card for admin (in Admin tab header area):**
```html
<!-- Show assigned profiles with rename capability -->
<div v-if="userRole === 'admin' && assignedProfiles.length" class="mb-4">
    <label class="text-xs font-medium mb-2 block" style="color: var(--tg-muted);">Your Profiles</label>
    <div v-for="p in assignedProfiles" :key="p.id" class="flex items-center gap-2 p-2 rounded-lg mb-1"
        style="background: var(--tg-hover);">
        <div class="w-8 h-8 rounded-lg flex items-center justify-center" :style="{ background: p.color }">
            <i :class="'fas fa-' + p.icon" class="text-white text-xs"></i>
        </div>
        <input v-model="p.name" @blur="renameProfile(p)" class="flex-1 bg-transparent text-sm outline-none"
            style="color: var(--tg-text);" />
        <i class="fas fa-pen text-[10px]" style="color: var(--tg-muted);"></i>
    </div>
</div>
```

**JS additions:**
```javascript
const assignedProfiles = ref([])

const loadAssignedProfiles = async () => {
    if (userRole.value !== 'admin') return
    const res = await fetch('/api/admin/profiles', { credentials: 'include' })
    if (res.ok) assignedProfiles.value = (await res.json()).profiles || []
}

const renameProfile = async (profile) => {
    await fetch(`/api/admin/profiles/${profile.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ name: profile.name, description: profile.description })
    })
}
```

### Update `userRole` checks globally

Search `index.html` for `userRole === 'master'` and update to include new roles:

| Current check | Updated check | Context |
|---------------|---------------|---------|
| `userRole === 'master'` (show settings gear) | `['super_admin','admin','master'].includes(userRole)` | Settings icon visibility |
| `userRole === 'master'` (show admin tab) | `['super_admin','admin','master'].includes(userRole)` | Admin tab in settings |
| `userRole === 'master'` (show admin panel) | `['super_admin','admin','master'].includes(userRole)` | Admin panel toggle |

## Implementation Steps
1. Add `allowed_profile_ids` to `UserContext` and `SessionData`
2. Populate `allowed_profile_ids` from `admin_accounts` table during login
3. Update viewer/token CRUD endpoints: accept `admin` role, scope to profile
4. Add profile rename endpoint with admin field restriction
5. Filter audit log by username for admin role
6. Frontend: add `visibleAdminTabs` computed, conditional sub-tab rendering
7. Frontend: add profile rename card for admin users
8. Frontend: update all `userRole === 'master'` checks
9. Add `assignedProfiles`, `renameProfile` to JS and return block

## Success Criteria
- Admin logs in → sees Admin tab with Viewers, Tokens, Audit only
- Admin can rename their assigned profiles inline
- Admin creates viewers → only sees chats from their profile scope
- Admin creates tokens → scoped to their profile's chats
- Admin audit log → only shows own actions
- Super admin sees everything (unchanged)
- `master` role backward compat preserved

## Risk Assessment
- Scoping logic adds complexity — must test edge cases (admin with no profiles, admin with all profiles)
- Frontend `userRole` checks scattered across ~7100 lines — must find all occurrences
- Chat-to-profile mapping: initially profiles don't restrict chats (that's a later enhancement when multi-DB is implemented)
