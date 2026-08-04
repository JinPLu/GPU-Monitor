"""add generic resource planning core

Revision ID: 20260804_0011
Revises: 20260801_0010
Create Date: 2026-08-04
"""

from alembic import op
import sqlalchemy as sa


revision = "20260804_0011"
down_revision = "20260801_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resource_providers",
        sa.Column("id", sa.String(length=260), nullable=False),
        sa.Column("provider_type", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("endpoint_id", sa.String(length=128), nullable=True),
        sa.Column("scheduler_target_id", sa.String(length=64), nullable=True),
        sa.Column("native_ref_json", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider_type IN ('direct-gpu', 'host-capacity', 'scheduler')",
            name="ck_resource_provider_type",
        ),
        sa.CheckConstraint(
            "("
            "provider_type IN ('direct-gpu', 'host-capacity') "
            "AND endpoint_id IS NOT NULL "
            "AND scheduler_target_id IS NULL"
            ") OR ("
            "provider_type = 'scheduler' "
            "AND endpoint_id IS NULL "
            "AND scheduler_target_id IS NOT NULL"
            ")",
            name="ck_resource_provider_native_ref",
        ),
        sa.ForeignKeyConstraint(["endpoint_id"], ["endpoints.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["scheduler_target_id"], ["scheduler_targets.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_type",
            "endpoint_id",
            name="uq_resource_provider_endpoint",
        ),
        sa.UniqueConstraint(
            "provider_type",
            "scheduler_target_id",
            name="uq_resource_provider_scheduler_target",
        ),
    )
    op.create_index(
        "ix_resource_provider_type_enabled",
        "resource_providers",
        ["provider_type", "enabled"],
    )
    op.create_index("ix_resource_providers_endpoint_id", "resource_providers", ["endpoint_id"])
    op.create_index(
        "ix_resource_providers_scheduler_target_id",
        "resource_providers",
        ["scheduler_target_id"],
    )

    op.create_table(
        "allocatable_units",
        sa.Column("id", sa.String(length=320), nullable=False),
        sa.Column("provider_id", sa.String(length=260), nullable=False),
        sa.Column("unit_key", sa.String(length=260), nullable=False),
        sa.Column("unit_type", sa.String(length=32), nullable=False),
        sa.Column("endpoint_id", sa.String(length=128), nullable=True),
        sa.Column("gpu_id", sa.String(length=260), nullable=True),
        sa.Column("scheduler_target_id", sa.String(length=64), nullable=True),
        sa.Column("total_gpu_count", sa.Integer(), nullable=False),
        sa.Column("total_cpu_cores", sa.Float(), nullable=True),
        sa.Column("total_memory_mib", sa.Integer(), nullable=True),
        sa.Column("total_vram_mib", sa.Integer(), nullable=True),
        sa.Column("labels_json", sa.Text(), nullable=False),
        sa.Column("native_ref_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "unit_type IN ('gpu', 'host', 'scheduler-target')",
            name="ck_allocatable_unit_type",
        ),
        sa.CheckConstraint(
            "total_gpu_count >= 0 "
            "AND (total_cpu_cores IS NULL OR total_cpu_cores >= 0) "
            "AND (total_memory_mib IS NULL OR total_memory_mib >= 0) "
            "AND (total_vram_mib IS NULL OR total_vram_mib >= 0)",
            name="ck_allocatable_unit_quantities_nonnegative",
        ),
        sa.CheckConstraint(
            "("
            "unit_type = 'gpu' "
            "AND endpoint_id IS NOT NULL "
            "AND gpu_id IS NOT NULL "
            "AND scheduler_target_id IS NULL "
            "AND total_gpu_count = 1 "
            "AND total_vram_mib > 0"
            ") OR ("
            "unit_type = 'host' "
            "AND endpoint_id IS NOT NULL "
            "AND gpu_id IS NULL "
            "AND scheduler_target_id IS NULL "
            "AND (total_cpu_cores > 0 OR total_memory_mib > 0)"
            ") OR ("
            "unit_type = 'scheduler-target' "
            "AND endpoint_id IS NULL "
            "AND gpu_id IS NULL "
            "AND scheduler_target_id IS NOT NULL"
            ")",
            name="ck_allocatable_unit_native_ref",
        ),
        sa.ForeignKeyConstraint(["endpoint_id"], ["endpoints.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["gpu_id"], ["gpu_devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["resource_providers.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["scheduler_target_id"], ["scheduler_targets.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_id",
            "unit_key",
            name="uq_allocatable_unit_provider_key",
        ),
    )
    op.create_index(
        "ix_allocatable_unit_type_state",
        "allocatable_units",
        ["unit_type", "state"],
    )
    op.create_index("ix_allocatable_units_endpoint_id", "allocatable_units", ["endpoint_id"])
    op.create_index("ix_allocatable_units_gpu_id", "allocatable_units", ["gpu_id"])
    op.create_index("ix_allocatable_units_provider_id", "allocatable_units", ["provider_id"])
    op.create_index(
        "ix_allocatable_units_scheduler_target_id",
        "allocatable_units",
        ["scheduler_target_id"],
    )

    op.create_table(
        "resource_claims",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("task_ref", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.String(length=1000), nullable=False),
        sa.Column("provider_type", sa.String(length=32), nullable=True),
        sa.Column("requested_quantities_json", sa.Text(), nullable=False),
        sa.Column("forecast_json", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider_type IS NULL OR provider_type IN ('direct-gpu', 'host-capacity', 'scheduler')",
            name="ck_resource_claim_provider_type",
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_resource_claim_project_state",
        "resource_claims",
        ["project_id", "state"],
    )
    op.create_index(
        "ix_resource_claim_actor_created",
        "resource_claims",
        ["actor_id", "created_at"],
    )
    op.create_index("ix_resource_claims_actor_id", "resource_claims", ["actor_id"])
    op.create_index("ix_resource_claims_project_id", "resource_claims", ["project_id"])

    op.create_table(
        "resource_allocations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("claim_id", sa.String(length=64), nullable=False),
        sa.Column("unit_id", sa.String(length=320), nullable=False),
        sa.Column("native_lease_id", sa.String(length=64), nullable=True),
        sa.Column("native_scheduler_job_id", sa.String(length=64), nullable=True),
        sa.Column("quantities_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["resource_claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["native_lease_id"], ["leases.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["native_scheduler_job_id"], ["scheduler_jobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["unit_id"], ["allocatable_units.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_resource_allocation_claim_state",
        "resource_allocations",
        ["claim_id", "state"],
    )
    op.create_index(
        "ix_resource_allocation_unit_state",
        "resource_allocations",
        ["unit_id", "state"],
    )

    op.create_table(
        "resource_plan_evaluations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("claim_id", sa.String(length=64), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("task_ref", sa.String(length=255), nullable=False),
        sa.Column("baseline_runtime_seconds", sa.Integer(), nullable=False),
        sa.Column("marginal_min_saved_seconds", sa.Integer(), nullable=False),
        sa.Column("marginal_min_saved_ratio", sa.Float(), nullable=False),
        sa.Column("selected_candidate_key", sa.String(length=120), nullable=True),
        sa.Column("forecast_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"]),
        sa.ForeignKeyConstraint(["claim_id"], ["resource_claims.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_resource_plan_eval_project_created",
        "resource_plan_evaluations",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_resource_plan_eval_actor_created",
        "resource_plan_evaluations",
        ["actor_id", "created_at"],
    )
    op.create_index(
        "ix_resource_plan_evaluations_claim_id",
        "resource_plan_evaluations",
        ["claim_id"],
    )
    op.create_index(
        "ix_resource_plan_evaluations_actor_id",
        "resource_plan_evaluations",
        ["actor_id"],
    )
    op.create_index(
        "ix_resource_plan_evaluations_project_id",
        "resource_plan_evaluations",
        ["project_id"],
    )

    op.create_table(
        "resource_plan_candidates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("evaluation_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_key", sa.String(length=120), nullable=False),
        sa.Column("provider_type", sa.String(length=32), nullable=True),
        sa.Column("quantities_json", sa.Text(), nullable=False),
        sa.Column("predicted_runtime_seconds", sa.Integer(), nullable=False),
        sa.Column("predicted_saved_seconds", sa.Integer(), nullable=False),
        sa.Column("predicted_saved_ratio", sa.Float(), nullable=False),
        sa.Column("satisfies_marginal_threshold", sa.Boolean(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("rejection_reason", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(
            ["evaluation_id"], ["resource_plan_evaluations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "evaluation_id",
            "candidate_key",
            name="uq_resource_plan_candidate_eval_key",
        ),
    )
    op.create_index(
        "ix_resource_plan_candidate_selected",
        "resource_plan_candidates",
        ["evaluation_id", "selected"],
    )

    op.create_table(
        "resource_run_actuals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("evaluation_id", sa.String(length=64), nullable=True),
        sa.Column("claim_id", sa.String(length=64), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("task_ref", sa.String(length=255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("quantities_json", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("notes_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"]),
        sa.ForeignKeyConstraint(["claim_id"], ["resource_claims.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["evaluation_id"], ["resource_plan_evaluations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_resource_run_actual_project_created",
        "resource_run_actuals",
        ["project_id", "created_at"],
    )
    op.create_index("ix_resource_run_actual_task_ref", "resource_run_actuals", ["task_ref"])
    op.create_index("ix_resource_run_actuals_evaluation_id", "resource_run_actuals", ["evaluation_id"])
    op.create_index("ix_resource_run_actuals_claim_id", "resource_run_actuals", ["claim_id"])
    op.create_index("ix_resource_run_actuals_actor_id", "resource_run_actuals", ["actor_id"])
    op.create_index("ix_resource_run_actuals_project_id", "resource_run_actuals", ["project_id"])

    _seed_existing_native_resources()


def _seed_existing_native_resources() -> None:
    op.execute(
        """
        INSERT INTO resource_providers (
            id, provider_type, display_name, endpoint_id, scheduler_target_id,
            native_ref_json, metadata_json, enabled, created_at, updated_at
        )
        SELECT
            'direct-gpu:endpoint:' || e.id,
            'direct-gpu',
            e.id || ' direct GPUs',
            e.id,
            NULL,
            '{}',
            '{}',
            e.enabled,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM endpoints e
        WHERE EXISTS (
            SELECT 1 FROM gpu_devices g WHERE g.endpoint_id = e.id
        )
        """
    )
    op.execute(
        """
        INSERT INTO resource_providers (
            id, provider_type, display_name, endpoint_id, scheduler_target_id,
            native_ref_json, metadata_json, enabled, created_at, updated_at
        )
        SELECT
            'host-capacity:endpoint:' || e.id,
            'host-capacity',
            e.id || ' host capacity',
            e.id,
            NULL,
            '{}',
            '{}',
            e.enabled,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM endpoints e
        """
    )
    op.execute(
        """
        INSERT INTO resource_providers (
            id, provider_type, display_name, endpoint_id, scheduler_target_id,
            native_ref_json, metadata_json, enabled, created_at, updated_at
        )
        SELECT
            'scheduler:' || s.id,
            'scheduler',
            s.display_name,
            NULL,
            s.id,
            '{}',
            '{}',
            s.enabled,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM scheduler_targets s
        """
    )

    op.execute(
        """
        INSERT INTO allocatable_units (
            id, provider_id, unit_key, unit_type, endpoint_id, gpu_id, scheduler_target_id,
            total_gpu_count, total_cpu_cores, total_memory_mib, total_vram_mib,
            labels_json, native_ref_json, state, enabled, created_at, updated_at
        )
        SELECT
            'gpu:' || g.id,
            'direct-gpu:endpoint:' || g.endpoint_id,
            g.id,
            'gpu',
            g.endpoint_id,
            g.id,
            NULL,
            1,
            NULL,
            NULL,
            g.total_vram_mib,
            g.labels_json,
            '{}',
            CASE WHEN e.enabled AND g.enabled AND g.present THEN 'available' ELSE 'unavailable' END,
            e.enabled AND g.enabled AND g.present,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM gpu_devices g
        JOIN endpoints e ON e.id = g.endpoint_id
        """
    )
    op.execute(
        """
        INSERT INTO allocatable_units (
            id, provider_id, unit_key, unit_type, endpoint_id, gpu_id, scheduler_target_id,
            total_gpu_count, total_cpu_cores, total_memory_mib, total_vram_mib,
            labels_json, native_ref_json, state, enabled, created_at, updated_at
        )
        SELECT
            'host:' || e.id,
            'host-capacity:endpoint:' || e.id,
            e.id,
            'host',
            e.id,
            NULL,
            NULL,
            0,
            CAST(t.cpu_count AS FLOAT),
            t.memory_total_mib,
            NULL,
            e.labels_json,
            '{}',
            CASE WHEN e.enabled AND e.lifecycle_state = 'active' THEN 'available' ELSE 'unavailable' END,
            e.enabled AND e.lifecycle_state = 'active',
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM endpoints e
        JOIN endpoint_telemetry_current t ON t.endpoint_id = e.id
        WHERE t.cpu_count > 0 OR t.memory_total_mib > 0
        """
    )
    op.execute(
        """
        INSERT INTO allocatable_units (
            id, provider_id, unit_key, unit_type, endpoint_id, gpu_id, scheduler_target_id,
            total_gpu_count, total_cpu_cores, total_memory_mib, total_vram_mib,
            labels_json, native_ref_json, state, enabled, created_at, updated_at
        )
        SELECT
            'scheduler-target:' || s.id,
            'scheduler:' || s.id,
            s.id,
            'scheduler-target',
            NULL,
            NULL,
            s.id,
            0,
            NULL,
            NULL,
            NULL,
            s.capabilities_json,
            '{}',
            CASE WHEN s.enabled THEN 'available' ELSE 'unavailable' END,
            s.enabled,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM scheduler_targets s
        """
    )


def downgrade() -> None:
    op.drop_index("ix_resource_run_actuals_project_id", table_name="resource_run_actuals")
    op.drop_index("ix_resource_run_actuals_actor_id", table_name="resource_run_actuals")
    op.drop_index("ix_resource_run_actuals_claim_id", table_name="resource_run_actuals")
    op.drop_index("ix_resource_run_actuals_evaluation_id", table_name="resource_run_actuals")
    op.drop_index("ix_resource_run_actual_task_ref", table_name="resource_run_actuals")
    op.drop_index(
        "ix_resource_run_actual_project_created",
        table_name="resource_run_actuals",
    )
    op.drop_table("resource_run_actuals")

    op.drop_index(
        "ix_resource_plan_candidate_selected",
        table_name="resource_plan_candidates",
    )
    op.drop_table("resource_plan_candidates")

    op.drop_index(
        "ix_resource_plan_evaluations_project_id",
        table_name="resource_plan_evaluations",
    )
    op.drop_index(
        "ix_resource_plan_evaluations_actor_id",
        table_name="resource_plan_evaluations",
    )
    op.drop_index(
        "ix_resource_plan_evaluations_claim_id",
        table_name="resource_plan_evaluations",
    )
    op.drop_index(
        "ix_resource_plan_eval_actor_created",
        table_name="resource_plan_evaluations",
    )
    op.drop_index(
        "ix_resource_plan_eval_project_created",
        table_name="resource_plan_evaluations",
    )
    op.drop_table("resource_plan_evaluations")

    op.drop_index(
        "ix_resource_allocation_unit_state",
        table_name="resource_allocations",
    )
    op.drop_index(
        "ix_resource_allocation_claim_state",
        table_name="resource_allocations",
    )
    op.drop_table("resource_allocations")

    op.drop_index("ix_resource_claims_project_id", table_name="resource_claims")
    op.drop_index("ix_resource_claims_actor_id", table_name="resource_claims")
    op.drop_index("ix_resource_claim_actor_created", table_name="resource_claims")
    op.drop_index("ix_resource_claim_project_state", table_name="resource_claims")
    op.drop_table("resource_claims")

    op.drop_index(
        "ix_allocatable_units_scheduler_target_id",
        table_name="allocatable_units",
    )
    op.drop_index("ix_allocatable_units_provider_id", table_name="allocatable_units")
    op.drop_index("ix_allocatable_units_gpu_id", table_name="allocatable_units")
    op.drop_index("ix_allocatable_units_endpoint_id", table_name="allocatable_units")
    op.drop_index("ix_allocatable_unit_type_state", table_name="allocatable_units")
    op.drop_table("allocatable_units")

    op.drop_index(
        "ix_resource_providers_scheduler_target_id",
        table_name="resource_providers",
    )
    op.drop_index("ix_resource_providers_endpoint_id", table_name="resource_providers")
    op.drop_index("ix_resource_provider_type_enabled", table_name="resource_providers")
    op.drop_table("resource_providers")
