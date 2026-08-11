"""add sealed keepalive persistence fields

Revision ID: 20260811_0015
Revises: 20260810_0014
Create Date: 2026-08-11

The feature remains off for every existing endpoint. Existing leases are
classified as ordinary workload leases. No controller, lock, or fence state is
introduced here.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260811_0015"
down_revision = "20260810_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Inline checks keep SQLite on its native ADD COLUMN path. Rebuilding these
    # heavily referenced tables merely to add two leaf fields is unnecessary.
    inspector = inspect(op.get_bind())
    endpoint_columns = {column["name"] for column in inspector.get_columns("endpoints")}
    if "keepalive_adapter_id" not in endpoint_columns:
        op.add_column(
            "endpoints",
            sa.Column(
                "keepalive_adapter_id",
                sa.String(length=40),
                sa.CheckConstraint(
                    "keepalive_adapter_id IS NULL OR keepalive_adapter_id = 'server-script-v1'",
                    name="ck_endpoint_keepalive_adapter",
                ),
                nullable=True,
            ),
        )
    lease_columns = {column["name"] for column in inspector.get_columns("leases")}
    if "kind" not in lease_columns:
        op.add_column(
            "leases",
            sa.Column(
                "kind",
                sa.String(length=16),
                sa.CheckConstraint(
                    "kind IN ('workload', 'keepalive')",
                    name="ck_lease_kind",
                ),
                nullable=False,
                server_default="workload",
            ),
        )


def downgrade() -> None:
    # Additive discriminator/configuration columns are harmless to older code.
    # Retaining them avoids rebuilding two highly referenced SQLite tables;
    # downgrade-to-base still removes them when the initial tables are dropped.
    pass
