"""persist desired per-GPU keepalive policy and legacy scope

Revision ID: 20260812_0019
Revises: 20260812_0018
Create Date: 2026-08-12

The old keepalive feature reserved an endpoint's complete GPU set in one
lease.  It is intentionally *not* converted into a collection of new
per-GPU leases: existing active rows are tagged ``legacy_endpoint`` and stay
fail-closed until an operator has explicitly stopped and verified them.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = "20260812_0019"
down_revision = "20260812_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    endpoint_columns = {column["name"] for column in inspector.get_columns("endpoints")}
    if "keepalive_policy" not in endpoint_columns:
        op.add_column(
            "endpoints",
            sa.Column(
                "keepalive_policy",
                sa.String(length=32),
                sa.CheckConstraint(
                    "keepalive_policy IN ('disabled', 'idle_keepalive')",
                    name="ck_endpoint_keepalive_policy",
                ),
                nullable=False,
                server_default="disabled",
            ),
        )

    lease_columns = {column["name"] for column in inspector.get_columns("leases")}
    if "keepalive_scope" not in lease_columns:
        op.add_column(
            "leases",
            sa.Column(
                "keepalive_scope",
                sa.String(length=24),
                sa.CheckConstraint(
                    "keepalive_scope IS NULL OR keepalive_scope IN ('gpu', 'legacy_endpoint')",
                    name="ck_lease_keepalive_scope",
                ),
                nullable=True,
            ),
        )
        # Migration is deliberately one-way from the old semantic model: a
        # historic kind=keepalive row remains endpoint-scoped even on a
        # single-GPU endpoint.  Only fresh writes opt into scope='gpu'.
        bind.execute(
            text(
                "UPDATE leases SET keepalive_scope = 'legacy_endpoint' "
                "WHERE kind = 'keepalive'"
            )
        )


def downgrade() -> None:
    # Both columns are additive metadata. Retaining them avoids rebuilding
    # high-reference SQLite tables and cannot cause older code to allocate a
    # keepalive lease.
    pass
