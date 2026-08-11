"""add audited staged scheduler uploads

Revision ID: 20260731_0008
Revises: 20260731_0007
Create Date: 2026-07-31
"""

from alembic import op
import sqlalchemy as sa


revision = "20260731_0008"
down_revision = "20260731_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduler_transfers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("submission_key", sa.String(length=255), nullable=False),
        sa.Column("approval_ref", sa.String(length=500), nullable=False),
        sa.Column("local_source_path", sa.String(length=4000), nullable=False),
        sa.Column("remote_directory", sa.String(length=2000), nullable=False),
        sa.Column("remote_staged_path", sa.String(length=2000), nullable=True),
        sa.Column("source_size_bytes", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("error_message", sa.String(length=2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["target_id"], ["scheduler_targets.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_id",
            "submission_key",
            name="uq_scheduler_transfer_submission",
        ),
    )
    op.create_index(
        "ix_scheduler_transfer_project_created",
        "scheduler_transfers",
        ["project_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scheduler_transfer_project_created",
        table_name="scheduler_transfers",
    )
    op.drop_table("scheduler_transfers")
