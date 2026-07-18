"""add bounded latest-telemetry table

Revision ID: 20260719_0002
Revises: 20260719_0001
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa


revision = "20260719_0002"
down_revision = "20260719_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telemetry_current",
        sa.Column("gpu_id", sa.String(length=260), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("memory_used_mib", sa.Integer(), nullable=False),
        sa.Column("memory_free_mib", sa.Integer(), nullable=False),
        sa.Column("gpu_utilization_pct", sa.Integer(), nullable=True),
        sa.Column("memory_utilization_pct", sa.Integer(), nullable=True),
        sa.Column("temperature_c", sa.Integer(), nullable=True),
        sa.Column("power_watts", sa.Float(), nullable=True),
        sa.Column("pstate", sa.String(length=32), nullable=True),
        sa.Column("health", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["gpu_id"], ["gpu_devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("gpu_id"),
    )
    op.create_index("ix_telemetry_current_observed_at", "telemetry_current", ["observed_at"])


def downgrade() -> None:
    op.drop_index("ix_telemetry_current_observed_at", table_name="telemetry_current")
    op.drop_table("telemetry_current")
