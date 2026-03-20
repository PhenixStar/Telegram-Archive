# Phase 2: Role Hierarchy + Super Admin Backend

## Priority: High
## Status: Planned

## Overview
Introduce `super_admin` role as the top-level authority. Current `master` role becomes `admin`. New DB table `backup_profiles` replaces env/file-based profile config. Super admin credentials set via env vars (like current master). Admins are DB-backed accounts with profile assignments.

## Key Insights
- Current roles: `master` (env var creds), `viewer` (DB `viewer_accounts`), `token` (DB `viewer_tokens`)
- `require_master()` dependency used by ~20 endpoints (line 1007)
- `UserContext.role` string field — easy to extend
- `ViewerAccount` model already has `allowed_chat_ids`, `is_active`, `no_download`, `created_by`
- Sessions persist in `viewer_sessions` table with `role` column

## Role Hierarchy (New)
```
super_admin  — env var creds (SUPER_ADMIN_USERNAME/SUPER_ADMIN_PASSWORD)
               OR falls back to VIEWER_USERNAME/VIEWER_PASSWORD for backward compat
               Full access to everything. Creates/manages profiles and admins.

admin        — DB-backed account (new `admin_accounts` table)
               Assigned to specific profiles. Can rename their profiles,
               create viewers/tokens within their profile scope.

viewer       — DB-backed (existing `viewer_accounts`)
               Read-only, scoped to allowed_chat_ids.

token        — DB-backed (existing `viewer_tokens`)
               Scoped read-only via share link.
```

## DB Schema Changes

### New table: `backup_profiles`
```python
class BackupProfile(Base):
    __tablename__ = "backup_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # slug: "main", "team-a"
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str] = mapped_column(String(50), default="database")
    color: Mapped[str] = mapped_column(String(20), default="#8774e1")
    url: Mapped[str | None] = mapped_column(Text)  # external viewer URL, NULL = current instance
    db_path: Mapped[str | None] = mapped_column(Text)  # path to SQLite DB for this profile
    is_active: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
```

### New table: `admin_accounts`
```python
class AdminAccount(Base):
    __tablename__ = "admin_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    salt: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    allowed_profile_ids: Mapped[str | None] = mapped_column(Text)  # JSON array of profile IDs, NULL = all
    is_active: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_by: Mapped[str | None] = mapped_column(String(255))  # super_admin username
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
```

### Migration: `viewer_sessions.role`
- Existing `role="master"` sessions → treat as `super_admin` at runtime
- No schema change needed — just update logic in `require_auth` and `require_master`

## Backend API Changes

### Auth flow updates (`src/web/main.py`)

**Login order (updated):**
1. Check DB admin accounts → role `admin`
2. Check DB viewer accounts → role `viewer` (existing)
3. Check env var super admin creds → role `super_admin`
4. Backward compat: if `SUPER_ADMIN_*` not set, use `VIEWER_USERNAME`/`VIEWER_PASSWORD` → `super_admin`

**New env vars:**
```
SUPER_ADMIN_USERNAME (optional, falls back to VIEWER_USERNAME)
SUPER_ADMIN_PASSWORD (optional, falls back to VIEWER_PASSWORD)
```

**Updated dependencies:**
```python
def require_super_admin(user: UserContext = Depends(require_auth)) -> UserContext:
    if user.role != "super_admin":
        raise HTTPException(403, "Super admin access required")
    return user

def require_admin_or_above(user: UserContext = Depends(require_auth)) -> UserContext:
    if user.role not in ("super_admin", "admin"):
        raise HTTPException(403, "Admin access required")
    return user
```

**Backward compat for `require_master()`:**
```python
def require_master(request: Request, user: UserContext = Depends(require_auth)) -> UserContext:
    # Accept both super_admin and admin (backward compat for existing endpoints)
    if user.role not in ("master", "super_admin", "admin"):
        raise HTTPException(403, "Admin access required")
    ...
```

### New API endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/admin/profiles` | super_admin | List all backup profiles |
| POST | `/api/admin/profiles` | super_admin | Create backup profile |
| PUT | `/api/admin/profiles/{id}` | super_admin OR admin (own) | Update profile |
| DELETE | `/api/admin/profiles/{id}` | super_admin | Delete profile |
| GET | `/api/admin/admins` | super_admin | List admin accounts |
| POST | `/api/admin/admins` | super_admin | Create admin account |
| PUT | `/api/admin/admins/{id}` | super_admin | Update admin (profile assignments) |
| DELETE | `/api/admin/admins/{id}` | super_admin | Delete admin account |

**Profile rename by admin:**
- PUT `/api/admin/profiles/{id}` — admin can only change `name`/`description` of profiles they're assigned to
- Super admin can change all fields

### Updated `/api/profiles` (public, login page)
```python
@app.get("/api/profiles")
async def get_profiles():
    """Return profiles for login page. DB-first, env fallback."""
    if db:
        profiles = await db.list_backup_profiles(active_only=True)
        if profiles:
            return {"profiles": profiles, "show_selector": True}
    # Fallback: env var / file / auto-generate default
    # ... existing logic + auto-generate default ...
```

### Updated `/api/auth/check`
Return role info that frontend uses to show/hide UI:
```python
return {
    "authenticated": True,
    "role": session.role,  # "super_admin", "admin", "viewer", "token"
    "username": session.username,
    "is_super_admin": session.role == "super_admin",
    "allowed_profile_ids": session.allowed_profile_ids,  # admin only
}
```

## Files to Modify
- `src/db/models.py` — Add `BackupProfile`, `AdminAccount` models
- `src/db/adapter.py` — Add CRUD methods for profiles and admins
- `src/web/main.py` — Auth flow, new endpoints, updated dependencies
- `docker-compose.yml` — Add `SUPER_ADMIN_*` env var docs

## Implementation Steps
1. Add `BackupProfile` and `AdminAccount` to `models.py`
2. Add adapter methods: `list_backup_profiles`, `create_backup_profile`, `update_backup_profile`, `delete_backup_profile`, `get_admin_by_username`, `list_admins`, `create_admin`, `update_admin`, `delete_admin`
3. Add auto-migration in `adapter.py` `ensure_tables()` for new tables
4. Update login flow: check admin accounts before env var fallback
5. Add `require_super_admin()`, `require_admin_or_above()` dependencies
6. Update `require_master()` to accept `super_admin` and `admin`
7. Add profile CRUD API endpoints (super_admin only)
8. Add admin account CRUD API endpoints (super_admin only)
9. Update `/api/profiles` to query DB first
10. Update `/api/auth/check` to include `is_super_admin` flag

## Success Criteria
- Super admin can log in with env var creds
- Admin can log in with DB-backed creds
- `require_master()` still works for existing endpoints (backward compat)
- Profiles stored in DB, served to login page
- Admin CRUD restricted to super admin only
- Existing viewers and tokens unaffected

## Risk Assessment
- **Migration**: New tables only (no ALTER TABLE) — zero risk to existing data
- **Backward compat**: `master` role still accepted everywhere `super_admin` is
- **Session migration**: existing `master` sessions treated as `super_admin` at runtime
- **Env var fallback**: if no `SUPER_ADMIN_*` set, `VIEWER_USERNAME`/`VIEWER_PASSWORD` → `super_admin`
