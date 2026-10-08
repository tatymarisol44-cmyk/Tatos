"""numbered times offered by the WhatsApp assistant (pseudonymous number, no text)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-08 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "whatsapp_offers",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("address_key", sa.String(length=64), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "address_key"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("whatsapp_offers")
