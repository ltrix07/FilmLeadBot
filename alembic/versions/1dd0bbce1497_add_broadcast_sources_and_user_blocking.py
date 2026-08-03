"""add broadcast sources and user blocking

Revision ID: 1dd0bbce1497
Revises: 413a0ca4190d
Create Date: 2026-08-01
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "1dd0bbce1497"
down_revision: str | None = "413a0ca4190d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("broadcasts", sa.Column("source_chat_id", sa.BigInteger(), nullable=False))
    op.add_column("broadcasts", sa.Column("source_message_id", sa.BigInteger(), nullable=False))
    op.add_column("users", sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "blocked_at")
    op.drop_column("broadcasts", "source_message_id")
    op.drop_column("broadcasts", "source_chat_id")
