"""add admin permissions

Revision ID: b4e8f2a1c6d9
Revises: a3d9f1c8b7e2
Create Date: 2026-08-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b4e8f2a1c6d9"
down_revision: str | None = "a3d9f1c8b7e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("admins", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("admins", sa.Column("can_manage_admins", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("admins", sa.Column("can_manage_payouts", sa.Boolean(), server_default="false", nullable=False))
    op.execute("UPDATE admins SET can_manage_admins = true, can_manage_payouts = true WHERE role = 'owner'")


def downgrade() -> None:
    op.drop_column("admins", "can_manage_payouts")
    op.drop_column("admins", "can_manage_admins")
    op.drop_column("admins", "revoked_at")
