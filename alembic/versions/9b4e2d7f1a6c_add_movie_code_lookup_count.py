"""add movie code lookup count

Revision ID: 9b4e2d7f1a6c
Revises: 4e5f6a7b8c9d
Create Date: 2026-08-01
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "9b4e2d7f1a6c"
down_revision: str | None = "4e5f6a7b8c9d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "movie_codes",
        sa.Column("lookup_count", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("movie_codes", "lookup_count")
