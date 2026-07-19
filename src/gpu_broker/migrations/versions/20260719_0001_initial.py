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


# Keep the initial revision independent from future model additions. New
# tables must arrive in a later revision so upgrades remain deterministic.
INITIAL_TABLES = frozenset(
    {
        "revisions",
        "endpoints",
        "endpoint_projects",
        "gpu_devices",
        "telemetry_snapshots",
        "process_observations",
        "projects",
        "actors",
        "actor_projects",
        "api_tokens",
        "allocation_requests",
        "leases",
        "lease_resources",
        "workload_bindings",
        "reservations",
        "maintenance_windows",
        "audit_events",
        "alerts",
        "idempotency_records",
        "provider_states",
    }
)


def _initial_tables():
    return [table for table in Base.metadata.sorted_tables if table.name in INITIAL_TABLES]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=_initial_tables(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=_initial_tables(), checkfirst=False)
