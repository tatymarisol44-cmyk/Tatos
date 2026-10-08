"""rate limit of warm automatic WhatsApp replies (pseudonymous number, kind, last time)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-08 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "channel_auto_replies",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("network", sa.String(length=16), nullable=False),
        sa.Column("address_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "network", "address_key", "kind"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("channel_auto_replies")
