"""cache external scheduler access observations

Revision ID: 20260731_0007
Revises: 20260731_0006
Create Date: 2026-07-31
"""

from alembic import op
import sqlalchemy as sa


revision = "20260731_0007"
down_revision = "20260731_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduler_targets",
        sa.Column("access_status", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "scheduler_targets",
        sa.Column("access_message", sa.String(length=2000), nullable=True),
    )
    op.add_column(
        "scheduler_targets",
        sa.Column("access_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scheduler_targets", "access_checked_at")
    op.drop_column("scheduler_targets", "access_message")
    op.drop_column("scheduler_targets", "access_status")
