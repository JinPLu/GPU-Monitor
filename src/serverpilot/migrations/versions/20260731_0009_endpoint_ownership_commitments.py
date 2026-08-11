"""add endpoint lifecycle ownership and direct-lease commitments

Revision ID: 20260731_0009
Revises: 20260731_0008
Create Date: 2026-07-31
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260731_0009"
down_revision = "20260731_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    endpoint_columns = {column["name"] for column in inspector.get_columns("endpoints")}
    if "owner_project_id" not in endpoint_columns:
        op.add_column(
            "endpoints",
            sa.Column("owner_project_id", sa.String(length=64), nullable=True),
        )
    if "lifecycle_state" not in endpoint_columns:
        op.add_column(
            "endpoints",
            sa.Column(
                "lifecycle_state",
                sa.String(length=16),
                nullable=False,
                server_default="active",
            ),
        )
        op.execute(
            "UPDATE endpoints SET lifecycle_state = "
            "CASE WHEN enabled THEN 'active' ELSE 'draining' END"
        )
    # Legacy endpoint scope existed only in inventory.  Keep it null here so
    # startup can reconcile configured endpoints to their actual project owner;
    # an unconfigured legacy endpoint is adopted explicitly by its project.
    endpoint_indexes = {index["name"] for index in inspector.get_indexes("endpoints")}
    if "ix_endpoints_owner_project_id" not in endpoint_indexes:
        op.create_index("ix_endpoints_owner_project_id", "endpoints", ["owner_project_id"])

    if "lease_endpoint_commitments" not in set(inspector.get_table_names()):
        op.create_table(
            "lease_endpoint_commitments",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("lease_id", sa.String(length=64), nullable=False),
            sa.Column("endpoint_id", sa.String(length=128), nullable=False),
            sa.Column("cpu_cores", sa.Float(), nullable=False, server_default="0"),
            sa.Column("memory_mib", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["lease_id"], ["leases.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["endpoint_id"], ["endpoints.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("lease_id", "endpoint_id", name="uq_lease_endpoint_commitment"),
        )
        op.create_index(
            "ix_endpoint_commitment_endpoint",
            "lease_endpoint_commitments",
            ["endpoint_id"],
        )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if "lease_endpoint_commitments" in set(inspector.get_table_names()):
        op.drop_index("ix_endpoint_commitment_endpoint", table_name="lease_endpoint_commitments")
        op.drop_table("lease_endpoint_commitments")
    endpoint_columns = {column["name"] for column in inspector.get_columns("endpoints")}
    if "owner_project_id" in endpoint_columns or "lifecycle_state" in endpoint_columns:
        with op.batch_alter_table("endpoints") as batch:
            if "ix_endpoints_owner_project_id" in {
                index["name"] for index in inspector.get_indexes("endpoints")
            }:
                batch.drop_index("ix_endpoints_owner_project_id")
            if "lifecycle_state" in endpoint_columns:
                batch.drop_column("lifecycle_state")
            if "owner_project_id" in endpoint_columns:
                batch.drop_column("owner_project_id")
