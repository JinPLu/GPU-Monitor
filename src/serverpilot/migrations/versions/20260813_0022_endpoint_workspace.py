"""add remote workspace metadata to endpoints

Revision ID: 20260813_0022
Revises: 20260812_0021
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260813_0022"
down_revision = "20260812_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable preserves every legacy endpoint without inventing a remote path.
    # All creation surfaces require the value after this migration.
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("endpoints")}
    if "workspace_path" not in columns:
        with op.batch_alter_table("endpoints") as batch:
            batch.add_column(sa.Column("workspace_path", sa.String(length=2000)))


def downgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("endpoints")}
    if "workspace_path" in columns:
        with op.batch_alter_table("endpoints") as batch:
            batch.drop_column("workspace_path")
