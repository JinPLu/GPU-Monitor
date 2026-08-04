"""track GPU presence across complete endpoint observations

Revision ID: 20260801_0010
Revises: 20260731_0009
Create Date: 2026-08-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260801_0010"
down_revision = "20260731_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    gpu_columns = {column["name"] for column in inspector.get_columns("gpu_devices")}
    if "present" not in gpu_columns:
        op.add_column(
            "gpu_devices",
            sa.Column(
                "present",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )
    if "absent_at" not in gpu_columns:
        op.add_column("gpu_devices", sa.Column("absent_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    gpu_columns = {column["name"] for column in inspector.get_columns("gpu_devices")}
    if "present" in gpu_columns or "absent_at" in gpu_columns:
        with op.batch_alter_table("gpu_devices") as batch:
            if "absent_at" in gpu_columns:
                batch.drop_column("absent_at")
            if "present" in gpu_columns:
                batch.drop_column("present")
