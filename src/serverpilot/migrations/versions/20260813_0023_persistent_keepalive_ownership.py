"""make internal keepalive ownership persistent

Revision ID: 20260813_0023
Revises: 20260813_0022
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa


revision = "20260813_0023"
down_revision = "20260813_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "keepalive_current",
        sa.Column("gpu_id", sa.String(length=260), nullable=False),
        sa.Column("actual", sa.String(length=16), nullable=False),
        sa.Column("error_reason", sa.String(length=1000)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("actual IN ('ON', 'OFF', 'ERROR')", name="ck_keepalive_actual"),
        sa.ForeignKeyConstraint(["gpu_id"], ["gpu_devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("gpu_id"),
    )
    op.execute(
        "UPDATE leases SET expires_at = NULL "
        "WHERE kind = 'keepalive' "
        "AND state IN ('HELD', 'ACTIVE', 'ORPHANED_BUSY', 'CONFLICT') "
        "AND EXISTS ("
        "SELECT 1 FROM lease_resources "
        "WHERE lease_resources.lease_id = leases.id "
        "AND lease_resources.active = 1"
        ")"
    )


def downgrade() -> None:
    # There is no safe synthetic expiry for controller-owned workers.  Older
    # code can still read NULL because the column became nullable in 0021.
    op.drop_table("keepalive_current")
