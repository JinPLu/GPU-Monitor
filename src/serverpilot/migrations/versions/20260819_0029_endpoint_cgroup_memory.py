"""store endpoint cgroup memory usage and limit

Revision ID: 20260819_0029
Revises: 20260819_0028
Create Date: 2026-08-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260819_0029"
down_revision = "20260819_0028"
branch_labels = None
depends_on = None

_TABLES = ("endpoint_telemetry_current", "endpoint_telemetry_snapshots")
_COLUMNS = (
    ("memory_limit_mib", sa.Integer()),
    ("memory_current_mib", sa.Integer()),
)


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    for table_name in _TABLES:
        if table_name not in inspector.get_table_names():
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        missing = [name for name, _type in _COLUMNS if name not in columns]
        if not missing:
            continue
        with op.batch_alter_table(table_name) as batch:
            for name, column_type in _COLUMNS:
                if name not in columns:
                    batch.add_column(sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    for table_name in _TABLES:
        if table_name not in inspector.get_table_names():
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        present = [name for name, _type in reversed(_COLUMNS) if name in columns]
        if not present:
            continue
        with op.batch_alter_table(table_name) as batch:
            for name in present:
                batch.drop_column(name)
