"""persist automatic activation for routine claims

Revision ID: 20260719_0005
Revises: 20260719_0004
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260719_0005"
down_revision = "20260719_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("allocation_requests")}
    if "auto_activate" not in columns:
        op.add_column(
            "allocation_requests",
            sa.Column("auto_activate", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        with op.batch_alter_table("allocation_requests") as batch:
            batch.alter_column("auto_activate", server_default=None)


def downgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("allocation_requests")}
    if "auto_activate" in columns:
        with op.batch_alter_table("allocation_requests") as batch:
            batch.drop_column("auto_activate")
