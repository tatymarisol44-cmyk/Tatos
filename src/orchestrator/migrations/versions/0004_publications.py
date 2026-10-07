"""publications on social networks, approved before they are published

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "publications",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("publication_id", sa.String(length=32), nullable=False),
        sa.Column("account_id", sa.String(length=32), nullable=False),
        sa.Column("network", sa.String(length=16), nullable=False),
        sa.Column("media_type", sa.String(length=8), nullable=False),
        sa.Column("media_format", sa.String(length=8), nullable=False),
        sa.Column("object_name", sa.String(length=160), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("caption", sa.String(length=2200), nullable=False),
        sa.Column("brief", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("needs_owner_approval", sa.Boolean(), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=True),
        sa.Column("visibility", sa.String(length=8), nullable=True),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant", "publication_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("publications")
