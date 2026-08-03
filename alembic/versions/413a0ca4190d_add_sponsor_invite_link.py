"""add sponsor invite_link

Revision ID: 413a0ca4190d
Revises: 580108d867a2
Create Date: 2026-08-01
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "413a0ca4190d"
down_revision: str | None = "580108d867a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sponsors", sa.Column("invite_link", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sponsors", "invite_link")
