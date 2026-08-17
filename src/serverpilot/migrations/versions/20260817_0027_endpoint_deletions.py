"""persist endpoint deletion tombstones

Revision ID: 20260817_0027
Revises: 20260815_0026
Create Date: 2026-08-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260817_0027"
down_revision = "20260815_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if "endpoint_deletions" in inspector.get_table_names():
        return
    op.create_table(
        "endpoint_deletions",
        sa.Column("endpoint_id", sa.String(length=128), primary_key=True),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if "endpoint_deletions" not in inspector.get_table_names():
        return
    op.drop_table("endpoint_deletions")
