"""add bounded endpoint CPU and memory telemetry

Revision ID: 20260719_0003
Revises: 20260719_0002
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa


revision = "20260719_0003"
down_revision = "20260719_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "endpoint_telemetry_current",
        sa.Column("endpoint_id", sa.String(length=128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cpu_count", sa.Integer(), nullable=False),
        sa.Column("load_1m", sa.Float(), nullable=False),
        sa.Column("memory_total_mib", sa.Integer(), nullable=False),
        sa.Column("memory_available_mib", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["endpoint_id"], ["endpoints.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("endpoint_id"),
    )
    op.create_index(
        "ix_endpoint_telemetry_current_observed_at",
        "endpoint_telemetry_current",
        ["observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_endpoint_telemetry_current_observed_at",
        table_name="endpoint_telemetry_current",
    )
    op.drop_table("endpoint_telemetry_current")
