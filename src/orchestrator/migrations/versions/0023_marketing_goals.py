"""SMART goals for the monthly report (RF-MKT-06)

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-14 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "marketing_goals",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("goal_id", sa.String(length=32), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("target", sa.Float(), nullable=False),
        sa.Column("note", sa.String(length=300), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "goal_id"),
    )
    op.create_index(op.f("ix_marketing_goals_month"), "marketing_goals", ["month"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_marketing_goals_month"), table_name="marketing_goals")
    op.drop_table("marketing_goals")
