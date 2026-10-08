"""psychological instruments defined by professionals, and their results

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-08 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "instruments",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("author_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "instrument_id", "version"),
    )
    op.create_table(
        "instrument_results",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("result_id", sa.String(length=32), nullable=False),
        sa.Column("patient_id", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("instrument_name", sa.String(length=120), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("total", sa.Float(), nullable=True),
        sa.Column("subscales", sa.JSON(), nullable=False),
        sa.Column("band", sa.String(length=80), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=True),
        sa.Column("alerts", sa.JSON(), nullable=False),
        sa.Column("note", sa.String(length=2000), nullable=True),
        sa.Column("author_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "result_id"),
    )
    op.create_index(
        op.f("ix_instrument_results_patient_id"),
        "instrument_results",
        ["patient_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_instrument_results_patient_id"), table_name="instrument_results")
    op.drop_table("instrument_results")
    op.drop_table("instruments")
