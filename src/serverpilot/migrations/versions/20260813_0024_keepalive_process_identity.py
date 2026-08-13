"""persist the exact process identity owned by each keepalive

Revision ID: 20260813_0024
Revises: 20260813_0023
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa


revision = "20260813_0024"
down_revision = "20260813_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("keepalive_current") as batch:
        batch.add_column(sa.Column("expected_pid", sa.Integer()))
        batch.add_column(sa.Column("expected_boot_id", sa.String(length=120)))
        batch.add_column(sa.Column("expected_process_started_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("keepalive_current") as batch:
        batch.drop_column("expected_process_started_at")
        batch.drop_column("expected_boot_id")
        batch.drop_column("expected_pid")
