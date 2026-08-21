"""record when a workload lease was first observed idle

Revision ID: 20260821_0030
Revises: 20260819_0029
Create Date: 2026-08-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260821_0030"
down_revision = "20260819_0029"
branch_labels = None
depends_on = None

_TABLE = "leases"
_COLUMN = "idle_since"


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        return
    # Existing leases start with no idle history.  The reconcile loop only sets
    # this from a fresh observation that shows no process, so a NULL here can
    # never be read as "already idle for a while".
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column(_COLUMN, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN not in columns:
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
