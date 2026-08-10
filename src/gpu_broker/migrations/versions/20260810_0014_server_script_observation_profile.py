"""record the sealed server-script-v1 endpoint observation profile

Revision ID: 20260810_0014
Revises: 20260810_0013
Create Date: 2026-08-10

``observation_profile`` is deliberately a String rather than a database enum,
so accepting the new sealed profile is an application-contract change and does
not require an unsafe table rebuild. This revision advances persisted databases
to that contract without rewriting endpoint identities or historical evidence.
"""


revision = "20260810_0014"
down_revision = "20260810_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No DDL: the existing sealed-profile String column already stores this value."""


def downgrade() -> None:
    """No DDL: downgrading must not rewrite endpoint monitoring evidence."""
