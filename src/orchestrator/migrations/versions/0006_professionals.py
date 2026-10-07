"""professionals of an establishment, each with their own profession pack

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "professionals",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("professional_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("pack_id", sa.String(length=64), nullable=False),
        sa.Column("staff_id", sa.String(length=128), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "professional_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("professionals")
