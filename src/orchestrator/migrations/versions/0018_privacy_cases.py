"""privacy cases: rights requests and breaches with their legal deadlines

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-09 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "privacy_cases",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("tenant", "case_id"),
    )
    op.create_table(
        "privacy_case_steps",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=32), nullable=False),
        sa.Column("step", sa.String(length=32), nullable=False),
        sa.Column("legal_basis", sa.String(length=80), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_by", sa.String(length=128), nullable=True),
        sa.Column("outcome", sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint("tenant", "case_id", "step"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("privacy_case_steps")
    op.drop_table("privacy_cases")
