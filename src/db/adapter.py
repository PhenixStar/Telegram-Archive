"""
Async database adapter for Telegram Backup.

Provides all database operations using SQLAlchemy async.
This is a drop-in replacement for the old Database class.

The adapter is composed from domain-focused mixins:
- MessageMixin: message CRUD, pagination, export
- MediaMixin: media files and reactions
- ViewerMixin: viewer/user accounts, sessions, tokens, audit logs
- SyncMixin: chat/user upserts, sync status, folders, statistics
- SettingsMixin: app settings key-value store
- SearchMixin: FTS5, AI/OCR, semantic search
"""

import asyncio
import json
import logging
from datetime import datetime
from functools import wraps
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .base import DatabaseManager
from .models import Metadata

logger = logging.getLogger(__name__)


def _strip_tz(dt: datetime | None) -> datetime | None:
    """Strip timezone info from datetime for PostgreSQL compatibility."""
    if dt is None:
        return None
    if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def retry_on_locked(
    max_retries: int = 5, initial_delay: float = 0.1, max_delay: float = 2.0, backoff_factor: float = 2.0
):
    """
    Decorator to retry async database operations on operational errors.

    Works for both SQLite (database locked) and PostgreSQL (connection issues).
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            delay = initial_delay
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(self, *args, **kwargs)
                except Exception as e:
                    error_str = str(e).lower()
                    if "locked" not in error_str and "connection" not in error_str:
                        raise

                    last_exception = e
                    if attempt < max_retries:
                        logger.warning(
                            f"Database error on {func.__name__}, attempt {attempt + 1}/{max_retries + 1}. "
                            f"Retrying in {delay:.2f}s... Error: {e}"
                        )
                        await asyncio.sleep(delay)
                        delay = min(delay * backoff_factor, max_delay)
                    else:
                        logger.error(f"Database error on {func.__name__} after {max_retries + 1} attempts. Giving up.")
                        raise

            if last_exception:
                raise last_exception

        return wrapper

    return decorator


# Import mixins (must be after _strip_tz and retry_on_locked are defined)
from .adapter_media import MediaMixin  # noqa: E402
from .adapter_messages import MessageMixin  # noqa: E402
from .adapter_search import SearchMixin  # noqa: E402
from .adapter_settings import SettingsMixin  # noqa: E402
from .adapter_sync import SyncMixin  # noqa: E402
from .adapter_viewer import ViewerMixin  # noqa: E402


class DatabaseAdapter(
    MessageMixin,
    MediaMixin,
    ViewerMixin,
    SyncMixin,
    SettingsMixin,
    SearchMixin,
):
    """
    Async database adapter compatible with the old Database class interface.

    All methods are async and should be awaited.
    Composed from domain-focused mixins for maintainability.
    """

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize adapter with a DatabaseManager.

        Args:
            db_manager: Initialized DatabaseManager instance
        """
        self.db_manager = db_manager
        self._is_sqlite = db_manager._is_sqlite

    def _serialize_raw_data(self, raw_data: Any) -> str:
        """
        Safely serialize raw_data to JSON.

        Args:
            raw_data: Data to serialize

        Returns:
            JSON string representation
        """
        if not raw_data:
            return "{}"

        try:
            return json.dumps(raw_data)
        except (TypeError, ValueError) as e:
            logger.warning(f"Failed to serialize raw_data directly: {e}")
            try:

                def convert_to_serializable(obj):
                    if isinstance(obj, dict):
                        return {k: convert_to_serializable(v) for k, v in obj.items()}
                    elif isinstance(obj, list):
                        return [convert_to_serializable(item) for item in obj]
                    elif isinstance(obj, (str, int, float, bool, type(None))):
                        return obj
                    else:
                        return str(obj)

                serializable_data = convert_to_serializable(raw_data)
                return json.dumps(serializable_data)
            except Exception as e2:
                logger.error(f"Failed to serialize raw_data even after conversion: {e2}")
                return "{}"

    # ========== Metadata Operations ==========

    async def set_metadata(self, key: str, value: str) -> None:
        """Set a metadata key-value pair."""
        async with self.db_manager.async_session_factory() as session:
            # Use upsert
            if self._is_sqlite:
                stmt = sqlite_insert(Metadata).values(key=key, value=value)
                stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value})
            else:
                stmt = pg_insert(Metadata).values(key=key, value=value)
                stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value})
            await session.execute(stmt)
            await session.commit()

    async def get_metadata(self, key: str) -> str | None:
        """Get a metadata value by key."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(Metadata.value).where(Metadata.key == key))
            row = result.scalar_one_or_none()
            return row

    async def close(self) -> None:
        """Close database connections."""
        await self.db_manager.close()
