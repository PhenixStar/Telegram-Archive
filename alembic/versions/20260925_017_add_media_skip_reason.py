"""Record why a media row will not download on its own.

A row sits at ``downloaded = 0`` for a file over ``MAX_MEDIA_SIZE_MB``, one the
``DOWNLOAD_MEDIA_TYPES`` / ``DOWNLOAD_DOCUMENT_MIME_TYPES`` filter declined, one
that is gone from Telegram (view-once or timer media, a deleted message), or a
download that failed and will be retried. The viewer said "Will download on
next backup" for all of them. The backup records the reason on the row and the
viewer reads it (semantic port of upstream #474, migration 030 there).

Nullable, no backfill here: the backup classifies existing rows on its next
run. Idempotent: a database built by create_all() already has the column.

Revision ID: 017
Revises: 016
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "017"
down_revision: str | None = "016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "media"
COLUMN_NAME = "skip_reason"


def _has_column(inspector: sa.Inspector) -> bool | None:
    """Whether media has the column; None when there is no media table at all."""
    if TABLE_NAME not in inspector.get_table_names():
        return None
    return COLUMN_NAME in {c["name"] for c in inspector.get_columns(TABLE_NAME)}


def upgrade() -> None:
    if _has_column(sa.inspect(op.get_bind())) is False:
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.String(16), nullable=True))


def downgrade() -> None:
    if _has_column(sa.inspect(op.get_bind())):
        op.drop_column(TABLE_NAME, COLUMN_NAME)
