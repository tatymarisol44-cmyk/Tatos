"""test assignments: instruments sent to the patient's app; alerts that name the patient

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-14 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "instrument_assignments",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("assignment_id", sa.String(length=32), nullable=False),
        sa.Column("patient_id", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("instrument_name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("assigned_by", sa.String(length=128), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_id", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("tenant", "assignment_id"),
    )
    op.create_index(
        op.f("ix_instrument_assignments_patient_id"),
        "instrument_assignments",
        ["patient_id"],
        unique=False,
    )
    with op.batch_alter_table("channel_alerts") as batch:
        batch.add_column(sa.Column("subject_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("channel_alerts") as batch:
        batch.drop_column("subject_id")
    op.drop_index(op.f("ix_instrument_assignments_patient_id"), table_name="instrument_assignments")
    op.drop_table("instrument_assignments")
