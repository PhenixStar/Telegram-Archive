"""Keep every profile photo sighting (semantic port of upstream #469/#479).

``chats.avatar_photo_id`` records the photo id currently seen for a chat, so
the viewer serves that file rather than whichever avatar file is newest, and
a recorded removal shows no avatar. ``avatar_history`` is append-only: a row
is added whenever the recorded photo changes, a removal is a row with a NULL
photo id. Nothing is deleted; every avatar file stays on disk.

No seed: history starts with the next backup run. The viewer's "Previous
photos" strip also lists avatar files already on disk, so older photos show
without it. Idempotent: a create_all() database already has both.

Revision ID: 018
Revises: 017
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "018"
down_revision: str | None = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = inspector.get_table_names()
    if "chats" in tables and "avatar_photo_id" not in {c["name"] for c in inspector.get_columns("chats")}:
        op.add_column("chats", sa.Column("avatar_photo_id", sa.BigInteger(), nullable=True))
    if "avatar_history" not in tables:
        op.create_table(
            "avatar_history",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("photo_id", sa.BigInteger(), nullable=True),
            sa.Column("seen_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_avatar_history_chat_seen", "avatar_history", ["chat_id", "seen_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = inspector.get_table_names()
    if "avatar_history" in tables:
        op.drop_index("ix_avatar_history_chat_seen", table_name="avatar_history")
        op.drop_table("avatar_history")
    if "chats" in tables and "avatar_photo_id" in {c["name"] for c in inspector.get_columns("chats")}:
        op.drop_column("chats", "avatar_photo_id")
