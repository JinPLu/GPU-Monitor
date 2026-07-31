"""add external scheduler targets, profile grants and jobs

Revision ID: 20260731_0006
Revises: 20260719_0005
Create Date: 2026-07-31
"""

from alembic import op
import sqlalchemy as sa


revision = "20260731_0006"
down_revision = "20260719_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduler_targets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("adapter", sa.String(length=40), nullable=False),
        sa.Column("connection_json", sa.Text(), nullable=False),
        sa.Column("credential_refs_json", sa.Text(), nullable=False),
        sa.Column("capabilities_json", sa.Text(), nullable=False),
        sa.Column("access_hint", sa.String(length=2000), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.add_column(
        "workload_profiles",
        sa.Column(
            "runtime_kind",
            sa.String(length=32),
            nullable=False,
            server_default="direct-gpu",
        ),
    )
    op.add_column(
        "workload_profiles",
        sa.Column("scheduler_target_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "workload_profiles",
        sa.Column("scheduler_spec_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "workload_profiles",
        sa.Column("scheduler_script", sa.Text(), nullable=True),
    )
    op.add_column(
        "workload_profiles",
        sa.Column(
            "grant_all_projects",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "workload_profiles",
        sa.Column(
            "retain_submission_body",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_workload_profiles_scheduler_target_id",
        "workload_profiles",
        ["scheduler_target_id"],
    )

    op.create_table(
        "workload_profile_grants",
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["workload_profiles.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("profile_id", "project_id"),
    )

    op.create_table(
        "scheduler_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("profile_id", sa.String(length=64), nullable=True),
        sa.Column("submission_key", sa.String(length=255), nullable=False),
        sa.Column("task_ref", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.String(length=1000), nullable=False),
        sa.Column("approval_ref", sa.String(length=500), nullable=True),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("script_digest", sa.String(length=64), nullable=False),
        sa.Column("script_body", sa.Text(), nullable=True),
        sa.Column("retain_submission_body", sa.Boolean(), nullable=False),
        sa.Column("scheduler_job_id", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("raw_state", sa.String(length=80), nullable=True),
        sa.Column("allocated_tres_json", sa.Text(), nullable=False),
        sa.Column("node_list", sa.String(length=2000), nullable=True),
        sa.Column("stdout_path", sa.String(length=2000), nullable=True),
        sa.Column("stderr_path", sa.String(length=2000), nullable=True),
        sa.Column("exit_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=2000), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["target_id"], ["scheduler_targets.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_id",
            "submission_key",
            name="uq_scheduler_job_submission",
        ),
    )
    op.create_index(
        "ix_scheduler_job_target_state",
        "scheduler_jobs",
        ["target_id", "state"],
    )
    op.create_index(
        "ix_scheduler_job_project_created",
        "scheduler_jobs",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_scheduler_jobs_profile_id",
        "scheduler_jobs",
        ["profile_id"],
    )
    op.create_index(
        "ix_scheduler_jobs_scheduler_job_id",
        "scheduler_jobs",
        ["scheduler_job_id"],
    )

    op.create_table(
        "scheduler_job_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("raw_state", sa.String(length=80), nullable=True),
        sa.Column("detail_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["scheduler_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scheduler_job_event_job_time",
        "scheduler_job_events",
        ["job_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scheduler_job_event_job_time",
        table_name="scheduler_job_events",
    )
    op.drop_table("scheduler_job_events")
    op.drop_index("ix_scheduler_jobs_scheduler_job_id", table_name="scheduler_jobs")
    op.drop_index("ix_scheduler_jobs_profile_id", table_name="scheduler_jobs")
    op.drop_index("ix_scheduler_job_project_created", table_name="scheduler_jobs")
    op.drop_index("ix_scheduler_job_target_state", table_name="scheduler_jobs")
    op.drop_table("scheduler_jobs")
    op.drop_table("workload_profile_grants")
    op.drop_index(
        "ix_workload_profiles_scheduler_target_id",
        table_name="workload_profiles",
    )
    op.drop_column("workload_profiles", "retain_submission_body")
    op.drop_column("workload_profiles", "grant_all_projects")
    op.drop_column("workload_profiles", "scheduler_script")
    op.drop_column("workload_profiles", "scheduler_spec_json")
    op.drop_column("workload_profiles", "scheduler_target_id")
    op.drop_column("workload_profiles", "runtime_kind")
    op.drop_table("scheduler_targets")
