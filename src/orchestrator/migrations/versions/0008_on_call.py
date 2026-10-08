"""on-call contacts, alert notifications, escalation state on channel alerts

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-08 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "on_call_contacts",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("contact_id", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("telegram_chat_id", sa.String(length=32), nullable=True),
        sa.Column("whatsapp_number", sa.String(length=20), nullable=True),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "contact_id"),
    )
    op.create_table(
        "alert_notifications",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("notification_id", sa.String(length=32), nullable=False),
        sa.Column("alert_id", sa.String(length=32), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.String(length=32), nullable=True),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "notification_id"),
    )
    op.create_index(
        op.f("ix_alert_notifications_alert_id"),
        "alert_notifications",
        ["alert_id"],
        unique=False,
    )
    # Existing alerts become level 0 with nothing due: they were created before on-call.
    with op.batch_alter_table("channel_alerts") as batch:
        batch.add_column(
            sa.Column("escalation_level", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("next_escalation_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("acknowledged_by", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("channel_alerts") as batch:
        batch.drop_column("acknowledged_at")
        batch.drop_column("acknowledged_by")
        batch.drop_column("next_escalation_at")
        batch.drop_column("escalation_level")
    op.drop_index(op.f("ix_alert_notifications_alert_id"), table_name="alert_notifications")
    op.drop_table("alert_notifications")
    op.drop_table("on_call_contacts")
