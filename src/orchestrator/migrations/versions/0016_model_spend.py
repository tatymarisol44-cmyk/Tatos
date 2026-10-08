"""monthly model spend per tenant (cost cap, threat T14)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-09 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "model_spend",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("llm_calls", sa.Integer(), nullable=False),
        sa.Column("unpriced_calls", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "period"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("model_spend")
