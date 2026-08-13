"""store verified PCI-order CUDA ordinals

Revision ID: 20260814_0025
Revises: 20260813_0024
Create Date: 2026-08-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260814_0025"
down_revision = "20260813_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("gpu_devices")}
    if "cuda_ordinal" not in columns:
        with op.batch_alter_table("gpu_devices") as batch:
            batch.add_column(sa.Column("cuda_ordinal", sa.Integer()))


def downgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("gpu_devices")}
    if "cuda_ordinal" in columns:
        with op.batch_alter_table("gpu_devices") as batch:
            batch.drop_column("cuda_ordinal")
