"""persist collector-discovered endpoint hardware kind

Revision ID: 20260815_0026
Revises: 20260814_0025
Create Date: 2026-08-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260815_0026"
down_revision = "20260814_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("endpoints")}
    checks = {check["name"] for check in inspector.get_check_constraints("endpoints")}
    if "resource_kind" not in columns or "ck_endpoint_resource_kind" not in checks:
        with op.batch_alter_table("endpoints") as batch:
            if "resource_kind" not in columns:
                batch.add_column(
                    sa.Column(
                        "resource_kind",
                        sa.String(length=16),
                        nullable=False,
                        server_default="unknown",
                    )
                )
            if "ck_endpoint_resource_kind" not in checks:
                batch.create_check_constraint(
                    "ck_endpoint_resource_kind",
                    "resource_kind IN ('unknown', 'cpu_only', 'gpu')",
                )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("endpoints")}
    checks = {check["name"] for check in inspector.get_check_constraints("endpoints")}
    if "resource_kind" in columns or "ck_endpoint_resource_kind" in checks:
        with op.batch_alter_table("endpoints") as batch:
            if "ck_endpoint_resource_kind" in checks:
                batch.drop_constraint("ck_endpoint_resource_kind", type_="check")
            if "resource_kind" in columns:
                batch.drop_column("resource_kind")
