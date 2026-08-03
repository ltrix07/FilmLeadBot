"""add broadcast scheduling

Revision ID: c1b5a3f9d2e4
Revises: 1dd0bbce1497
Create Date: 2026-08-01
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c1b5a3f9d2e4"
down_revision: str | None = "1dd0bbce1497"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("broadcasts", sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("broadcasts", "scheduled_at")
