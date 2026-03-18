"""App settings database operations mixin.

Provides key-value store for application settings.
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select

from .adapter import retry_on_locked
from .models import AppSettings

logger = logging.getLogger(__name__)


class SettingsMixin:
    """Mixin providing app settings key-value operations.

    Assumes ``self.db_manager`` and ``self._is_sqlite`` are set by the host class.
    """

    @retry_on_locked()
    async def set_setting(self, key: str, value: str) -> None:
        """Set a key-value setting (upsert)."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        async with self.db_manager.async_session_factory() as session:
            if self._is_sqlite:
                stmt = sqlite_insert(AppSettings).values(key=key, value=value, updated_at=datetime.utcnow())
                stmt = stmt.on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": value, "updated_at": datetime.utcnow()},
                )
            else:
                stmt = pg_insert(AppSettings).values(key=key, value=value, updated_at=datetime.utcnow())
                stmt = stmt.on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": value, "updated_at": datetime.utcnow()},
                )
            await session.execute(stmt)
            await session.commit()

    async def get_setting(self, key: str) -> str | None:
        """Get a setting value by key. Returns None if not found."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(AppSettings).where(AppSettings.key == key))
            row = result.scalar_one_or_none()
            return row.value if row else None

    async def get_all_settings(self) -> dict[str, str]:
        """Get all settings as a dict."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(AppSettings))
            return {row.key: row.value for row in result.scalars().all()}
