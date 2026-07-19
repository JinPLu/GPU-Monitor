"""add project workload profiles

Revision ID: 20260719_0004
Revises: 20260719_0003
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260719_0004"
down_revision = "20260719_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workload_profiles",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("purpose", sa.String(length=1000), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("constraints_json", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workload_profiles_project_enabled",
        "workload_profiles",
        ["project_id", "enabled"],
    )
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("allocation_requests")}
    if "profile_id" not in columns:
        op.add_column(
            "allocation_requests", sa.Column("profile_id", sa.String(length=64), nullable=True)
        )
    indexes = {index["name"] for index in inspector.get_indexes("allocation_requests")}
    if "ix_allocation_requests_profile_id" not in indexes:
        op.create_index(
            "ix_allocation_requests_profile_id", "allocation_requests", ["profile_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("allocation_requests") as batch:
        batch.drop_index("ix_allocation_requests_profile_id")
        batch.drop_column("profile_id")
    op.drop_index("ix_workload_profiles_project_enabled", table_name="workload_profiles")
    op.drop_table("workload_profiles")
