"""persist local collector runtime settings

Revision ID: 20260812_0018
Revises: 20260812_0017
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260812_0018"
down_revision = "20260812_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if "runtime_settings" not in tables:
        op.create_table(
            "runtime_settings",
            sa.Column("key", sa.String(length=64), primary_key=True),
            sa.Column("value", sa.String(length=255), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if "runtime_settings" in tables:
        op.drop_table("runtime_settings")
