# Phase 3: Super Admin Config UI

## Priority: Medium
## Status: Planned
## Depends on: Phase 2

## Overview
Add a "Super Admin" tab in the settings panel, visible only to `super_admin` role. This tab contains profile management (CRUD), admin account management, and login page design controls. Existing admin tabs remain accessible to both `super_admin` and `admin` roles.

## Key Insights
- Settings modal has tabs: General, Appearance, Notifications, Admin (line ~2400)
- Admin tab has sub-tabs: AI Models, Listener, Backup, Viewers, Tokens, Audit (line ~2622)
- Tab visibility controlled by `userRole.value === 'master'` checks
- All frontend in single `index.html` — add new tab + panels inline

## Requirements

### Functional
1. New "Super Admin" settings tab — only visible when `userRole === 'super_admin'`
2. Profile Management sub-panel: list, create, edit, delete backup profiles
3. Admin Management sub-panel: list, create, edit, delete admin accounts with profile assignment
4. Login Design sub-panel: customize login page gradient, logo, title text (future, stub for now)
5. Existing "Admin" tab visible to both `super_admin` and `admin` roles

### Non-Functional
- Consistent with existing admin panel styling
- Mobile-responsive (full-screen modal on mobile)

## Changes

### Frontend: `src/web/templates/index.html`

**Settings tab bar — add Super Admin tab (near line 2456):**
```html
<button v-if="userRole === 'super_admin'"
    @click="settingsActiveTab = 'super-admin'; loadSuperAdminData()"
    :class="settingsActiveTab === 'super-admin' ? 'border-current text-[color:var(--tg-accent)]' : 'border-transparent'"
    :style="{color: settingsActiveTab === 'super-admin' ? '' : 'var(--tg-muted)'}"
    class="px-3 py-2 border-b-2 text-sm font-medium hover:opacity-80 whitespace-nowrap">
    <i class="fas fa-crown text-xs mr-1"></i>Super Admin
</button>
```

**Update Admin tab visibility (line ~2456):**
```html
<!-- Before: only master sees Admin tab -->
@click="settingsActiveTab = 'admin'" v-if="userRole === 'master'"

<!-- After: super_admin and admin see it -->
@click="settingsActiveTab = 'admin'" v-if="userRole === 'super_admin' || userRole === 'admin' || userRole === 'master'"
```

**Super Admin panel content:**
```html
<div v-if="settingsActiveTab === 'super-admin'" class="space-y-4">
    <!-- Sub-tabs: Profiles | Admins | Login Design -->
    <div class="flex gap-1 border-b" style="border-color: var(--tg-border);">
        <button @click="superAdminTab = 'profiles'" ...>Profiles</button>
        <button @click="superAdminTab = 'admins'" ...>Admins</button>
        <button @click="superAdminTab = 'login-design'" ...>Login Design</button>
    </div>

    <!-- Profiles sub-panel -->
    <div v-if="superAdminTab === 'profiles'">
        <!-- Create profile form -->
        <div class="p-3 rounded-lg mb-4" style="background: var(--tg-hover);">
            <h4>Create Backup Profile</h4>
            <input v-model="newProfile.name" placeholder="Profile Name" />
            <input v-model="newProfile.description" placeholder="Description (optional)" />
            <input v-model="newProfile.icon" placeholder="Icon (fa icon name)" />
            <input v-model="newProfile.color" type="color" />
            <input v-model="newProfile.url" placeholder="External URL (optional)" />
            <button @click="createProfile()">Create</button>
        </div>

        <!-- Profile list -->
        <div v-for="profile in managedProfiles" :key="profile.id"
            class="flex items-center gap-3 p-3 rounded-lg mb-2" style="background: var(--tg-hover);">
            <div class="w-10 h-10 rounded-lg flex items-center justify-center"
                :style="{ background: profile.color }">
                <i :class="'fas fa-' + profile.icon" class="text-white"></i>
            </div>
            <div class="flex-1 min-w-0">
                <div class="font-medium" style="color: var(--tg-text);">{{ profile.name }}</div>
                <div class="text-xs" style="color: var(--tg-muted);">{{ profile.description || 'No description' }}</div>
            </div>
            <button @click="editProfile(profile)" class="p-1.5 hover:opacity-80"><i class="fas fa-edit text-xs"></i></button>
            <button @click="deleteProfile(profile.id)" class="p-1.5 hover:opacity-80 text-red-400"><i class="fas fa-trash text-xs"></i></button>
        </div>
    </div>

    <!-- Admins sub-panel -->
    <div v-if="superAdminTab === 'admins'">
        <!-- Create admin form -->
        <div class="p-3 rounded-lg mb-4" style="background: var(--tg-hover);">
            <h4>Create Admin Account</h4>
            <input v-model="newAdmin.username" placeholder="Username" />
            <input v-model="newAdmin.password" type="password" placeholder="Password" />
            <input v-model="newAdmin.displayName" placeholder="Display Name (optional)" />
            <!-- Profile assignment checkboxes -->
            <div class="mt-2">
                <label class="text-xs" style="color: var(--tg-muted);">Assigned Profiles</label>
                <div v-for="p in managedProfiles" :key="p.id" class="flex items-center gap-2 mt-1">
                    <input type="checkbox" :value="p.id" v-model="newAdmin.profileIds" />
                    <span class="text-sm">{{ p.name }}</span>
                </div>
            </div>
            <button @click="createAdmin()">Create</button>
        </div>

        <!-- Admin list -->
        <div v-for="admin in managedAdmins" :key="admin.id"
            class="flex items-center gap-3 p-3 rounded-lg mb-2" style="background: var(--tg-hover);">
            <div class="w-8 h-8 rounded-full flex items-center justify-center" style="background: var(--tg-primary);">
                <span class="text-white text-xs font-bold">{{ (admin.username || '?')[0].toUpperCase() }}</span>
            </div>
            <div class="flex-1 min-w-0">
                <div class="font-medium" style="color: var(--tg-text);">{{ admin.display_name || admin.username }}</div>
                <div class="text-xs" style="color: var(--tg-muted);">
                    Profiles: {{ admin.profile_names || 'All' }}
                </div>
            </div>
            <button @click="editAdmin(admin)" class="p-1.5"><i class="fas fa-edit text-xs"></i></button>
            <button @click="deleteAdmin(admin.id)" class="p-1.5 text-red-400"><i class="fas fa-trash text-xs"></i></button>
        </div>
    </div>

    <!-- Login Design sub-panel (stub) -->
    <div v-if="superAdminTab === 'login-design'">
        <p class="text-sm" style="color: var(--tg-muted);">
            Login page customization coming soon. Currently uses default Telegram-inspired design.
        </p>
    </div>
</div>
```

