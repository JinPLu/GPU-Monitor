"""add endpoint telemetry history

Revision ID: 20260809_0012
Revises: 20260804_0011
Create Date: 2026-08-09
"""

from alembic import op
import sqlalchemy as sa


revision = "20260809_0012"
down_revision = "20260804_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("endpoint_telemetry_current") as batch:
        batch.add_column(sa.Column("cpu_total_ticks", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("cpu_idle_ticks", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("cpu_utilization_pct", sa.Float(), nullable=True))

    op.create_table(
        "endpoint_telemetry_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("endpoint_id", sa.String(length=128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cpu_count", sa.Integer(), nullable=False),
        sa.Column("load_1m", sa.Float(), nullable=False),
        sa.Column("cpu_total_ticks", sa.Integer(), nullable=True),
        sa.Column("cpu_idle_ticks", sa.Integer(), nullable=True),
        sa.Column("cpu_utilization_pct", sa.Float(), nullable=True),
        sa.Column("memory_total_mib", sa.Integer(), nullable=False),
        sa.Column("memory_available_mib", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["endpoint_id"], ["endpoints.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_endpoint_telemetry_endpoint_observed",
        "endpoint_telemetry_snapshots",
        ["endpoint_id", "observed_at"],
    )
    op.create_index(
        "ix_endpoint_telemetry_snapshots_endpoint_id",
        "endpoint_telemetry_snapshots",
        ["endpoint_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_endpoint_telemetry_snapshots_endpoint_id",
        table_name="endpoint_telemetry_snapshots",
    )
    op.drop_index(
        "ix_endpoint_telemetry_endpoint_observed",
        table_name="endpoint_telemetry_snapshots",
    )
    op.drop_table("endpoint_telemetry_snapshots")

    with op.batch_alter_table("endpoint_telemetry_current") as batch:
        batch.drop_column("cpu_utilization_pct")
        batch.drop_column("cpu_idle_ticks")
        batch.drop_column("cpu_total_ticks")
