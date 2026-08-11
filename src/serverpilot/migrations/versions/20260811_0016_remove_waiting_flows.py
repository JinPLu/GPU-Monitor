"""remove queued, reservation, and maintenance workflows

Revision ID: 20260811_0016
Revises: 20260811_0015
Create Date: 2026-08-11

Successful allocation requests remain as lease ownership evidence. Only
unallocated waiting records and the two removed scheduling features are
cleared.
"""

from alembic import op
from sqlalchemy import inspect


revision = "20260811_0016"
down_revision = "20260811_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "allocation_requests" in tables:
        bind.exec_driver_sql(
            "UPDATE allocation_requests "
            "SET state = 'REJECTED', blocked_reason = 'waiting queues are no longer supported' "
            "WHERE state IN ('QUEUED', 'PENDING_APPROVAL')"
        )
    if "reservations" in tables:
        bind.exec_driver_sql("DELETE FROM reservations")
    if "maintenance_windows" in tables:
        bind.exec_driver_sql("DELETE FROM maintenance_windows")


def downgrade() -> None:
    # Removed waiting state and time-window records cannot be reconstructed.
    pass
