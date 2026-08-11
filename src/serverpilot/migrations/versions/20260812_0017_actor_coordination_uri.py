"""add optional Codex coordination URI to actors

Revision ID: 20260812_0017
Revises: 20260811_0016
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260812_0017"
down_revision = "20260811_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("actors")}
    if "coordination_uri" not in columns:
        op.add_column(
            "actors", sa.Column("coordination_uri", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    # This optional leaf field is harmless to older code. Retaining it avoids
    # rebuilding the referenced actors table on SQLite; downgrade-to-base still
    # removes it with the initial table.
    pass
