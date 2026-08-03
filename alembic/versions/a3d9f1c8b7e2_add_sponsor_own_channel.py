"""add sponsor own channel

Revision ID: a3d9f1c8b7e2
Revises: d2e8f6a1c5b3
Create Date: 2026-08-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a3d9f1c8b7e2"
down_revision: str | None = "d2e8f6a1c5b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sponsors",
        sa.Column("own_channel", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("sponsors", "own_channel")
