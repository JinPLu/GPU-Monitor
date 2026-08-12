"""remove obsolete derived fields and keep plain local records

Revision ID: 20260812_0020
Revises: 20260812_0019
Create Date: 2026-08-12
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260812_0020"
down_revision = "20260812_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "audit_events" in tables:
        columns = {column["name"] for column in inspector.get_columns("audit_events")}
        with op.batch_alter_table("audit_events") as batch:
            if "before_hash" in columns:
                batch.drop_column("before_hash")
            if "after_hash" in columns:
                batch.drop_column("after_hash")
            if "before_json" not in columns:
                batch.add_column(sa.Column("before_json", sa.Text()))
            if "after_json" not in columns:
                batch.add_column(sa.Column("after_json", sa.Text()))

    if "scheduler_jobs" in tables:
        columns = {column["name"] for column in inspector.get_columns("scheduler_jobs")}
        with op.batch_alter_table("scheduler_jobs") as batch:
            if "script_digest" in columns:
                batch.drop_column("script_digest")
            if "script_digest_scheme" in columns:
                batch.drop_column("script_digest_scheme")

    if "leases" in tables:
        columns = {column["name"] for column in inspector.get_columns("leases")}
        if "keepalive_scope" in columns:
            with op.batch_alter_table("leases") as batch:
                batch.drop_constraint("ck_lease_keepalive_scope", type_="check")
                batch.drop_column("keepalive_scope")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "leases" in tables:
        columns = {column["name"] for column in inspector.get_columns("leases")}
        if "keepalive_scope" not in columns:
            with op.batch_alter_table("leases") as batch:
                batch.add_column(
                    sa.Column(
                        "keepalive_scope",
                        sa.String(length=32),
                        nullable=False,
                        server_default="per_gpu",
                    )
                )
