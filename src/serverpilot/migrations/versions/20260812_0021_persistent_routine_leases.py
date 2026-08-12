"""allow routine leases to remain active until explicit release

Revision ID: 20260812_0021
Revises: 20260812_0020
Create Date: 2026-08-12
"""

from alembic import op
import sqlalchemy as sa


revision = "20260812_0021"
down_revision = "20260812_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("leases") as batch:
        batch.alter_column(
            "expires_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    persistent_count = bind.scalar(
        sa.select(sa.func.count()).select_from(sa.table("leases")).where(
            sa.column("expires_at").is_(None)
        )
    )
    if persistent_count:
        raise RuntimeError("cannot downgrade while persistent routine leases exist")
    with op.batch_alter_table("leases") as batch:
        batch.alter_column(
            "expires_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
