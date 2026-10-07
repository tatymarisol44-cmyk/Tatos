"""social channel accounts per establishment and professional

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "channel_accounts",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=32), nullable=False),
        sa.Column("network", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("handle", sa.String(length=120), nullable=False),
        sa.Column("professional_id", sa.String(length=64), nullable=True),
        sa.Column("secret_ref", sa.String(length=64), nullable=False),
        sa.Column("audited", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "account_id"),
    )
    op.create_index(
        "ix_channel_accounts_lookup",
        "channel_accounts",
        ["tenant", "network", "external_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_channel_accounts_lookup", table_name="channel_accounts")
    op.drop_table("channel_accounts")
