"""Add immutable per-message sender name snapshots.

Revision ID: 016
Revises: 015
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "016"
down_revision: str | None = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "messages"
COLUMN_NAME = "sender_name"


def _column_exists(inspector: sa.Inspector) -> bool:
    return COLUMN_NAME in {column["name"] for column in inspector.get_columns(TABLE_NAME)}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if TABLE_NAME not in inspector.get_table_names():
        return
    if _column_exists(inspector):
        return
    # Additive, nullable column — safe on the existing production dataset. Uses
    # batch mode so it also works on SQLite (no ALTER TABLE ADD COLUMN limits
    # here, but batch mode keeps upgrade/downgrade symmetric on both backends).
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.add_column(sa.Column(COLUMN_NAME, sa.Text(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if TABLE_NAME not in inspector.get_table_names():
        return
    if not _column_exists(inspector):
        return
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.drop_column(COLUMN_NAME)
