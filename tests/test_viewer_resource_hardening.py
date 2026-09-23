"""Viewer resource limits: the per-socket subscription cap, thumbnail decode
memory and atomicity, and share-token hashing off the event loop."""

import asyncio
import hashlib
import secrets

import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base
from src.web import thumbnails
from src.web.dependencies import ConnectionManager


@pytest_asyncio.fixture
async def viewer_adapter():
    """In-memory SQLite adapter, for the token path that must hash off-loop."""
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    db_manager = DatabaseManager.__new__(DatabaseManager)
    db_manager.engine = engine
    db_manager.database_url = "sqlite+aiosqlite://"
    db_manager._is_sqlite = True
    db_manager.async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    yield DatabaseAdapter(db_manager)
    await engine.dispose()


class TestSubscriptionCap:
    def test_socket_cannot_exceed_the_subscription_cap(self):
        manager = ConnectionManager()
        socket = object()
        manager.active_connections[socket] = set()
        manager._allowed_chats[socket] = None

        for chat_id in range(manager._MAX_SUBSCRIPTIONS_PER_SOCKET):
            manager.subscribe(socket, chat_id)
        assert len(manager.active_connections[socket]) == manager._MAX_SUBSCRIPTIONS_PER_SOCKET

        manager.subscribe(socket, 999_999)
        assert 999_999 not in manager.active_connections[socket]
        assert len(manager.active_connections[socket]) == manager._MAX_SUBSCRIPTIONS_PER_SOCKET

    def test_resubscribing_an_existing_chat_at_the_cap_still_works(self):
        manager = ConnectionManager()
        socket = object()
        manager.active_connections[socket] = set()
        manager._allowed_chats[socket] = None
        for chat_id in range(manager._MAX_SUBSCRIPTIONS_PER_SOCKET):
            manager.subscribe(socket, chat_id)

        manager.subscribe(socket, 0)
        assert 0 in manager.active_connections[socket]


class TestThumbnailPixelGate:
    def test_declared_size_over_the_cap_is_refused(self, tmp_path, monkeypatch):
        # A tiny file whose declared dimensions exceed the cap: the gate must read
        # the declared size, so no large image has to be built in the test.
        source = tmp_path / "big.png"
        Image.new("RGB", (40, 40), "white").save(source)
        monkeypatch.setattr(thumbnails, "_MAX_SOURCE_PIXELS", 100)

        assert thumbnails._generate_sync(source, tmp_path / "out.webp", 200) is False
        assert not (tmp_path / "out.webp").exists()

    def test_image_under_the_cap_is_generated(self, tmp_path):
        source = tmp_path / "small.png"
        Image.new("RGB", (40, 40), "white").save(source)
        dest = tmp_path / "thumbs" / "small.webp"

        assert thumbnails._generate_sync(source, dest, 200) is True
        with Image.open(dest) as thumb:
            assert thumb.format == "WEBP"

    def test_jpeg_gate_is_applied_before_draft_shrinks_the_size(self, tmp_path, monkeypatch):
        # draft() rewrites img.size, so a gate placed after it would read the
        # shrunken size and let a full-size progressive decode through.
        source = tmp_path / "big.jpg"
        Image.new("RGB", (400, 400), "white").save(source, "JPEG", progressive=True)
        monkeypatch.setattr(thumbnails, "_MAX_SOURCE_PIXELS", 400 * 400 - 1)

        assert thumbnails._generate_sync(source, tmp_path / "out.webp", 64) is False


class TestAtomicThumbnailWrite:
    def test_write_leaves_no_temp_file_and_a_complete_image(self, tmp_path):
        dest = tmp_path / "thumb.webp"
        img = Image.new("RGB", (20, 20), "red")

        thumbnails._save_webp_atomic(img, dest, 80)

        with Image.open(dest) as written:
            assert written.size == (20, 20)
        assert list(tmp_path.glob(".thumb-*")) == []

    def test_a_failed_save_never_leaves_a_partial_cache_entry(self, tmp_path, monkeypatch):
        dest = tmp_path / "thumb.webp"
        img = Image.new("RGB", (20, 20), "red")

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Image.Image, "save", boom)
        with pytest.raises(OSError):
            thumbnails._save_webp_atomic(img, dest, 80)

        assert not dest.exists()
        assert list(tmp_path.glob(".thumb-*")) == []


class TestFailureCache:
    def test_recorded_failure_is_remembered_then_expires(self, monkeypatch):
        thumbnails._recent_failures.clear()
        clock = {"now": 1000.0}
        monkeypatch.setattr(thumbnails.time, "monotonic", lambda: clock["now"])

        key = (200, "/data/backups/media/126/broken.mp4")
        assert thumbnails._failure_cached(key) is False
        thumbnails._record_failure(key)
        assert thumbnails._failure_cached(key) is True

        clock["now"] += thumbnails._FAILURE_TTL_SECONDS + 1
        assert thumbnails._failure_cached(key) is False
        assert key not in thumbnails._recent_failures

    def test_cache_stays_bounded(self, monkeypatch):
        thumbnails._recent_failures.clear()
        monkeypatch.setattr(thumbnails, "_MAX_FAILURE_ENTRIES", 4)
        for i in range(20):
            thumbnails._record_failure((200, f"/media/{i}.mp4"))
        assert len(thumbnails._recent_failures) <= 4


class TestTokenHashingOffLoop:
    """600k PBKDF2 rounds per stored token must not run on the event loop."""

    @pytest.mark.asyncio
    async def test_verify_still_matches_and_hashes_in_a_thread(self, viewer_adapter, monkeypatch):
        from src.db import adapter_viewer

        salt = secrets.token_hex(16)
        plaintext = "share-token-value"
        token_hash = hashlib.pbkdf2_hmac("sha256", plaintext.encode(), bytes.fromhex(salt), 600_000).hex()
        await viewer_adapter.create_viewer_token(
            label="test", token_hash=token_hash, token_salt=salt, created_by="alaa", allowed_chat_ids="126"
        )

        offloaded: list[int] = []
        real_to_thread = asyncio.to_thread

        async def counting_to_thread(func, *args, **kwargs):
            offloaded.append(1)
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(adapter_viewer.asyncio, "to_thread", counting_to_thread)

        matched = await viewer_adapter.verify_viewer_token(plaintext)
        assert matched is not None
        assert matched["allowed_chat_ids"] == "126"
        assert offloaded, "PBKDF2 must be dispatched through asyncio.to_thread"

        assert await viewer_adapter.verify_viewer_token("wrong-token") is None
