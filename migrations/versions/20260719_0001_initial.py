"""initial global GPU broker schema

Revision ID: 20260719_0001
Revises:
Create Date: 2026-07-19
"""

from alembic import op

from gpu_broker.models import Base


revision = "20260719_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This revision is the immutable initial model snapshot for the pilot. New
    # schema changes require a later Alembic revision, not direct DB mutation.
    tables = [table for table in Base.metadata.sorted_tables if table.name != "telemetry_current"]
    Base.metadata.create_all(bind=op.get_bind(), tables=tables, checkfirst=False)


def downgrade() -> None:
    tables = [table for table in Base.metadata.sorted_tables if table.name != "telemetry_current"]
    Base.metadata.drop_all(bind=op.get_bind(), tables=tables, checkfirst=False)
