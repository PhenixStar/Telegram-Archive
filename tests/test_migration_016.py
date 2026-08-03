"""Tests for Alembic migration 016 (message sender_name column)."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260803_016_add_message_sender_name.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_016", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(conn, func):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        func()


def _create_messages_table(conn):
    conn.execute(
        sa.text(
            "CREATE TABLE messages ("
            "id BIGINT NOT NULL, "
            "chat_id BIGINT NOT NULL, "
            "sender_id BIGINT, "
            "text TEXT, "
            "PRIMARY KEY (id, chat_id)"
            ")"
        )
    )


def test_revision_chain():
    migration = _load_migration()
    assert migration.revision == "016"
    assert migration.down_revision == "015"


def test_upgrade_adds_nullable_column_and_is_idempotent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table(conn)

        _run(conn, migration.upgrade)
        inspector = sa.inspect(conn)
        columns = {c["name"]: c for c in inspector.get_columns("messages")}
        assert "sender_name" in columns
        assert columns["sender_name"]["nullable"] is True

        # Re-run must be a no-op (no duplicate-column error).
        _run(conn, migration.upgrade)
        assert "sender_name" in {c["name"] for c in sa.inspect(conn).get_columns("messages")}


def test_upgrade_is_additive_and_preserves_existing_rows():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table(conn)
        conn.execute(sa.text("INSERT INTO messages (id, chat_id, sender_id, text) VALUES (1, 100, 7, 'hi')"))
        conn.commit()

        _run(conn, migration.upgrade)
        conn.commit()

        row = conn.execute(sa.text("SELECT id, chat_id, sender_id, text, sender_name FROM messages")).one()
        assert row.id == 1
        assert row.chat_id == 100
        assert row.text == "hi"
        assert row.sender_name is None


def test_downgrade_drops_column_and_is_idempotent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table(conn)
        _run(conn, migration.upgrade)

        _run(conn, migration.downgrade)
        assert "sender_name" not in {c["name"] for c in sa.inspect(conn).get_columns("messages")}

        # Re-run must be a no-op.
        _run(conn, migration.downgrade)
        assert "sender_name" not in {c["name"] for c in sa.inspect(conn).get_columns("messages")}


def test_upgrade_downgrade_noop_when_table_missing():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        # No messages table created — both directions must return cleanly.
        _run(conn, migration.upgrade)
        _run(conn, migration.downgrade)
