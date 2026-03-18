"""Viewer account, user account, session, token, and audit log operations mixin.

Handles all authentication and access-control related database operations.
"""

import hashlib
import logging
import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select

from .adapter import retry_on_locked
from .models import UserAccount, ViewerAccount, ViewerAuditLog, ViewerSession, ViewerToken

logger = logging.getLogger(__name__)


class ViewerMixin:
    """Mixin providing viewer/user account, session, token, and audit operations.

    Assumes ``self.db_manager`` and ``self._is_sqlite`` are set by the host class.
    """

    # ========================================================================
    # Viewer Account Management (v7.0.0)
    # ========================================================================

    @retry_on_locked()
    async def create_viewer_account(
        self,
        username: str,
        password_hash: str,
        salt: str,
        allowed_chat_ids: str | None = None,
        created_by: str | None = None,
        is_active: int = 1,
        no_download: int = 0,
    ) -> dict[str, Any]:
        """Create a new viewer account. Returns the created account dict."""
        async with self.db_manager.async_session_factory() as session:
            account = ViewerAccount(
                username=username,
                password_hash=password_hash,
                salt=salt,
                allowed_chat_ids=allowed_chat_ids,
                created_by=created_by,
                is_active=is_active,
                no_download=no_download,
            )
            session.add(account)
            await session.commit()
            await session.refresh(account)
            return self._viewer_account_to_dict(account)

    async def get_viewer_account(self, account_id: int) -> dict[str, Any] | None:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerAccount).where(ViewerAccount.id == account_id))
            account = result.scalar_one_or_none()
            return self._viewer_account_to_dict(account) if account else None

    async def get_viewer_by_username(self, username: str) -> dict[str, Any] | None:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerAccount).where(ViewerAccount.username == username))
            account = result.scalar_one_or_none()
            return self._viewer_account_to_dict(account) if account else None

    async def get_all_viewer_accounts(self) -> list[dict[str, Any]]:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerAccount).order_by(ViewerAccount.created_at.desc()))
            return [self._viewer_account_to_dict(a) for a in result.scalars().all()]

    @retry_on_locked()
    async def update_viewer_account(self, account_id: int, **kwargs) -> dict[str, Any] | None:
        """Update viewer account fields. Returns updated account or None if not found."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerAccount).where(ViewerAccount.id == account_id))
            account = result.scalar_one_or_none()
            if not account:
                return None
            for key, value in kwargs.items():
                if hasattr(account, key):
                    setattr(account, key, value)
            account.updated_at = datetime.utcnow()
            await session.commit()
            await session.refresh(account)
            return self._viewer_account_to_dict(account)

    @retry_on_locked()
    async def delete_viewer_account(self, account_id: int) -> bool:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerAccount).where(ViewerAccount.id == account_id))
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    def _viewer_account_to_dict(account: ViewerAccount) -> dict[str, Any]:
        return {
            "id": account.id,
            "username": account.username,
            "password_hash": account.password_hash,
            "salt": account.salt,
            "allowed_chat_ids": account.allowed_chat_ids,
            "is_active": account.is_active,
            "no_download": account.no_download,
            "created_by": account.created_by,
            "created_at": account.created_at.isoformat() if account.created_at else None,
            "updated_at": account.updated_at.isoformat() if account.updated_at else None,
        }

    # ========================================================================
    # User Account Management (v11.0.0) — super_admin + admin roles
    # ========================================================================

    @retry_on_locked()
    async def create_user_account(self, **kwargs) -> dict[str, Any]:
        """Create a super_admin or admin user account."""
        async with self.db_manager.async_session_factory() as session:
            account = UserAccount(**kwargs)
            session.add(account)
            await session.commit()
            await session.refresh(account)
            return self._user_account_to_dict(account)

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """Look up a user account (super_admin/admin) by username."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(UserAccount).where(UserAccount.username == username))
            account = result.scalar_one_or_none()
            return self._user_account_to_dict(account) if account else None

    async def list_user_accounts(self, role: str | None = None) -> list[dict[str, Any]]:
        """List user accounts, optionally filtered by role."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(UserAccount).order_by(UserAccount.created_at.desc())
            if role:
                stmt = stmt.where(UserAccount.role == role)
            result = await session.execute(stmt)
            return [self._user_account_to_dict(a) for a in result.scalars().all()]

    @retry_on_locked()
    async def update_user_account(self, account_id: int, **kwargs) -> dict[str, Any] | None:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(UserAccount).where(UserAccount.id == account_id))
            account = result.scalar_one_or_none()
            if not account:
                return None
            for key, value in kwargs.items():
                if hasattr(account, key):
                    setattr(account, key, value)
            await session.commit()
            await session.refresh(account)
            return self._user_account_to_dict(account)

    @retry_on_locked()
    async def delete_user_account(self, account_id: int) -> bool:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(UserAccount).where(UserAccount.id == account_id))
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    def _user_account_to_dict(account: UserAccount) -> dict[str, Any]:
        return {
            "id": account.id,
            "username": account.username,
            "password_hash": account.password_hash,
            "salt": account.salt,
            "role": account.role,
            "email": account.email,
            "display_name": account.display_name,
            "allowed_profile_ids": account.allowed_profile_ids,  # raw JSON string; callers parse as needed
            "is_active": bool(account.is_active),
            "created_by": account.created_by,
            "created_at": account.created_at.isoformat() if account.created_at else None,
            "updated_at": account.updated_at.isoformat() if account.updated_at else None,
        }

    # ========================================================================
    # Viewer Audit Log (v7.0.0)
    # ========================================================================

    @retry_on_locked()
    async def create_audit_log(
        self,
        username: str,
        role: str,
        action: str,
        endpoint: str | None = None,
        chat_id: int | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        async with self.db_manager.async_session_factory() as session:
            entry = ViewerAuditLog(
                username=username,
                role=role,
                action=action,
                endpoint=endpoint,
                chat_id=chat_id,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            session.add(entry)
            await session.commit()

    async def get_audit_logs(
        self, limit: int = 100, offset: int = 0, username: str | None = None, action: str | None = None
    ) -> list[dict[str, Any]]:
        async with self.db_manager.async_session_factory() as session:
            stmt = select(ViewerAuditLog).order_by(ViewerAuditLog.created_at.desc())
            if username:
                stmt = stmt.where(ViewerAuditLog.username == username)
            if action:
                stmt = stmt.where(ViewerAuditLog.action.startswith(action))
            stmt = stmt.limit(limit).offset(offset)
            result = await session.execute(stmt)
            return [
                {
                    "id": log.id,
                    "username": log.username,
                    "role": log.role,
                    "action": log.action,
                    "endpoint": log.endpoint,
                    "chat_id": log.chat_id,
                    "ip_address": log.ip_address,
                    "user_agent": log.user_agent,
                    "created_at": log.created_at.isoformat() if log.created_at else None,
                }
                for log in result.scalars().all()
            ]

    # ========================================================================
    # Viewer Sessions (v7.1.0 - persistent sessions)
    # ========================================================================

    @retry_on_locked()
    async def save_session(
        self,
        token: str,
        username: str,
        role: str,
        allowed_chat_ids: str | None,
        created_at: float,
        last_accessed: float,
        no_download: int = 0,
        source_token_id: int | None = None,
    ) -> None:
        """Save or update a session in the database."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        async with self.db_manager.async_session_factory() as session:
            values = {
                "token": token,
                "username": username,
                "role": role,
                "allowed_chat_ids": allowed_chat_ids,
                "no_download": no_download,
                "source_token_id": source_token_id,
                "created_at": created_at,
                "last_accessed": last_accessed,
            }
            if self._is_sqlite:
                stmt = sqlite_insert(ViewerSession).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["token"],
                    set_={"last_accessed": last_accessed},
                )
            else:
                stmt = pg_insert(ViewerSession).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["token"],
                    set_={"last_accessed": last_accessed},
                )
            await session.execute(stmt)
            await session.commit()

    async def get_session(self, token: str) -> dict[str, Any] | None:
        """Get a session by token."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerSession).where(ViewerSession.token == token))
            row = result.scalar_one_or_none()
            return self._viewer_session_to_dict(row) if row else None

    async def load_all_sessions(self) -> list[dict[str, Any]]:
        """Load all sessions from the database (used on startup)."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerSession))
            return [self._viewer_session_to_dict(s) for s in result.scalars().all()]

    @retry_on_locked()
    async def delete_session(self, token: str) -> bool:
        """Delete a single session by token."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerSession).where(ViewerSession.token == token))
            await session.commit()
            return result.rowcount > 0

    @retry_on_locked()
    async def delete_user_sessions(self, username: str) -> int:
        """Delete all sessions for a given username. Returns count deleted."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerSession).where(ViewerSession.username == username))
            await session.commit()
            return result.rowcount

    @retry_on_locked()
    async def cleanup_expired_sessions(self, max_age_seconds: float) -> int:
        """Delete all expired sessions. Returns count deleted."""
        import time

        cutoff = time.time() - max_age_seconds
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerSession).where(ViewerSession.created_at < cutoff))
            await session.commit()
            return result.rowcount

    @retry_on_locked()
    async def delete_sessions_by_source_token_id(self, token_id: int) -> int:
        """Delete all sessions created from a specific share token."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerSession).where(ViewerSession.source_token_id == token_id))
            await session.commit()
            return result.rowcount

    @staticmethod
    def _viewer_session_to_dict(row: ViewerSession) -> dict[str, Any]:
        return {
            "token": row.token,
            "username": row.username,
            "role": row.role,
            "allowed_chat_ids": row.allowed_chat_ids,
            "no_download": row.no_download,
            "source_token_id": row.source_token_id,
            "created_at": row.created_at,
            "last_accessed": row.last_accessed,
        }

    # ========================================================================
    # Viewer Tokens (v7.2.0 - share tokens)
    # ========================================================================

    @retry_on_locked()
    async def create_viewer_token(
        self,
        label: str | None,
        token_hash: str,
        token_salt: str,
        created_by: str,
        allowed_chat_ids: str,
        no_download: int = 0,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Create a new share token. Returns the created token dict."""
        async with self.db_manager.async_session_factory() as session:
            token = ViewerToken(
                label=label,
                token_hash=token_hash,
                token_salt=token_salt,
                created_by=created_by,
                allowed_chat_ids=allowed_chat_ids,
                no_download=no_download,
                expires_at=expires_at,
            )
            session.add(token)
            await session.commit()
            await session.refresh(token)
            return self._viewer_token_to_dict(token)

    async def get_all_viewer_tokens(self) -> list[dict[str, Any]]:
        """Get all tokens (for admin panel)."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerToken).order_by(ViewerToken.created_at.desc()))
            return [self._viewer_token_to_dict(t) for t in result.scalars().all()]

    async def verify_viewer_token(self, plaintext_token: str) -> dict[str, Any] | None:
        """Verify a plaintext token against stored hashes. Returns token dict or None."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerToken).where(ViewerToken.is_revoked == 0))
            for record in result.scalars().all():
                if record.expires_at and record.expires_at < datetime.utcnow():
                    continue
                computed = hashlib.pbkdf2_hmac(
                    "sha256", plaintext_token.encode(), bytes.fromhex(record.token_salt), 600_000
                ).hex()
                if secrets.compare_digest(computed, record.token_hash):
                    record.last_used_at = datetime.utcnow()
                    record.use_count = (record.use_count or 0) + 1
                    await session.commit()
                    return self._viewer_token_to_dict(record)
            return None

    @retry_on_locked()
    async def update_viewer_token(self, token_id: int, **kwargs) -> dict[str, Any] | None:
        """Update token fields. Supports: label, allowed_chat_ids, is_revoked, no_download."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerToken).where(ViewerToken.id == token_id))
            token = result.scalar_one_or_none()
            if not token:
                return None
            allowed_fields = {"label", "allowed_chat_ids", "is_revoked", "no_download"}
            for key, value in kwargs.items():
                if key in allowed_fields:
                    setattr(token, key, value)
            await session.commit()
            await session.refresh(token)
            return self._viewer_token_to_dict(token)

    @retry_on_locked()
    async def delete_viewer_token(self, token_id: int) -> bool:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerToken).where(ViewerToken.id == token_id))
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    def _viewer_token_to_dict(token: ViewerToken) -> dict[str, Any]:
        return {
            "id": token.id,
            "label": token.label,
            "token_hash": token.token_hash,
            "token_salt": token.token_salt,
            "created_by": token.created_by,
            "allowed_chat_ids": token.allowed_chat_ids,
            "is_revoked": token.is_revoked,
            "no_download": token.no_download,
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
            "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
            "use_count": token.use_count,
            "created_at": token.created_at.isoformat() if token.created_at else None,
        }
