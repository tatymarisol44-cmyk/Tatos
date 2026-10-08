"""agenda: professionals' working hours; appointments belong to a professional

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-08 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "professional_hours",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("professional_id", sa.String(length=64), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_minute", sa.Integer(), nullable=False),
        sa.Column("end_minute", sa.Integer(), nullable=False),
        sa.Column("slot_minutes", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "professional_id", "weekday", "start_minute"),
    )
    op.add_column(
        "crm_appointments", sa.Column("professional_id", sa.String(length=64), nullable=True)
    )
    op.create_index(
        op.f("ix_crm_appointments_professional_id"),
        "crm_appointments",
        ["professional_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_crm_appointments_professional_id"), table_name="crm_appointments")
    op.drop_column("crm_appointments", "professional_id")
    op.drop_table("professional_hours")
