"""Manual WhatsApp invoice follow-up without automatic delivery.

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_followups",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("invoice_id", sa.String(32), primary_key=True),
        sa.Column("next_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promised_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "billing_followup_events",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("event_id", sa.String(32), primary_key=True),
        sa.Column("invoice_id", sa.String(32), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("open_slot", sa.String(8), nullable=True),
        sa.Column("address_key", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant", "invoice_id", "open_slot", name="uq_billing_followup_open"),
    )
    op.create_table(
        "billing_subscription_contacts",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("phone_sealed", sa.Text(), nullable=False),
        sa.Column("whatsapp_allowed", sa.Boolean(), nullable=False),
        sa.Column("consent_source", sa.String(128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("billing_subscription_contacts")
    op.drop_table("billing_followup_events")
    op.drop_table("billing_followups")
