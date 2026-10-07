"""incoming messages: dedup, alerts for staff, pseudonymous opt-outs

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "inbound_events",
        sa.Column("network", sa.String(length=16), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("intent", sa.String(length=16), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("network", "message_id"),
    )
    op.create_table(
        "channel_alerts",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("alert_id", sa.String(length=32), nullable=False),
        sa.Column("network", sa.String(length=16), nullable=False),
        sa.Column("account_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("address", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_by", sa.String(length=128), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant", "alert_id"),
    )
    op.create_table(
        "channel_optouts",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("network", sa.String(length=16), nullable=False),
        sa.Column("address_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "network", "address_key"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("channel_optouts")
    op.drop_table("channel_alerts")
    op.drop_table("inbound_events")
