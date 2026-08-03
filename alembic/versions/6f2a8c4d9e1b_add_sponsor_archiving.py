"""add sponsor archiving

Revision ID: 6f2a8c4d9e1b
Revises: f3a9d7c2e5b1
Create Date: 2026-08-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "6f2a8c4d9e1b"
down_revision: str | None = "f3a9d7c2e5b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sponsors", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("sponsors", "archived_at")