**JS refs (near line 3155):**
```javascript
// Super Admin state
const superAdminTab = ref('profiles')
const managedProfiles = ref([])
const managedAdmins = ref([])
const newProfile = ref({ name: '', description: '', icon: 'database', color: '#8774e1', url: '' })
const newAdmin = ref({ username: '', password: '', displayName: '', profileIds: [] })
```

**JS functions:**
```javascript
const loadSuperAdminData = async () => {
    await Promise.all([loadManagedProfiles(), loadManagedAdmins()])
}

const loadManagedProfiles = async () => {
    const res = await fetch('/api/admin/profiles', { credentials: 'include' })
    if (res.ok) managedProfiles.value = (await res.json()).profiles || []
}

const createProfile = async () => {
    const res = await fetch('/api/admin/profiles', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(newProfile.value)
    })
    if (res.ok) {
        newProfile.value = { name: '', description: '', icon: 'database', color: '#8774e1', url: '' }
        await loadManagedProfiles()
    }
}

const deleteProfile = async (id) => {
    if (!confirm('Delete this profile?')) return
    await fetch(`/api/admin/profiles/${id}`, { method: 'DELETE', credentials: 'include' })
    await loadManagedProfiles()
}

const loadManagedAdmins = async () => {
    const res = await fetch('/api/admin/admins', { credentials: 'include' })
    if (res.ok) managedAdmins.value = (await res.json()).admins || []
}

const createAdmin = async () => {
    const res = await fetch('/api/admin/admins', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
            username: newAdmin.value.username,
            password: newAdmin.value.password,
            display_name: newAdmin.value.displayName,
            allowed_profile_ids: newAdmin.value.profileIds
        })
    })
    if (res.ok) {
        newAdmin.value = { username: '', password: '', displayName: '', profileIds: [] }
        await loadManagedAdmins()
    }
}

const deleteAdmin = async (id) => {
    if (!confirm('Delete this admin?')) return
    await fetch(`/api/admin/admins/${id}`, { method: 'DELETE', credentials: 'include' })
    await loadManagedAdmins()
}
```

**Return block additions:**
```javascript
superAdminTab, managedProfiles, managedAdmins, newProfile, newAdmin,
loadSuperAdminData, loadManagedProfiles, createProfile, deleteProfile,
loadManagedAdmins, createAdmin, deleteAdmin,
```

## Implementation Steps
1. Add Super Admin tab button (visible only to `super_admin`)
2. Update Admin tab visibility to include `admin` role
3. Add Super Admin panel with Profiles / Admins / Login Design sub-tabs
4. Add profile CRUD UI (list + create form + edit/delete)
5. Add admin CRUD UI with profile assignment checkboxes
6. Add stub for Login Design panel
7. Wire JS refs, functions, and return block
8. Update `userRole` checks throughout settings for backward compat (`master` → `super_admin`)

## Success Criteria
- Super admin sees "Super Admin" tab with crown icon
- Regular admin does NOT see "Super Admin" tab
- Profile CRUD works: create, list, edit, delete
- Admin CRUD works: create with profile assignment, list, delete
- Login Design tab shows "coming soon" stub
- Existing admin features (viewers, tokens, backup, etc.) still accessible

## Risk Assessment
- Large HTML file — insert at correct position to avoid breaking template
- `userRole === 'master'` checks must also accept `super_admin` (search all occurrences)
- No security risk — all CRUD endpoints require `super_admin` backend auth
