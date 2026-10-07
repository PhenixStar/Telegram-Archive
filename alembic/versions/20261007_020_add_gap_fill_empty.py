"""Remember message-id gaps the gap-fill found empty.

Gap-fill re-asked Telegram about every gap on every scheduled run, and nearly
all of them are ranges of deleted messages: on a large archive that took ~10h
per run, longer than the schedule interval. ``gap_fill_empty`` records gaps
whose exact bounds were fetched cleanly and returned nothing, so later runs
skip them. Idempotent: a create_all() database already has the table.

Revision ID: 020
Revises: 019
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "020"
down_revision: str | None = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "gap_fill_empty" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "gap_fill_empty",
            sa.Column("chat_id", sa.BigInteger(), primary_key=True),
            sa.Column("gap_start", sa.BigInteger(), primary_key=True),
            sa.Column("gap_end", sa.BigInteger(), primary_key=True),
            sa.Column("checked_at", sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    if "gap_fill_empty" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("gap_fill_empty")
