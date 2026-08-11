"""add a sealed observation profile to endpoint configuration

Revision ID: 20260810_0013
Revises: 20260809_0012
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
import json


revision = "20260810_0013"
down_revision = "20260809_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("endpoints")}
    if "observation_profile" not in columns:
        op.add_column(
            "endpoints",
            sa.Column(
                "observation_profile",
                sa.String(length=40),
                nullable=False,
                server_default="linux-nvidia",
            ),
        )

    # Versions before this revision stored a mutable command_prefix directly
    # in every scheduler target. Remove that executable/argv material during
    # upgrade and disable the target until an administrator assigns a sealed,
    # deployment-owned transport profile through the new connection contract.
    tables = set(inspect(bind).get_table_names())
    if "scheduler_targets" not in tables:
        return
    targets = sa.table(
        "scheduler_targets",
        sa.column("id", sa.String()),
        sa.column("connection_json", sa.Text()),
        sa.column("enabled", sa.Boolean()),
        sa.column("access_status", sa.String()),
        sa.column("access_message", sa.String()),
    )
    rows = bind.execute(sa.select(targets.c.id, targets.c.connection_json)).all()
    for target_id, raw_connection in rows:
        try:
            connection = json.loads(raw_connection)
        except (TypeError, json.JSONDecodeError):
            connection = {}
        if not isinstance(connection, dict):
            connection = {}
        if "command_prefix" not in connection:
            continue
        connection.pop("command_prefix", None)
        connection["transport_profile"] = "unconfigured"
        connection.setdefault("inspection_profile", "slurm-basic")
        bind.execute(
            targets.update()
            .where(targets.c.id == target_id)
            .values(
                connection_json=json.dumps(connection, separators=(",", ":"), sort_keys=True),
                enabled=False,
                access_status="unconfigured",
                access_message="legacy scheduler transport removed; administrator must configure a transport profile",
            )
        )


def downgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("endpoints")}
    if "observation_profile" in columns:
        op.drop_column("endpoints", "observation_profile")
