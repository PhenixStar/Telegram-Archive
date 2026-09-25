"""Persist an admin session's account (profile) scope.

``viewer_sessions`` stored chat scoping but not ``allowed_profile_ids``, so a
session reloaded from the database after a viewer restart lost it, and an
admin limited to some accounts came back unrestricted. Nullable column, NULL =
unrestricted, which is what every existing (super_admin / token) session is.
Idempotent: a create_all() database already has it.

Revision ID: 019
Revises: 018
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "019"
down_revision: str | None = "018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(inspector: sa.Inspector) -> bool | None:
    if "viewer_sessions" not in inspector.get_table_names():
        return None
    return "allowed_profile_ids" in {c["name"] for c in inspector.get_columns("viewer_sessions")}


def upgrade() -> None:
    if _has_column(sa.inspect(op.get_bind())) is False:
        op.add_column("viewer_sessions", sa.Column("allowed_profile_ids", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column(sa.inspect(op.get_bind())):
        op.drop_column("viewer_sessions", "allowed_profile_ids")
