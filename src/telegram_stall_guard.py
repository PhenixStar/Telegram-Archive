"""Bounded waits for Telegram requests so a dead connection can never hang a backup.

When Telethon's automatic reconnection gives up ("Automatic reconnection failed N
time(s)"), requests that were already in flight are never resolved: the awaiting
coroutine blocks forever. A scheduled backup stuck that way holds its APScheduler
instance slot, so every later run is skipped and backups silently stop.

These helpers put an upper bound on each single Telegram round-trip. A timed-out
wait raises ``TimeoutError``, which the backup loops already treat as a connection
error (heal the connection, move on to the next item).
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


def _positive_float_env(name: str, default: float) -> float | None:
    """Read a timeout from the environment; ``0`` or a negative value disables it."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default=%s", name, raw, default)
        return default
    return value if value > 0 else None


# Upper bound for one one-shot Telegram call (get_entity, get_dialogs, get_messages...).
# Generous because Telethon may sleep internally for flood waits up to
# FLOOD_SLEEP_THRESHOLD and get_dialogs pages through thousands of chats.
TELEGRAM_CALL_TIMEOUT_SECONDS = _positive_float_env("TELEGRAM_CALL_TIMEOUT_SECONDS", 900.0)

# Upper bound for fetching the next message from an iterator. Only the fetch is
# timed: work done by the consumer between items (media downloads, DB writes) is not.
TELEGRAM_ITER_STALL_TIMEOUT_SECONDS = _positive_float_env("TELEGRAM_ITER_STALL_TIMEOUT_SECONDS", 300.0)


async def with_call_timeout(awaitable, timeout: float | None):
    """Await ``awaitable``, raising ``TimeoutError`` after ``timeout`` seconds (``None`` = unbounded)."""
    # asyncio.timeout runs the await in the current task (no Task per call, which
    # matters when iterating thousands of messages); None means no deadline.
    async with asyncio.timeout(timeout):
        return await awaitable


async def iter_with_stall_timeout[T](
    iterable: AsyncIterator[T], timeout: float | None = TELEGRAM_ITER_STALL_TIMEOUT_SECONDS
) -> AsyncIterator[T]:
    """Yield from ``iterable``, raising ``TimeoutError`` if fetching one item stalls past ``timeout``."""
    iterator = iterable.__aiter__()
    while True:
        try:
            item = await with_call_timeout(iterator.__anext__(), timeout)
        except StopAsyncIteration:
            return
        except TimeoutError:
            logger.error("Telegram fetch stalled for %ss with no response; aborting this iteration", timeout)
            raise
        yield item
